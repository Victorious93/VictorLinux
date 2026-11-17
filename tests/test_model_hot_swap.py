"""Model hot swap: the gateway holds API requests during a switch; the Control Center switches or rolls back; Debian parity."""
import http.server
import json
import os
import sys
import threading
import time

import pytest

import cc_ext
import control_center as cc
import gateway
from test_control_api import request, serving

DEB_LIB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages",
                       "usr", "local", "lib", "victorlinux")


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self, body):
        b = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._ok({"status": "ok"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._ok({"choices": [{"message": {"content": "from the new model"}}]})


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway.tc, "RUN_DIR", str(tmp_path))
    with serving(Upstream) as up:
        monkeypatch.setattr(gateway.Handler, "cfg", {"require_auth": False, "keys": [], "upstream_port": up,
                                                     "upstream_key": "private", "max_body": 4096})
        with serving(gateway.Handler) as port:
            yield port, tmp_path / "swap.json"


def test_requests_are_held_during_switch_then_answered(gw):
    port, marker = gw
    marker.write_text(json.dumps({"state": "switching", "to": "new.gguf", "since": time.time()}))
    s, d = request(port, "GET", "/victor/health")
    assert s == 503 and json.loads(d)["status"] == "switching_model"
    threading.Timer(0.6, marker.unlink).start()
    t0 = time.time()
    s, d = request(port, "POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
    assert s == 200 and b"from the new model" in d and time.time() - t0 >= 0.5


def test_switch_that_takes_too_long_gets_retry_after(gw, monkeypatch):
    port, marker = gw
    monkeypatch.setattr(gateway, "SWAP_WAIT_S", 0.3)
    marker.write_text(json.dumps({"state": "switching", "to": "big.gguf", "since": time.time()}))
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/v1/chat/completions", body=b"{}", headers={"Content-Type": "application/json"})
    r = c.getresponse()
    body = json.loads(r.read())
    assert r.status == 503 and r.getheader("Retry-After") == "10" and body["error"]["type"] == "model_switching"
    assert "big.gguf" in body["error"]["message"]


def test_stale_marker_is_ignored(gw):
    port, marker = gw
    marker.write_text(json.dumps({"state": "switching", "since": time.time() - 3600}))
    assert request(port, "POST", "/v1/chat/completions", {"messages": []})[0] == 200


# ------------------------------------------------------------------ Control Center switch logic (fake runtime) --
class FakeProc:
    def __init__(self):
        self.up = True

    def running(self):
        return self.up


class FakeApp:
    """Records what the switch does. `good` = model paths that load and answer."""
    def __init__(self, good):
        self.lock = threading.Lock()
        self.llm, self.gw = FakeProc(), FakeProc()
        self.good, self.calls, self.llm_info, self.llm_verify = set(good), [], {}, {}

    def stop(self, proc):
        self.calls.append(("stop", "llm" if proc is self.llm else "gw"))
        proc.up = False

    def start_llm(self, settings, force=False, keep_gateway=False):
        self.calls.append(("start", settings["model"], keep_gateway))
        self.llm.up = True
        self.llm_verify = {"state": "verified" if settings["model"] in self.good else "failed", "error": "bad file"}
        return {"ok": True}


@pytest.fixture
def swap(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    import cc_ext.swap as sw
    monkeypatch.setattr(sw.tc, "RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(sw.tc, "list_models", lambda extra=(): [{"path": p, "name": os.path.basename(p)} for p in ("/m/a.gguf", "/m/b.gguf", "/m/broken.gguf")])
    s = sw.tc.load_settings(); s["model"] = "/m/a.gguf"; sw.tc.save_settings(s)
    sw.SWAP.clear(); sw.SWAP["state"] = "idle"
    return sw


def _wait(sw):
    for _ in range(100):
        if sw.SWAP["state"] != "switching":
            return
        time.sleep(0.05)
    raise AssertionError("switch never finished")


def test_switch_keeps_gateway_and_saves_on_success(swap, tmp_path):
    app = FakeApp({"/m/a.gguf", "/m/b.gguf"})
    st, body = swap.switch(app, {"model": "/m/b.gguf"})
    assert st == 200 and body["started"]
    _wait(swap)
    assert swap.SWAP["state"] == "done"
    assert ("stop", "gw") not in app.calls and ("start", "/m/b.gguf", True) in app.calls
    assert swap.tc.load_settings()["model"] == "/m/b.gguf"
    assert not (tmp_path / "run" / "swap.json").exists()


def test_failed_switch_rolls_back_and_keeps_old_choice(swap, tmp_path):
    app = FakeApp({"/m/a.gguf"})
    swap.switch(app, {"model": "/m/broken.gguf"})
    _wait(swap)
    assert swap.SWAP["state"] == "error" and swap.SWAP["rolled_back"] is True
    assert app.calls[-1] == ("start", "/m/a.gguf", True)
    assert swap.tc.load_settings()["model"] == "/m/a.gguf"
    assert not (tmp_path / "run" / "swap.json").exists()


def test_switch_rejects_unknown_paths_and_just_selects_when_idle(swap):
    app = FakeApp({"/m/a.gguf", "/m/b.gguf"})
    assert swap.switch(app, {"model": "/etc/passwd"})[0] == 400
    app.llm.up = False
    st, body = swap.switch(app, {"model": "/m/b.gguf"})
    assert st == 200 and not body["started"] and app.calls == []
    assert swap.tc.load_settings()["model"] == "/m/b.gguf"


def test_switch_route_is_registered():
    cc_ext.load()
    assert ("POST", "/api/llm/switch") in cc_ext.ROUTES and ("GET", "/api/llm/switch") in cc_ext.ROUTES


def test_start_llm_accepts_keep_gateway():
    import inspect
    assert "keep_gateway" in inspect.signature(cc.App.start_llm).parameters


# ------------------------------------------------------------------------------------ Debian victor-ctl switch --
@pytest.fixture
def deb(tmp_path, monkeypatch):
    sys.path.insert(0, DEB_LIB)
    try:
        from victorlib import llama as dl
        from victorlib.ctl_ext import switch as ds
        from victorlib.common import VictorError
    finally:
        sys.path.remove(DEB_LIB)
    models = tmp_path / "models"; models.mkdir()
    for n in ("a.gguf", "b.gguf", "broken.gguf"):
        (models / n).write_bytes(b"GGUF")
    cfg = {"model": "a.gguf"}
    state = {"running": True, "log": []}
    monkeypatch.setattr(ds, "data_path", lambda *p: str(tmp_path.joinpath(*p)))
    monkeypatch.setattr(dl, "load_config", lambda: dict(cfg, remote={"enabled": False, "tls": False}, bind="127.0.0.1", port=1))
    monkeypatch.setattr(dl, "save_config", lambda c: (cfg.update(model=c["model"]), state["log"].append(("save", c["model"]))))
    monkeypatch.setattr(dl, "is_running", lambda: state["running"])
    monkeypatch.setattr(dl, "stop", lambda: (state.update(running=False), state["log"].append(("stop",))))
    monkeypatch.setattr(dl, "start", lambda: (state.update(running=True), state["log"].append(("start", cfg["model"]))))
    monkeypatch.setattr(ds, "_healthy", lambda c, w: c["model"] != "broken.gguf")
    return ds, cfg, state, VictorError


def _args(model):
    import argparse
    return argparse.Namespace(model=model, wait=1)


def test_debian_switch_success(deb):
    ds, cfg, state, _ = deb
    r = ds.handle(_args("b.gguf"))
    assert r["switched"] and cfg["model"] == "b.gguf" and ("start", "b.gguf") in state["log"]


def test_debian_switch_rolls_back(deb):
    ds, cfg, state, VictorError = deb
    with pytest.raises(VictorError, match="previous model is running again"):
        ds.handle(_args("broken.gguf"))
    assert cfg["model"] == "a.gguf" and state["log"][-1] == ("start", "a.gguf")


def test_debian_switch_rejects_escape(deb):
    ds, _, _, VictorError = deb
    for bad in ("../etc/passwd", "/etc/passwd", "missing.gguf"):
        with pytest.raises(VictorError):
            ds.handle(_args(bad))
