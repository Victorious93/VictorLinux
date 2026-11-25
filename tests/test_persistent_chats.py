"""Encrypted persistent chats: vault crypto, lock/auto-lock, export/clear, API routes, Debian parity."""
import json
import os

import pytest

import cc_ext
import chatvault
import control_center as cc
from test_control_api import NO_BODY, request, serving

DEB_LIB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages",
                       "usr", "local", "lib", "victorlinux", "victorlib")
PW = "correct horse battery"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(chatvault, "SCRYPT", {"n": 2 ** 10, "r": 8, "p": 1})


def test_roundtrip_and_nothing_plain_on_disk(tmp_path):
    v = chatvault.Vault(str(tmp_path))
    v.create(PW)
    c = v.new()
    v.add_message(c["id"], "user", "my secret plan about zebras")
    v.add_message(c["id"], "assistant", "noted")
    v.set_settings({"system_prompt": "be brief", "temperature": 0.2})
    for f in os.listdir(tmp_path):
        data = open(tmp_path / f, "rb").read()
        assert b"zebras" not in data and b"be brief" not in data and PW.encode() not in data
    assert oct(os.stat(tmp_path / (c["id"] + ".vlc")).st_mode & 0o777) == "0o600"
    w = chatvault.Vault(str(tmp_path))                 # "after reboot": a new process, same partition
    with pytest.raises(chatvault.VaultError):
        w.list()
    with pytest.raises(chatvault.VaultError, match="Wrong"):
        w.unlock("wrong passphrase!!")
    w.unlock(PW)
    got = w.get(c["id"])
    assert [m["content"] for m in got["messages"]] == ["my secret plan about zebras", "noted"]
    assert got["title"].startswith("my secret plan")
    assert w.get_settings()["system_prompt"] == "be brief"
    assert w.history(c["id"])[0] == {"role": "system", "content": "be brief"}


def test_tamper_swap_and_bad_ids(tmp_path):
    v = chatvault.Vault(str(tmp_path))
    v.create(PW)
    a, b = v.new("a"), v.new("b")
    pa, pb = tmp_path / (a["id"] + ".vlc"), tmp_path / (b["id"] + ".vlc")
    pa.write_bytes(pb.read_bytes())                   # swapped file is detected (name is bound as associated data)
    with pytest.raises(chatvault.VaultError, match="damaged"):
        v.get(a["id"])
    assert v.list()["damaged"] == 1
    for bad in ("../vault", "", "ABCDEFGHIJKL"):
        with pytest.raises(chatvault.VaultError):
            v.get(bad)


def test_weak_passphrase_settings_bounds_and_auto_lock(tmp_path):
    now = [1000.0]
    v = chatvault.Vault(str(tmp_path), clock=lambda: now[0])
    for weak in ("short", "aaaaaaaaaaaa"):
        with pytest.raises(chatvault.VaultError):
            v.create(weak)
    v.create(PW)
    with pytest.raises(chatvault.VaultError):
        v.set_settings({"temperature": 5})
    with pytest.raises(chatvault.VaultError):
        v.set_settings({"telemetry": True})
    v.set_settings({"auto_lock_minutes": 1})
    now[0] += 61
    assert not v.unlocked() and v.key is None


def test_export_and_clear(tmp_path):
    v = chatvault.Vault(str(tmp_path))
    v.create(PW)
    c = v.new()
    v.add_message(c["id"], "user", "hello there")
    assert "**User:** hello there" in v.export("md")
    assert json.loads(v.export("json"))["chats"][0]["id"] == c["id"]
    v.lock()
    r = v.clear()                                     # works while locked
    assert r["deleted_chats"] == 1 and v.exists()
    v.clear(forget_vault=True)
    assert not v.exists() and os.listdir(tmp_path) == []


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cache" / "cc.token"))
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cc, "APP", cc.App(), raising=False)
    cc_ext.load()
    chats = cc_ext.MODULES["chats"]
    monkeypatch.setattr(chats, "_VAULT", {"v": None, "dir": None})
    seen = []
    monkeypatch.setattr(chats, "ask_model", lambda app, msgs, s: seen.append(msgs) or f"echo: {msgs[-1]['content']}")
    monkeypatch.setattr(cc.APP.llm, "running", lambda: True)
    with serving(cc.Handler) as port:
        yield port, {"X-Victor-Token": cc.APP.token}, tmp_path, seen
    cc_ext.MODULES.clear(); cc_ext.ROUTES.clear()


def call(port, h, method, path, body=None):
    status, data = request(port, method, path, body if method == "POST" else NO_BODY, h)
    return status, json.loads(data)


def test_api_flow(control):
    port, h, tmp, seen = control
    assert request(port, "GET", "/api/chats/state")[0] == 401
    s, st = call(port, h, "GET", "/api/chats/state")
    assert s == 200 and not st["exists"] and "Temporary session" in st["warning"]
    assert call(port, h, "POST", "/api/chats/setup", {"passphrase": PW, "confirm": "nope"})[0] == 400
    assert call(port, h, "POST", "/api/chats/setup", {"passphrase": PW, "confirm": PW})[0] == 200
    s, c = call(port, h, "POST", "/api/chats/send", {"message": "hi model"})
    assert s == 200 and [m["content"] for m in c["messages"]] == ["hi model", "echo: hi model"]
    call(port, h, "POST", "/api/chats/settings", {"settings": {"system_prompt": "pirate"}})
    call(port, h, "POST", "/api/chats/send", {"id": c["id"], "message": "again"})
    assert seen[-1][0] == {"role": "system", "content": "pirate"} and len(seen[-1]) == 4
    s, e = call(port, h, "GET", "/api/chats/export?fmt=md")
    assert "again" in e["text"] and "NOT encrypted" in e["warning"]
    call(port, h, "POST", "/api/chats/lock", {})
    s, err = call(port, h, "GET", "/api/chats/list")
    assert s == 400 and "locked" in err["error"]
    assert call(port, h, "POST", "/api/chats/unlock", {"passphrase": "bad passphrase"})[0] == 400
    assert call(port, h, "POST", "/api/chats/unlock", {"passphrase": PW})[0] == 200
    assert len(call(port, h, "GET", "/api/chats/list")[1]["chats"]) == 1
    assert call(port, h, "POST", "/api/chats/clear", {})[0] == 400
    s, r = call(port, h, "POST", "/api/chats/clear", {"confirm": True})
    assert s == 200 and r["deleted_chats"] == 1
    assert os.path.isdir(tmp / "data" / "chats" / "vault")


def test_debian_copy_is_identical_and_cli_reads_passphrase_from_stdin():
    here = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux", "chatvault.py")
    assert open(here).read() == open(os.path.join(DEB_LIB, "chatvault.py")).read()
    cli = open(os.path.join(DEB_LIB, "ctl_ext", "chats.py")).read()
    assert "sys.stdin" in cli and "add_argument(\"--passphrase" not in cli
