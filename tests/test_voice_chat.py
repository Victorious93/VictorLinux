"""Offline voice chat: WAV checks, settings validation, whisper.cpp / Piper invocation (fake programs), routes, Debian parity."""
import argparse
import base64
import filecmp
import os
import struct
import sys

import pytest

import cc_ext
import voice

ARCH = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux")
DEB_LIB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages",
                       "usr", "local", "lib", "victorlinux")


def wav(seconds=1.0, rate=16000, channels=1, bits=16):
    n = int(seconds * rate) * channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * bits // 8,
            channels * bits // 8, bits) + b"data" + struct.pack("<I", n) + b"\0" * n)


@pytest.fixture
def tools(tmp_path):
    """Fake whisper-cli (prints a transcript, records its argv) and fake piper (writes a WAV)."""
    log = tmp_path / "argv.log"
    stt = tmp_path / "whisper-cli"
    stt.write_text(f'#!/bin/sh\necho "$@" >> {log}\nfor a in "$@"; do case "$a" in *.wav) test -s "$a" || exit 3;; esac; done\n'
                   'printf "[BLANK_AUDIO]\\n  What is the weather like on Mars? \\n"\n')
    tts = tmp_path / "piper"
    tts.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --output_file ] && out=$2; shift; done\ncat > /dev/null\n'
                   'printf "RIFF....WAVEfake" > "$out"\n')
    stt.chmod(0o755); tts.chmod(0o755)
    vdir = tmp_path / "data" / "voice"; vdir.mkdir(parents=True)
    (vdir / "ggml-base.en.bin").write_bytes(b"x")
    (vdir / "en_US-amy-medium.onnx").write_bytes(b"x"); (vdir / "en_US-amy-medium.onnx.json").write_text("{}")
    (vdir / "notes.txt").write_text("ignored")
    cfg = dict(voice.DEFAULTS, enabled=True, stt_bin=str(stt), tts_bin=str(tts), stt_model=str(vdir / "ggml-base.en.bin"),
               tts_model=str(vdir / "en_US-amy-medium.onnx"))
    return cfg, log, vdir


def test_shared_module_is_identical_in_both_editions():
    assert filecmp.cmp(os.path.join(ARCH, "voice.py"), os.path.join(DEB_LIB, "victorlib", "voice.py"), shallow=False)


def test_off_by_default_and_wav_rules():
    assert voice.DEFAULTS["enabled"] is False
    assert abs(voice.check_wav(wav(2.0), 20) - 2.0) < 0.01
    for bad, msg in ((wav(1, rate=44100), "16 kHz"), (wav(1, channels=2), "mono"), (wav(0.1), "too short"),
                     (wav(30), "longer than"), (b"OggS" + b"\0" * 60, "WAV")):
        with pytest.raises(voice.VoiceError, match=msg):
            voice.check_wav(bad, 20)


def test_settings_only_accept_listed_files_and_sane_values(tools):
    cfg, _log, vdir = tools
    files = voice.model_files([str(vdir)])
    assert [os.path.basename(p) for p in files["stt"]] == ["ggml-base.en.bin"]
    assert [os.path.basename(p) for p in files["tts"]] == ["en_US-amy-medium.onnx"]
    for bad in ({"stt_model": "/etc/shadow"}, {"enabled": "yes"}, {"max_seconds": 60}, {"language": "english!"},
                {"stt_bin": "whisper-cli"}, {"tts_bin": "/etc/passwd"}):
        with pytest.raises(voice.VoiceError):
            voice.update(cfg, bad, files)
    n = voice.update(voice.DEFAULTS, {"enabled": True, "stt_model": files["stt"][0], "language": "auto", "max_seconds": 10}, files)
    assert n["enabled"] and n["language"] == "auto" and n["max_seconds"] == 10


def test_transcribe_and_speak_run_local_programs_and_leave_no_audio(tools, tmp_path):
    cfg, log, _ = tools
    work = tmp_path / "tmpfs"; work.mkdir()
    assert voice.transcribe(cfg, wav(1.5), threads=3, tmpdir=str(work)) == "What is the weather like on Mars?"
    argv = log.read_text()
    assert "-m " + cfg["stt_model"] in argv and "-l en" in argv and "-t 3" in argv and "-nt" in argv
    assert voice.speak(cfg, "  Thin   and cold.  ", tmpdir=str(work)).startswith(b"RIFF")
    assert list(work.iterdir()) == []                      # recordings and replies are not kept


