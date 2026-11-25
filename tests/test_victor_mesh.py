"""Victorious Mesh: validation, tailscale flags, key handling, the app-compatible share server and packaging."""
import base64
import http.client
import importlib.machinery
import importlib.util
import io
import json
import os
import threading
from pathlib import Path

import pytest

import victormesh as vm

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "airootfs/usr/local/lib/victorlinux"
DEB = ROOT / "debian-edition/config/includes.chroot_after_packages"
KEY = "hskey-auth-0123456789abcdefSECRET"


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("url,ok", [
    ("https://headscale.example.com", "https://headscale.example.com"),
    ("https://hs.example.com:8443/", "https://hs.example.com:8443"),
    ("http://192.168.1.10:8080", "http://192.168.1.10:8080"),
    ("http://headscale.lan", "http://headscale.lan"),
])
def test_url_accepted(url, ok):
    assert vm.normalize_url(url) == ok


@pytest.mark.parametrize("url", ["", "headscale.example.com", "ftp://x", "http://headscale.example.com",
                                 "https://user:pw@hs.example.com", "https://hs.example.com/?a=1", "https://hs example.com",
                                 "https://hs.example.com:99999", "https://x/" + "a" * 300])
def test_url_refused(url):
    with pytest.raises(vm.MeshError):
        vm.normalize_url(url)


def test_authkey_validation_never_echoes_the_key():
    assert vm.valid_authkey(" " + KEY + "\n") == KEY
    for bad in ("short", "has space in it 12345", "semi;colon-0123456789", "x" * 300):
        with pytest.raises(vm.MeshError) as e:
            vm.valid_authkey(bad)
        assert bad not in str(e.value)


def test_hostname_and_tags(tmp_path, monkeypatch):
    assert vm.valid_hostname("My-PC") == "my-pc" and vm.valid_hostname("") == ""
    with pytest.raises(vm.MeshError):
        vm.valid_hostname("bad_name!")
    monkeypatch.delenv("VICTOR_MESH_EDITION", raising=False)
    osr = tmp_path / "os-release"
    osr.write_text('ID=victorlinux\nVARIANT_ID=pi\n')
    assert vm.tag_for(vm.edition(str(osr))) == "tag:victorlinux-pi"
    osr.write_text('ID=victorlinux\n')
    assert vm.tag_for(vm.edition(str(osr))) == "tag:victorlinux"
    assert vm.edition(str(tmp_path / "missing")) == "pc"


def test_mesh_ip_ranges():
    assert vm.is_mesh_ip("100.64.0.9") and vm.is_mesh_ip("100.127.255.1") and vm.is_mesh_ip("fd7a:115c:a1e0::5")
    for ip in ("0.0.0.0", "192.168.1.5", "100.128.0.1", "127.0.0.1", "::", "nope"):
        assert not vm.is_mesh_ip(ip)


# ------------------------------------------------------------------ tailscale flags

def test_up_args_flags_and_keyfile():
    s = dict(vm.DEFAULTS, control_url="https://hs.example.com", tag="tag:victorlinux", hostname="pc1")
    a = vm.up_args(s, "/run/victor/.mesh-key-x")
    assert a[:2] == ["up", "--reset"]
    for f in ("--login-server=https://hs.example.com", "--advertise-tags=tag:victorlinux", "--operator=victor",
              "--accept-dns=false", "--accept-routes=false", "--ssh=false", "--hostname=pc1",
              "--auth-key=file:/run/victor/.mesh-key-x"):
        assert f in a
    assert "--advertise-exit-node" not in a
    assert "--advertise-exit-node" in vm.up_args(dict(s, exit_node=True))
    assert not any(x.startswith("--auth-key") for x in vm.up_args(s))
    with pytest.raises(vm.MeshError):
        vm.up_args(dict(vm.DEFAULTS))


def test_dropins():
    d = vm.tailscaled_dropin("/data/secrets/mesh/tailscaled.state", "/usr/sbin/tailscaled")
    assert "ExecStart=\n" in d and "--state=/data/secrets/mesh/tailscaled.state" in d
    ssh = vm.sshd_dropin("100.64.0.7")
    assert "ListenAddress 100.64.0.7" in ssh and "PasswordAuthentication no" in ssh and "AuthenticationMethods publickey" in ssh


def test_magic_packet():
    pkt = vm.magic_packet(vm.valid_mac("AA:bb:cc:dd:ee:01"))
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6 and pkt[6:12] == bytes.fromhex("aabbccddee01")
    with pytest.raises(vm.MeshError):
        vm.valid_mac("aa:bb:cc")


