"""whisper.cpp as a push-to-talk backend: reuses the voice-chat settings and the existing PttSession/grammar."""
import filecmp
import json
import os

import pytest

import victorptt
import victorvoice as vv
import voice

ARCH = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux")
DEB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages", "usr", "local", "lib", "victorlinux")


def test_copies_stay_identical():
    for a, d in (("victorptt.py", "victorptt.py"), ("victormic.py", "victormic.py"), ("voice.py", "victorlib/voice.py")):
        assert filecmp.cmp(os.path.join(ARCH, a), os.path.join(DEB, d), shallow=False), a


@pytest.fixture
def cfg(tmp_path):
    stt = tmp_path / "whisper-cli"
    stt.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in *.wav) test -s "$a" || exit 3;; esac; done\necho "[BLANK_AUDIO]"\necho " screenshot "\n')
    stt.chmod(0o755)
    model = tmp_path / "ggml-base.en.bin"; model.write_bytes(b"x")
    c = dict(voice.DEFAULTS, enabled=True, stt_bin=str(stt), stt_model=str(model))
    p = tmp_path / "voice.json"; p.write_text(json.dumps(c))
    return c, str(p)


def test_pcm_to_wav_passes_the_voice_chat_checks():
    assert abs(voice.check_wav(voice.pcm_to_wav(b"\0" * 32000), 20) - 1.0) < 0.01


def test_whisper_recognizer_feeds_the_existing_ptt_session(cfg, tmp_path):
    _c, path = cfg
    rec = victorptt.make_recognizer("whisper", path)
    phones = [vv.Phone("dev1", "pixel", 1)]
    s = victorptt.PttSession(rec, phones, lambda: "dev1")
    s.press(); s.feed(b"\0" * 16000); s.feed(b"\0" * 16000)
    res = s.release()
    assert rec.finish() == ("", 0.0)                  # buffer cleared after each utterance; short audio is ignored
    assert isinstance(res, (vv.Command, vv.Ask))
    s.press(); s.feed(b"\0" * 32000); s.cancel()
    assert len(rec._buf) == 0                          # cancelled audio is dropped, never transcribed


def test_whisper_backend_explains_missing_setup(tmp_path):
    with pytest.raises(victorptt.RecognizerMissing, match="nothing is downloaded"):
        victorptt.make_recognizer("whisper", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError):
        victorptt.make_recognizer("cloud")


def test_victor_mic_has_backend_flag():
    src = open(os.path.join(ARCH, "victormic.py")).read()
    assert '"--ptt-backend", choices=("vosk", "whisper"), default="vosk"' in src