def test_missing_programs_are_explained(tools):
    cfg, _, vdir = tools
    c = dict(cfg, stt_bin="", tts_bin="")
    st = voice.status(dict(c, stt_bin="/nonexistent/whisper"), voice.model_files([str(vdir)]))
    assert not st["ready"] and any("whisper.cpp" in m for m in st["missing"])


@pytest.fixture
def routes(tools, tmp_path, monkeypatch):
    cfg, _, _ = tools
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    import cc_ext.voice as cv
    monkeypatch.setattr(cv.tc, "RUN_DIR", str(tmp_path / "run"))

    class App:
        class llm:
            @staticmethod
            def running():
                return True
    asked = []
    monkeypatch.setattr(cv, "ask_model", lambda app, msgs: asked.append(msgs) or "Cold, dusty and about minus 60 degrees.")
    return cv, App(), cfg, asked


def test_voice_routes_refuse_when_off_then_answer_with_speech(routes):
    cv, app, cfg, asked = routes
    audio = base64.b64encode(wav(1.0)).decode()
    st, body = cv.ROUTES[("POST", "/api/voice/ask")](app, {"audio": audio})
    assert st == 400 and "off" in body["error"]
    for k in ("stt_bin", "tts_bin"):
        assert cv.ROUTES[("POST", "/api/voice/config")](app, {k: cfg[k]})[0] == 200
    st, body = cv.ROUTES[("POST", "/api/voice/config")](app, {"enabled": True, "stt_model": cfg["stt_model"], "tts_model": cfg["tts_model"]})
    assert st == 200, body
    st, body = cv.ROUTES[("POST", "/api/voice/ask")](app, {"audio": audio, "history": [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]})
    assert st == 200, body
    assert body["text"].startswith("What is the weather") and body["reply"].startswith("Cold")
    assert base64.b64decode(body["audio"]).startswith(b"RIFF")
    assert asked[-1] == [{"role": "user", "content": "hi"}, {"role": "user", "content": body["text"]}]   # system role dropped
    assert cv.ROUTES[("POST", "/api/voice/ask")](app, {"audio": "%%%"})[0] == 400
    assert cv.ROUTES[("POST", "/api/voice/wake")](app, {})[1]["wake_seq"] >= 1
    st, s = cv.ROUTES[("GET", "/api/voice/state")](app, {})
    assert s["status"]["ready"] and s["status"]["tts_ready"]


def test_voice_routes_registered():
    cc_ext.load()
    assert ("POST", "/api/voice/ask") in cc_ext.ROUTES


@pytest.fixture
def deb(tools, tmp_path, monkeypatch):
    sys.path.insert(0, DEB_LIB)
    try:
        from victorlib.ctl_ext import voice as dv
        from victorlib.common import VictorError
    finally:
        sys.path.remove(DEB_LIB)
    monkeypatch.setattr(dv, "data_path", lambda *p: str(tmp_path.joinpath("data", *p)))
    monkeypatch.setattr(dv, "_ask_model", lambda text: "Thin air.")
    return dv, VictorError, tools[0]


def test_debian_voice_cli(deb, tmp_path):
    dv, VictorError, cfg = deb
    w = tmp_path / "q.wav"; w.write_bytes(wav(1.0))
    with pytest.raises(VictorError, match="off"):
        dv.handle(argparse.Namespace(cmd="transcribe", wav=str(w)))
    dv.handle(argparse.Namespace(cmd="set", assign=["enabled=true", "stt_model=ggml-base.en.bin", "stt_bin=" + cfg["stt_bin"],
                                                    "tts_bin=" + cfg["tts_bin"], "tts_model=en_US-amy-medium.onnx"]))
    assert dv.handle(argparse.Namespace(cmd="transcribe", wav=str(w)))["text"].startswith("What is")
    r = dv.handle(argparse.Namespace(cmd="ask", wav=str(w), speak=False))
    assert r["reply"] == "Thin air."
    out = tmp_path / "say.wav"
    assert dv.handle(argparse.Namespace(cmd="say", text="hello", out=str(out)))["bytes"] > 0
    with pytest.raises(VictorError):
        dv.handle(argparse.Namespace(cmd="set", assign=["stt_model=../../etc/shadow"]))