# ------------------------------------------------------------------ root helper join (no root, everything faked)

def load_priv():
    loader = importlib.machinery.SourceFileLoader("victor_mesh_priv", str(LIB / "victor-mesh-priv"))
    spec = importlib.util.spec_from_loader("victor_mesh_priv", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def priv(tmp_path, monkeypatch):
    mod = load_priv()
    monkeypatch.setenv("VICTOR_MESH_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VICTOR_MESH_EDITION", "pi")
    monkeypatch.setattr(vm, "RUN", str(tmp_path / "run"))
    monkeypatch.setattr(vm, "TS_DROPIN_DIR", str(tmp_path / "dropin"))
    monkeypatch.setattr(vm, "SSH_DROPIN", str(tmp_path / "sshd/15-victor-mesh.conf"))
    monkeypatch.setattr(mod, "ENV_FILE", str(tmp_path / "victor-mesh.env"))
    monkeypatch.setattr(mod, "owner_ids", lambda: (os.getuid(), os.getgid(), str(tmp_path / "home")))
    monkeypatch.setattr(mod.shutil, "which", lambda n: "/usr/bin/" + n)
    calls, seen_keyfile = [], {}

    def fake_run(cmd, timeout=30, **kw):
        calls.append(list(cmd))
        out = ""
        if cmd[:2] == ["tailscale", "up"]:
            kf = next(x.split("file:", 1)[1] for x in cmd if x.startswith("--auth-key=file:"))
            seen_keyfile["path"] = kf
            seen_keyfile["content"] = open(kf).read()
            seen_keyfile["mode"] = oct(os.stat(kf).st_mode & 0o777)
        if cmd[:3] == ["tailscale", "status", "--json"]:
            out = json.dumps({"BackendState": "Running", "TailscaleIPs": ["100.64.0.20", "fd7a:115c:a1e0::20"]})
        return vm.subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(vm, "run", fake_run)
    return mod, calls, seen_keyfile, tmp_path


def test_join_passes_key_once_by_file_and_never_stores_it(priv, monkeypatch, capsys):
    mod, calls, kf, tmp = priv
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    mod.cmd_join(["https://hs.example.com/", "pi-node"])
    res = json.loads(capsys.readouterr().out)
    assert res["tag"] == "tag:victorlinux-pi" and res["mesh_ips"][0] == "100.64.0.20" and res["hostname"] == "pi-node"
    assert kf["content"] == KEY and kf["mode"] == "0o600" and not os.path.exists(kf["path"])
    assert not any(KEY in " ".join(c) for c in calls)                       # never on a command line
    for p in tmp.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(errors="ignore"), p                # never in settings, env or drop-ins
    s = json.loads((tmp / "state/settings.json").read_text())
    assert s["control_url"] == "https://hs.example.com" and not any(s[t] for t in vm.TOGGLES)
    assert ["systemctl", "start", "tailscaled.service"] in calls
    assert not any(c[:2] == ["systemctl", "enable"] for c in calls)
    assert "--state=" + str(tmp / "state/tailscaled.state") in (tmp / "dropin/victor.conf").read_text()


def test_join_refuses_bad_key_before_touching_anything(priv, monkeypatch):
    mod, calls, _, _ = priv
    monkeypatch.setattr("sys.stdin", io.StringIO("bad key\n"))
    with pytest.raises(SystemExit):
        mod.cmd_join(["https://hs.example.com"])
    assert calls == []


def test_share_toggle_makes_password_opens_mesh_port_only(priv, monkeypatch, capsys):
    mod, calls, _, tmp = priv
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    mod.cmd_join(["https://hs.example.com"]); capsys.readouterr()
    calls.clear()
    real_run = vm.run
    monkeypatch.setattr(vm, "run", lambda cmd, timeout=30, **kw: vm.subprocess.CompletedProcess(cmd, 0, "active\n", "")
                        if cmd[:2] == ["systemctl", "is-active"] else real_run(cmd, timeout, **kw))
    mod.cmd_set(["share", "on"])
    out = json.loads(capsys.readouterr().out)
    assert vm.valid_password(out["password"])
    rec = json.loads((tmp / "state/share-auth.json").read_text())
    assert out["password"] not in json.dumps(rec) and vm.check_password(out["password"], rec)
    assert ["nft", "add", "element", "inet", "victor", "mesh_tcp", "{ 8090 }"] in calls
    assert "VICTOR_MESH_BIND=100.64.0.20" in (tmp / "victor-mesh.env").read_text()
    with pytest.raises(SystemExit):
        mod.cmd_set(["share", "maybe"])


def test_mesh_ssh_needs_a_key_and_binds_to_mesh_ip(priv, monkeypatch, capsys):
    mod, calls, _, tmp = priv
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    mod.cmd_join(["https://hs.example.com"]); capsys.readouterr()
    s = dict(vm.load_settings(), ssh=True)
    notes = mod.apply_extras(s, vm.paths())
    assert any("add an SSH public key" in n for n in notes) and not os.path.exists(vm.SSH_DROPIN)
    (tmp / "home/.ssh").mkdir(parents=True)
    (tmp / "home/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA test\n")
    calls.clear()
    mod.apply_extras(dict(s, ssh=True), vm.paths())
    assert "ListenAddress 100.64.0.20" in open(vm.SSH_DROPIN).read()
    assert ["nft", "add", "element", "inet", "victor", "mesh_tcp", "{ 22 }"] in calls


# ------------------------------------------------------------------ share server

@pytest.fixture
def share(tmp_path):
    d = tmp_path / "share"; d.mkdir()
    (d / "notes.txt").write_text("hello mesh")
    (d / "big.bin").write_bytes(b"0123456789" * 10)
    (d / ".hidden").write_text("secret")
    (tmp_path / "outside.txt").write_text("outside")
    os.symlink(tmp_path / "outside.txt", d / "link.txt")
    rec = vm.hash_password("correct-horse-battery-1")
    srv = vm.make_server("127.0.0.1", 0, str(d), rec, require_mesh=False)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield srv.server_address[1], d
    srv.shutdown(); srv.server_close()


def get(port, path, method="GET", auth=None, headers=None):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=h)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_share_requires_password_any_username(share):
    port, _ = share
    s, h, _ = get(port, "/")
    assert s == 401 and "Basic" in h["WWW-Authenticate"]
    assert get(port, "/", auth="mesh:wrong-password-xx")[0] == 401
    for user in ("mesh", "anything", ""):
        s, h, body = get(port, "/", auth=f"{user}:correct-horse-battery-1")
        assert s == 200 and b"notes.txt" in body
    assert b".hidden" not in body and b"link.txt" not in body and b"#0B0F1A" in body


