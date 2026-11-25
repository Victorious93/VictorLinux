"""Benchmark auto-tune: trial grid, llama-bench parsing, winner choice, per-PC profiles, start-time overrides, API, Debian."""
import json
import os
import sys
import time
import types

import pytest

import autotune
import cc_ext
import control_center as cc
from test_control_api import NO_BODY, request, serving

DEB_LIB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages",
                       "usr", "local", "lib", "victorlinux")
ROWS = [  # llama-bench -o json shape (fields used here only)
    {"n_gpu_layers": 33, "n_threads": 8, "n_prompt": 256, "n_gen": 0, "avg_ts": 900.5},
    {"n_gpu_layers": 33, "n_threads": 8, "n_prompt": 0, "n_gen": 64, "avg_ts": 61.2},
    {"n_gpu_layers": 25, "n_threads": 8, "n_prompt": 256, "n_gen": 0, "avg_ts": 500.0},
    {"n_gpu_layers": 25, "n_threads": 8, "n_prompt": 0, "n_gen": 64, "avg_ts": 30.1},
    {"n_gpu_layers": 33, "n_threads": 16, "n_prompt": 256, "n_gen": 0, "avg_ts": 910.0},
    {"n_gpu_layers": 33, "n_threads": 16, "n_prompt": 0, "n_gen": 64, "avg_ts": 58.0},
]


def test_grid_bounds():
    assert autotune.layer_options(32, 999, True) == [25, 33]
    assert autotune.layer_options(32, 20, True) == [12, 20]
    assert autotune.layer_options(32, 20, False) == [0]
    assert autotune.thread_options(8, 16) == [4, 8, 16]
    assert len(autotune.thread_options(1, 1)) == 1
    cmd = autotune.bench_command("/x/llama-bench", "/m.gguf", [25, 33], [4, 8])
    assert cmd[cmd.index("-ngl") + 1] == "25,33" and cmd[cmd.index("-t") + 1] == "4,8" and "-o" in cmd


def test_parse_and_pick_fastest_generation():
    r = autotune.parse_bench(json.dumps(ROWS))
    assert r[(33, 8)] == {"pp": 900.5, "tg": 61.2}
    assert autotune.pick(r) == {"ngl": 33, "threads": 8, "tg": 61.2, "pp": 900.5}
    with pytest.raises(ValueError):
        autotune.pick({})
    with pytest.raises(ValueError):
        autotune.pick({(0, 4): {"pp": 10, "tg": 0}})


def test_profiles_are_per_machine_and_model(tmp_path):
    p = autotune.Profiles(str(tmp_path / "a.json"))
    a, b = autotune.machine_id("Ryzen 7", ["1002:73bf"], 32 * 2**30), autotune.machine_id("i5", [], 16 * 2**30)
    assert a != b and a == autotune.machine_id("Ryzen 7", ["1002:73bf"], 32 * 2**30 + 12345)
    p.put(a, "m.gguf:1", {"ngl": 33, "threads": 8, "ctx": 8192})
    assert p.get(a, "m.gguf:1")["threads"] == 8 and p.get(b, "m.gguf:1") is None
    assert oct(os.stat(tmp_path / "a.json").st_mode & 0o777) == "0o600"
    assert p.remove(a) == 1 and p.for_machine(a) == {}


