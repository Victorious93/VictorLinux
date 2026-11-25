"""Coral Edge TPU: vision / wake-word only, user-supplied models, off by default, no fallback (fake runtime, no hardware)."""
import argparse
import base64
import filecmp
import os
import struct
import sys

import pytest

import cc_ext
import coral

ARCH = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux")
ROOT = os.path.join(ARCH, "..", "..", "..", "..", "..")
DEB_LIB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages", "usr", "local", "lib", "victorlinux")


def tflite(path, edgetpu=True):
    path.write_bytes(struct.pack("<I", 0x1c) + b"TFL3" + b"\0" * 40 + (b"edgetpu-custom-op" if edgetpu else b"CONV_2D") + b"\0" * 16)
    return str(path)


@pytest.fixture
def usb(tmp_path):
    d = tmp_path / "usb" / "1-2"; d.mkdir(parents=True)
    (d / "idVendor").write_text("18d1\n"); (d / "idProduct").write_text("9302\n")
    o = tmp_path / "usb" / "1-3"; o.mkdir()
    (o / "idVendor").write_text("046d\n"); (o / "idProduct").write_text("c52b\n")
    return str(tmp_path / "usb")


def test_shared_module_identical_off_by_default_no_network():
    assert filecmp.cmp(os.path.join(ARCH, "coral.py"), os.path.join(DEB_LIB, "victorlib", "coral.py"), shallow=False)
    assert coral.DEFAULTS["enabled"] is False
    src = open(os.path.join(ARCH, "coral.py")).read() + open(os.path.join(ARCH, "cc_ext", "coral.py")).read()
    for bad in ("urllib", "requests", "http://", "https://", "socket", "pip install"):
        assert bad not in src, bad


def test_detect_and_edgetpu_model_check(tmp_path, usb):
    (tmp_path / "dev").mkdir(); (tmp_path / "dev" / "apex_0").write_text("")
    d = coral.detect(usb, str(tmp_path / "dev"))
    assert [x["kind"] for x in d] == ["usb", "pcie"] and d[0]["id"] == "18d1:9302"
    assert coral.is_edgetpu_model(tflite(tmp_path / "ok_edgetpu.tflite"))
    assert not coral.is_edgetpu_model(tflite(tmp_path / "cpu.tflite", edgetpu=False))     # CPU-only model refused
    (tmp_path / "x.tflite").write_bytes(b"not a model")
    assert not coral.is_edgetpu_model(str(tmp_path / "x.tflite"))


def test_update_only_accepts_listed_edgetpu_models(tmp_path):
    ok, cpu = tflite(tmp_path / "a_edgetpu.tflite"), tflite(tmp_path / "b.tflite", edgetpu=False)
    files = coral.model_files(str(tmp_path))
    assert coral.update(coral.DEFAULTS, {"enabled": True, "vision_model": ok, "wake_threshold": 0.9}, files)["vision_model"] == ok
    for bad in ({"vision_model": cpu}, {"vision_model": "/etc/passwd"}, {"enabled": "yes"}, {"wake_threshold": 2}, {"wake_threshold": "x"}):
        with pytest.raises(coral.CoralError):
            coral.update(coral.DEFAULTS, bad, files)


def test_no_runtime_or_no_delegate_is_an_error_not_a_cpu_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "tflite_runtime", None)
    monkeypatch.setitem(sys.modules, "ai_edge_litert", None)
    with pytest.raises(coral.CoralError, match="nothing is downloaded"):
        coral.runtime()

    def bad_delegate(name):
        raise ValueError("no device")
    with pytest.raises(coral.CoralError, match="not loadable or no Coral"):
        coral.interpreter("m.tflite", rt=(lambda **k: pytest.fail("must not build a CPU interpreter"), bad_delegate))


class FakeInterp:
    def __init__(self, shape, scores, dtype="float32", **kw):
        import numpy as np
        self.shape, self.scores, self.dt, self.kw, self.fed = shape, scores, getattr(np, dtype), kw, []

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 0, "shape": self.shape, "dtype": self.dt, "quantization": (0.0, 0)}]

    def get_output_details(self):
        return [{"index": 1, "quantization": (0.0, 0)}]

    def set_tensor(self, i, x):
        self.fed.append(x)

    def invoke(self):
        pass

    def get_tensor(self, i):
        import numpy as np
        return np.array([self.scores])