def test_share_download_range_and_read_only(share):
    port, _ = share
    a = "x:correct-horse-battery-1"
    s, h, body = get(port, "/f/notes.txt", auth=a)
    assert s == 200 and body == b"hello mesh" and h["Content-Type"] == "application/octet-stream"
    assert "attachment" in h["Content-Disposition"] and h["X-Content-Type-Options"] == "nosniff"
    s, h, body = get(port, "/f/big.bin", auth=a, headers={"Range": "bytes=10-19"})
    assert s == 206 and body == b"0123456789" and h["Content-Range"] == "bytes 10-19/100"
    for m in ("POST", "PUT", "DELETE"):
        assert get(port, "/f/notes.txt", method=m, auth=a)[0] == 405
    for bad in ("/f/.hidden", "/f/link.txt", "/f/..%2Foutside.txt", "/f/../outside.txt", "/f/nope", "/etc/passwd"):
        assert get(port, bad, auth=a)[0] == 404, bad


def test_share_lockout_after_wrong_passwords(share):
    port, _ = share
    for _ in range(10):
        get(port, "/", auth="m:wrong-password-xx")
    assert get(port, "/", auth="m:correct-horse-battery-1")[0] == 429


def test_share_refuses_non_mesh_bind(tmp_path):
    for ip in ("0.0.0.0", "192.168.1.5", "127.0.0.1"):
        with pytest.raises(vm.MeshError):
            vm.make_server(ip, 0, str(tmp_path), vm.hash_password("x" * 20))


def test_share_cap_names_and_add_remove(tmp_path):
    d = tmp_path / "share"
    src = tmp_path / "report.pdf"; src.write_bytes(b"a" * 100)
    assert vm.share_add(str(d), str(src)) == "report.pdf"
    assert vm.share_add(str(d), str(src)) == "report (2).pdf"
    with pytest.raises(vm.MeshError):
        vm.share_add(str(d), str(src), cap=250)
    assert [f["name"] for f in vm.list_share(str(d), cap=150)] == ["report (2).pdf"]
    vm.share_remove(str(d), "report.pdf")
    with pytest.raises(vm.MeshError):
        vm.share_remove(str(d), "../report.pdf")
    assert vm.safe_name("C:\\\\x\\\\.ssh") is None and vm.safe_name("a/b/c.txt") == "c.txt" and vm.safe_name("..") is None
    assert vm.MAX_SHARE_BYTES == 2 * 1024 ** 3 and vm.SHARE_PORT == 8090


