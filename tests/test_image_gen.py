"""Image generation: off by default, validation, stable-diffusion.cpp invocation (fake program), routes, Debian parity."""
import argparse
import base64
import filecmp
import json
import os
import sys
import time

import pytest

import cc_ext
import imagegen

ARCH = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux")
DEB_LIB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages",
                       "usr", "local", "lib", "victorlinux")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def sd(tmp_path):
    log = tmp_path / "argv.log"
    b = tmp_path / "sd-cli"
    b.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                 'echo "loading model"\necho "  1/4 - 0.5s/it"\necho "  4/4 - 0.5s/it"\n'
                 'printf "\\211PNG\\r\\n\\032\\n0000" > "$out"\n')
    b.chmod(0o755)
    mdir = tmp_path / "data" / "images" / "models"; mdir.mkdir(parents=True)
    (mdir / "sd-v1-5-Q8_0.gguf").write_bytes(b"x"); (mdir / "readme.txt").write_text("ignored")
    cfg = dict(imagegen.DEFAULTS, enabled=True, bin=str(b), model=str(mdir / "sd-v1-5-Q8_0.gguf"))
    return cfg, log, mdir


def test_shared_module_identical_and_off_by_default():
    assert filecmp.cmp(os.path.join(ARCH, "imagegen.py"), os.path.join(DEB_LIB, "victorlib", "imagegen.py"), shallow=False)
    assert imagegen.DEFAULTS["enabled"] is False


def test_no_downloads_or_network_in_the_code():
    src = open(os.path.join(ARCH, "imagegen.py")).read() + open(os.path.join(ARCH, "cc_ext", "imagegen.py")).read()
    for bad in ("urllib", "requests", "http://", "https://", "socket", "huggingface"):
        assert bad not in src, bad


def test_job_validation():
    assert imagegen.validate_job({"prompt": "  a  neon   fox "})["prompt"] == "a neon fox"
    for bad in ({"prompt": ""}, {"prompt": "x" * 1001}, {"prompt": "a", "width": 500}, {"prompt": "a", "height": 2048},
                {"prompt": "a", "steps": 0}, {"prompt": "a", "steps": True}, {"prompt": "a", "cfg": 50},
                {"prompt": "a", "cfg": "lots"}, {"prompt": "a", "seed": -5}, {"prompt": "a", "sampler": "rm -rf"}):
        with pytest.raises(imagegen.ImageGenError):
            imagegen.validate_job(bad)


def test_settings_only_accept_listed_files(sd):
    cfg, _log, mdir = sd
    files = imagegen.model_files(str(mdir))
    assert [os.path.basename(f) for f in files] == ["sd-v1-5-Q8_0.gguf"]
    for bad in ({"model": "/etc/shadow"}, {"enabled": "yes"}, {"bin": "sd"}, {"bin": "/etc/passwd"}, {"threads": 999}):
        with pytest.raises(imagegen.ImageGenError):
            imagegen.update(cfg, bad, files)


def test_generate_runs_the_local_program(sd, tmp_path):
    cfg, log, _ = sd
    seen = []
    out = imagegen.generate(cfg, {"prompt": "a neon fox", "negative": "blurry", "steps": 4, "seed": 42}, str(tmp_path / "out"),
                            on_progress=lambda s, n: seen.append((s, n)))
    assert out.endswith(".png") and open(out, "rb").read(4) == b"\x89PNG"
    argv = log.read_text().split("\n")
    assert argv[argv.index("-p") + 1] == "a neon fox" and argv[argv.index("-n") + 1] == "blurry"
    assert argv[argv.index("--steps") + 1] == "4" and argv[argv.index("-s") + 1] == "42"
    assert seen[-1] == (4, 4)
    with pytest.raises(imagegen.ImageGenError, match="off"):
        imagegen.generate(dict(cfg, enabled=False), {"prompt": "x"}, str(tmp_path / "out"))
    st = imagegen.status(dict(cfg, bin="/nonexistent/sd"), [])
    assert not st["ready"] and any("stable-diffusion.cpp" in m for m in st["missing"])