def test_apply_only_where_automatic_and_never_raises_context():
    prof = {"ngl": 20, "threads": 6, "ctx": 16384}
    assert autotune.apply(prof, -1, 0, True, 8192) == {"ngl": 20, "threads": 6, "ctx": 8192}
    assert autotune.apply(prof, -1, 0, True, 32768)["ctx"] == 16384
    assert autotune.apply(prof, 10, 4, False, 8192) == {}
    assert autotune.apply(None, -1, 0, True, 8192) == {}


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cache" / "cc.token"))
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cc, "APP", cc.App(), raising=False)
    cc_ext.load()
    tune = cc_ext.MODULES["tune"]
    monkeypatch.setattr(tune, "JOB", {"state": "idle"})
    model = tmp_path / "data" / "models" / "m.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUF" + b"\0" * 64)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for n in ("llama-server", "llama-bench"):
        (bindir / n).write_text("#!/bin/sh\n"); (bindir / n).chmod(0o755)
    plan = {"backend": "vulkan", "model": str(model), "estimate": {"n_layer": 32}, "ngl": 999, "ctx": 8192,
            "device": {"name": "RX 6800 XT"}, "binary": str(bindir / "llama-server")}
    monkeypatch.setattr(cc.APP, "plan_llm", lambda s: dict(plan))
    monkeypatch.setattr(cc.APP, "machine_id", lambda *a: "pc-one")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(ROWS), stderr="")
    monkeypatch.setattr(tune, "RUN", fake_run)
    cc.tc.save_settings(dict(cc.tc.load_settings(), model=str(model)))
    with serving(cc.Handler) as port:
        yield port, {"X-Victor-Token": cc.APP.token}, calls, str(model)
    cc_ext.MODULES.clear(); cc_ext.ROUTES.clear()


def j(port, h, method, path, body=NO_BODY):
    s, d = request(port, method, path, body, h)
    return s, json.loads(d)


def test_api_run_saves_profile_and_start_uses_it(control, monkeypatch):
    port, h, calls, model = control
    monkeypatch.setattr(cc.APP.llm, "running", lambda: True)
    s, e = j(port, h, "POST", "/api/tune/run", {})
    assert s == 400 and "Stop the model" in e["error"]
    monkeypatch.setattr(cc.APP.llm, "running", lambda: False)
    s, r = j(port, h, "POST", "/api/tune/run", {})
    assert s == 200, r
    for _ in range(50):
        st = j(port, h, "GET", "/api/tune/status")[1]
        if st["job"]["state"] != "running":
            break
        time.sleep(0.05)
    assert st["job"]["state"] == "done" and st["current"]["ngl"] == 33 and st["current"]["threads"] == 8
    assert st["current"]["ctx"] == 8192 and len(st["current"]["trials"]) == 3
    assert calls[0][calls[0].index("-ngl") + 1] == "25,33"
    s0 = cc.tc.load_settings()
    assert cc.APP.autotune_overrides(s0, model, "vulkan", [], {}, 4096) == {"ngl": 33, "threads": 8, "ctx": 4096}
    assert cc.APP.autotune_overrides(s0, model, "cpu", [], {}, 4096) == {}            # profile from another backend
    assert cc.APP.autotune_overrides(dict(s0, threads=4, gpu_layers=10, ctx_auto=False), model, "vulkan", [], {}, 4096) == {}
    j(port, h, "POST", "/api/tune/apply", {"enabled": False})
    assert cc.APP.autotune_overrides(cc.tc.load_settings(), model, "vulkan", [], {}, 4096) == {}
    s, r = j(port, h, "POST", "/api/tune/forget", {})
    assert "Removed 1" in r["message"]


def test_debian_copy_identical_and_threads_hook(tmp_path, monkeypatch):
    here = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux", "autotune.py")
    assert open(here).read() == open(os.path.join(DEB_LIB, "victorlib", "autotune.py")).read()
    sys.path.insert(0, DEB_LIB)
    try:
        from victorlib import llama, hw
        from victorlib import autotune as dat
        monkeypatch.setattr(llama, "settings_file", lambda n: str(tmp_path / n))
        monkeypatch.setattr(hw, "machine_id", lambda: "pc-deb")
        m = tmp_path / "x.gguf"; m.write_bytes(b"x" * 10)
        dat.Profiles(str(tmp_path / "autotune.json")).put("pc-deb", dat.model_key(str(m)), {"threads": 6, "accelerated": True, "ngl": 999})
        gpu, cpu = {"accelerated": True}, {"accelerated": False}
        assert llama.tuned_threads({"threads": 0}, gpu, str(m)) == 6
        assert llama.tuned_threads({"threads": 4}, gpu, str(m)) is None
        assert llama.tuned_threads({"threads": 0}, cpu, str(m)) is None
        assert llama.tuned_threads({"threads": 0, "autotune_apply": False}, gpu, str(m)) is None
    finally:
        sys.path.remove(DEB_LIB)
