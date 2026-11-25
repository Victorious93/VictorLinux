"""Offline push-to-talk: audio from the SELECTED source -> local recogniser -> grammar -> shared dispatcher.

The microphone and the controlled phone are independent: the command is bound to the focus captured when the button
went down (or to a named phone). Audio while read-aloud/mirrored playback is active is ignored (EchoGuard). Audio
is processed in memory only. If the recogniser or model is missing, `RecognizerMissing` explains how to install it
and manual control keeps working."""
from __future__ import annotations

import json
import os
from typing import Callable, Protocol

import victorvoice as vv


class RecognizerMissing(Exception):
    pass


class Recognizer(Protocol):
    def accept(self, pcm: bytes) -> None: ...
    def finish(self) -> tuple[str, float]: ...      # (text, confidence 0..1)
    def reset(self) -> None: ...


class VoskRecognizer:
    """Vosk (Apache-2.0) offline recogniser. Needs `pip install vosk` and a model directory; neither is bundled or
    downloaded automatically. Host-tested with the official vosk-model-small-en-us-0.15 and synthetic speech.

    set_vocabulary() restricts recognition to the command words plus the phone nicknames (a Vosk grammar). Commands
    are a closed set, so this rejects unrelated speech and is far more accurate than open dictation."""
    def __init__(self, model_dir: str | None = None, rate: int = 16000):
        st = vv.model_status(model_dir)
        if not st["installed"]:
            raise RecognizerMissing(st["help"])
        try:
            import vosk
        except ImportError as e:
            raise RecognizerMissing("The offline recogniser is not installed. Run: pip install vosk (nothing is downloaded automatically).") from e
        vosk.SetLogLevel(-1)
        self._vosk, self._model, self._rate = vosk, vosk.Model(st["path"]), rate
        self._grammar: str | None = None
        self.reset()

    def set_vocabulary(self, words: list[str] | None) -> None:
        self._grammar = json.dumps(sorted(set(words)) + ["[unk]"]) if words else None
        self.reset()

    def reset(self) -> None:
        if self._grammar:
            self._rec = self._vosk.KaldiRecognizer(self._model, self._rate, self._grammar)
        else:
            self._rec = self._vosk.KaldiRecognizer(self._model, self._rate)
        self._rec.SetWords(True)

    def accept(self, pcm: bytes) -> None:
        self._rec.AcceptWaveform(pcm)

    def finish(self) -> tuple[str, float]:
        r = json.loads(self._rec.FinalResult())
        words = r.get("result") or []
        conf = sum(w.get("conf", 0.0) for w in words) / len(words) if words else 0.0
        return r.get("text", ""), conf


def _voice_module():
    try:
        import voice                      # Arch: /usr/local/lib/victorlinux/voice.py
    except ImportError:
        from victorlib import voice       # Debian: victorlib/voice.py
    return voice


def _default_voice_config() -> str | None:
    try:
        import victorcommon as tc                     # Arch
        return tc.data_path("config", "voice.json")
    except ImportError:
        try:
            from victorlib.common import data_path    # Debian
            return data_path("config", "voice.json")
        except Exception:
            return None
    except Exception:
        return None


COMMAND_WORDS = ("go back home recent apps disconnect release the input control focus enlarge switch to show all every "
                 "phones phone one two three first second third read selected selection pc screen text please my").split()


def command_vocabulary(nicknames: list[str]) -> list[str]:
    """Words the push-to-talk grammar accepts: the fixed command words plus each word of the phone nicknames."""
    nick = [w for n in nicknames for w in vv._norm(n).split()]
    return list(COMMAND_WORDS) + nick


def make_recognizer(backend: str = "vosk", voice_config: str | None = None) -> Recognizer:
    """Pick the offline recogniser for push-to-talk: Vosk (default) or whisper.cpp (the voice-chat setup is reused:
    same program and ggml model the owner picked in Control Center > Voice). Never downloads anything."""
    if backend == "vosk":
        return VoskRecognizer()
    if backend == "whisper":
        voice = _voice_module()
        cfg = voice.load(voice_config or _default_voice_config() or "")
        try:
            return voice.WhisperRecognizer(cfg)
        except voice.VoiceError as e:
            raise RecognizerMissing(str(e)) from e
    raise ValueError(f"unknown recogniser backend: {backend}")


class PttSession:
    MAX_BYTES = 16000 * 2 * 15                      # 15 s utterance cap

    def __init__(self, rec: Recognizer, phones: list[vv.Phone], focus_fn: Callable[[], str],
                 guard: vv.EchoGuard | None = None):
        self.rec, self.phones, self.focus_fn = rec, phones, focus_fn
        self.guard = guard or vv.EchoGuard()
        self.active = False
        self.focus_at_start = ""
        self._bytes = 0

    def press(self) -> None:
        self.rec.reset()
        self.active, self.focus_at_start, self._bytes = True, self.focus_fn(), 0

    def feed(self, pcm: bytes) -> None:
        if not self.active or not self.guard.accept_input() or self._bytes >= self.MAX_BYTES:
            return
        self._bytes += len(pcm)
        self.rec.accept(pcm)

    def cancel(self) -> None:
        """Source disconnected/changed/muted mid-utterance: drop it, never complete it from other audio."""
        self.active = False
        self._bytes = 0
        self.rec.reset()

    def release(self) -> "vv.Command | vv.Ask":
        if not self.active:
            return vv.Ask("unknown")
        self.active = False
        text, conf = self.rec.finish()
        return vv.parse(text, self.phones, self.focus_at_start, self.focus_fn(), conf)


def pc_capture_argv(source_name: str) -> list[str]:
    return ["parec", f"--device={source_name}", "--format=s16le", "--rate=16000", "--channels=1", "--latency-msec=40"]