def test_failure_is_reported(sd, tmp_path):
    cfg, _, _ = sd
    bad = tmp_path / "sd-bad"; bad.write_text('#!/bin/sh\necho "error: model file is not a diffusion model"\nexit 1\n'); bad.chmod(0o755)
    with pytest.raises(imagegen.ImageGenError, match="not a diffusion model"):
        imagegen.generate(dict(cfg, bin=str(bad)), {"prompt": "x"}, str(tmp_path / "out"))


@pytest.fixture
def routes(sd, tmp_path, monkeypatch):
    import cc_ext.imagegen as ci
    monkeypatch.setattr(ci.tc, "data_path", lambda *p: str(tmp_path.joinpath("data", *p)))
    ci.JOB.update(state="idle", step=0, steps=0, error="", file="", proc=None)
    return ci, sd[0]


def test_routes_refuse_when_off_then_draw(routes):
    ci, cfg = routes
    R = ci.ROUTES
    st, body = R[("POST", "/api/imagegen/generate")](None, {"prompt": "a fox"})
    assert st == 400 and "off" in body["error"]
    st, body = R[("POST", "/api/imagegen/config")](None, {"enabled": True, "model": cfg["model"], "bin": cfg["bin"]})
    assert st == 200, body
    st, body = R[("POST", "/api/imagegen/generate")](None, {"prompt": "a fox", "steps": 4})
    assert st == 200 and body["job"]["state"] in ("running", "done")   # the fake program can finish instantly
    for _ in range(100):
        j = R[("GET", "/api/imagegen/job")](None, {})[1]
        if j["state"] != "running":
            break
        time.sleep(0.05)
    assert j["state"] == "done", j
    assert base64.b64decode(j["png"]).startswith(b"\x89PNG") and j["file"].startswith("img-")
    assert R[("POST", "/api/imagegen/cancel")](None, {})[1]["message"] == "Nothing is running."
    s = R[("GET", "/api/imagegen/state")](None, {})[1]
    assert s["status"]["ready"] and s["folder"].endswith(os.path.join("images", "models"))


def test_routes_registered():
    cc_ext.load()
    assert ("POST", "/api/imagegen/generate") in cc_ext.ROUTES


@pytest.fixture
def deb(sd, tmp_path, monkeypatch):
    sys.path.insert(0, DEB_LIB)
    try:
        from victorlib.ctl_ext import imagegen as di
        from victorlib.common import VictorError
    finally:
        sys.path.remove(DEB_LIB)
    monkeypatch.setattr(di, "data_path", lambda *p: str(tmp_path.joinpath("data", *p)))
    monkeypatch.setattr(di, "_path", lambda: str(tmp_path / "data" / "config" / "imagegen.json"))
    return di, VictorError, sd[0]


def test_debian_cli(deb):
    di, VictorError, cfg = deb
    gen = argparse.Namespace(cmd="generate", prompt="a fox", negative="", width=512, height=512, steps=4, cfg=7.0, seed=-1, sampler="euler_a")
    with pytest.raises(VictorError, match="off"):
        di.handle(gen)
    n = di.handle(argparse.Namespace(cmd="set", assign=["enabled=true", "model=sd-v1-5-Q8_0.gguf", "bin=" + cfg["bin"]]))
    assert n["model"].endswith("sd-v1-5-Q8_0.gguf")
    r = di.handle(gen)
    assert r["ok"] and os.path.isfile(r["file"])
    with pytest.raises(VictorError):
        di.handle(argparse.Namespace(cmd="set", assign=["model=../../etc/shadow"]))


def test_optional_victorious_studio_links_are_local_and_preserve_generator():
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.links.append(dict(attrs))

    with open(os.path.join(ARCH, "web", "ext", "imagegen.html")) as stream:
        html = stream.read()
    parser = Links()
    parser.feed(html)
    links = [a for a in parser.links if a.get("href", "").startswith("http")]
    assert {a["href"] for a in links} == {
        "http://127.0.0.1:8188/victorious-studio/",
        "http://127.0.0.1:8188/",
    }
    for link in links:
        assert link["target"] == "_blank"
        assert {"noopener", "noreferrer"} <= set(link["rel"].split())
    assert 'id="igGo"' in html and 'id="igOn"' in html