def test_status_says_coral_cannot_run_llms(tmp_path, usb, monkeypatch):
    monkeypatch.setitem(sys.modules, "tflite_runtime", None); monkeypatch.setitem(sys.modules, "ai_edge_litert", None)
    st = coral.status(coral.DEFAULTS, [], coral.detect(usb, str(tmp_path)))
    assert "cannot run chat models" in st["llm_note"] and not st["ready"]
    assert any("TensorFlow Lite runtime" in m for m in st["missing"])
    for p in ("docs/coral-edge-tpu.md",):
        assert "cannot run" in open(os.path.join(ARCH, "..", "..", "..", "..", "..", p)).read().lower()


def test_classify_image_top_k(tmp_path):
    pytest.importorskip("numpy"); Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "a.png"; Image.new("RGB", (40, 30), (200, 10, 10)).save(p)
    it = FakeInterp([1, 8, 8, 3], [0.1, 0.7, 0.2], "uint8")
    top = coral.classify_image(it, str(p), {0: "cat", 1: "fox", 2: "dog"}, k=2)
    assert [t["label"] for t in top] == ["fox", "dog"] and it.fed[0].shape == (1, 8, 8, 3)


def test_wake_word_fires_once_then_cools_down():
    np = pytest.importorskip("numpy")
    it = FakeInterp([1, 1000], [0.1, 0.95])
    w = coral.WakeWord(it, {0: "noise", 1: "Victor"}, "victor", threshold=0.8, hop=1.0, cooldown=1)
    pcm = np.zeros(1000, dtype="<i2").tobytes()
    assert w.feed(pcm) is True and w.feed(pcm) is False and w.feed(pcm) is True
    with pytest.raises(coral.CoralError, match="not in the model"):
        coral.WakeWord(it, {0: "noise", 1: "Victor"}, "jarvis")
    with pytest.raises(coral.CoralError, match="raw audio"):
        coral.WakeWord(FakeInterp([1, 49, 40], [0, 1]), {1: "v"}, "v")


@pytest.fixture
def routes(tmp_path, monkeypatch, usb):
    import cc_ext.coral as cc
    monkeypatch.setattr(cc.tc, "data_path", lambda *p: str(tmp_path.joinpath("data", *p)))
    real = coral.detect
    monkeypatch.setattr(cc.coral, "detect", lambda *a: real(usb, str(tmp_path)))
    os.makedirs(tmp_path / "data" / "coral")
    return cc, tflite(tmp_path / "data" / "coral" / "m_edgetpu.tflite")


def test_routes_refuse_when_off_and_without_model(routes):
    cc, model = routes
    R = cc.ROUTES
    assert "off" in R[("POST", "/api/coral/classify")](None, {"image": ""})[1]["error"]
    assert R[("POST", "/api/coral/config")](None, {"enabled": True, "vision_model": model})[0] == 200
    assert R[("POST", "/api/coral/classify")](None, {"image": "%%%"})[0] == 400
    s = R[("GET", "/api/coral/state")](None, {})[1]
    assert s["status"]["devices"][0]["id"] == "18d1:9302" and model in s["status"]["edgetpu_files"]


def test_routes_registered():
    cc_ext.load()
    assert ("POST", "/api/coral/classify") in cc_ext.ROUTES


def test_debian_cli(tmp_path, monkeypatch, usb):
    sys.path.insert(0, DEB_LIB)
    try:
        from victorlib.ctl_ext import coral as dc
        from victorlib.common import VictorError
    finally:
        sys.path.remove(DEB_LIB)
    monkeypatch.setattr(dc, "data_path", lambda *p: str(tmp_path.joinpath("data", *p)))
    os.makedirs(tmp_path / "data" / "coral")
    tflite(tmp_path / "data" / "coral" / "v_edgetpu.tflite")
    with pytest.raises(VictorError, match="off"):
        dc.handle(argparse.Namespace(cmd="classify", image="x.png"))
    n = dc.handle(argparse.Namespace(cmd="set", assign=["enabled=true", "vision_model=v_edgetpu.tflite"]))
    assert n["enabled"] and n["vision_model"].endswith("v_edgetpu.tflite")
    with pytest.raises(VictorError, match="Unknown setting"):
        dc.handle(argparse.Namespace(cmd="set", assign=["shell=rm"]))
    with pytest.raises(VictorError):
        dc.handle(argparse.Namespace(cmd="set", assign=["vision_model=../../etc/passwd"]))