def test_password_hash_round_trip():
    p = vm.new_password()
    assert vm.valid_password(p) and len(p) >= 24
    rec = vm.hash_password(p)
    assert vm.check_password(p, rec) and not vm.check_password(p + "x", rec) and p not in json.dumps(rec)


# ------------------------------------------------------------------ packaging and both Control Centers

def test_copies_identical_between_editions():
    for a, b in (("airootfs/usr/local/lib/victorlinux/victormesh.py", DEB / "usr/local/lib/victorlinux/victormesh.py"),
                 ("airootfs/usr/local/lib/victorlinux/victor-mesh-priv", DEB / "usr/local/lib/victorlinux/victor-mesh-priv"),
                 ("airootfs/usr/local/bin/victor-mesh", DEB / "usr/local/bin/victor-mesh"),
                 ("airootfs/etc/systemd/system/victor-mesh-share.service", DEB / "etc/systemd/system/victor-mesh-share.service"),
                 ("airootfs/etc/systemd/system/victor-mesh-taildrop.service", DEB / "etc/systemd/system/victor-mesh-taildrop.service")):
        assert (ROOT / a).read_text() == Path(b).read_text(), a


def test_off_by_default_everywhere():
    for unit in ("victor-mesh-share.service", "victor-mesh-taildrop.service"):
        t = (ROOT / "airootfs/etc/systemd/system" / unit).read_text()
        assert "[Install]" not in t and "User=victor" in t
    assert "systemctl disable tailscaled.service" in (ROOT / "airootfs/root/customize_airootfs.sh.jinja2").read_text()
    hook = (ROOT / "debian-edition/config/hooks/live/0260-optional-tailscale.hook.chroot").read_text()
    assert "systemctl disable tailscaled.service" in hook and "TAILSCALE_KEYRING_SHA256" in hook and 'rm -f "$list" "$key"' in hook
    assert "TAILSCALE_KEYRING_SHA256=" in (ROOT / "debian-edition/versions.env").read_text()
    assert "0260-optional-tailscale.hook.chroot" in (ROOT / "pi-edition/build.sh").read_text()
    assert "\ntailscale\n" in (ROOT / "packages.x86_64.jinja2").read_text()


def test_firewall_and_sudoers():
    nft = (DEB / "etc/nftables.conf").read_text()
    assert "set mesh_tcp" in nft and 'iifname "tailscale0" tcp dport @mesh_tcp accept' in nft
    assert "victor-mesh-priv" in (ROOT / "airootfs/etc/sudoers.d/10-victor").read_text()
    assert "victor-mesh-priv" in (DEB / "etc/sudoers.d/victor-priv").read_text()


def test_control_centers_have_join_mesh():
    html = (LIB / "web/index.html").read_text()
    for t in ('id="t-mesh"', 'id="meshUrl"', 'id="meshKey" type="password"', "Join mesh", 'data-k="exit-node"', 'data-k="wol"',
              'data-k="taildrop"', 'data-k="share"', 'data-k="ssh"'):
        assert t in html
    assert '"/api/mesh"' in (LIB / "control_center.py").read_text()
    tk = (DEB / "usr/local/bin/victor-control").read_text()
    assert "self.tab_mesh()" in tk and "def tab_mesh" in tk and '"--key-stdin"' in tk and 'show="•"' in tk.replace("show=show", 'show="•"')
    assert (ROOT / "docs/victorious-mesh.md").read_text().count("tag:victorlinux-pi:22,8080,8090") == 1


def test_control_center_mesh_api(tmp_path, monkeypatch):
    import control_center as cc
    monkeypatch.setenv("VICTOR_MESH_DIR", str(tmp_path / "mesh"))
    app = cc.App.__new__(cc.App)
    code, st = app.mesh_action({"action": "status"})
    assert code == 200 and st["joined"] is False and not any(st["toggles"].values())
    code, r = app.mesh_action({"action": "join", "url": "https://hs.example.com", "key": "bad"})
    assert code == 400 and "bad" not in r["error"]
    assert app.mesh_action({"action": "set", "toggle": "x", "on": True})[0] == 400
    seen = {}
    monkeypatch.setattr(app, "mesh_priv", lambda args, stdin=None, timeout=240: (seen.update(args=args, stdin=stdin), (1, "boom " + KEY))[1], raising=False)
    code, r = app.mesh_action({"action": "join", "url": "https://hs.example.com", "key": KEY})
    assert seen["args"] == ["join", "https://hs.example.com"] and seen["stdin"] == KEY + "\n"
    assert code == 400 and KEY not in r["error"] and "***" in r["error"]
