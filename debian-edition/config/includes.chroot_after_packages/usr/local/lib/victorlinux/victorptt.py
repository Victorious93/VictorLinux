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
    downloaded automatically. UNVERIFIED with a real model in the build sandbox (the official model host was blocked)."""
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
        self.reset()

    def reset(self) -> None:
        self._rec = self._vosk.KaldiRecognizer(self._model, self._rate)
        self._rec.SetWords(True)

    def accept(self, pcm: bytes) -> None:
        self._rec.AcceptWaveform(pcm)

    def finish(self) -> tuple[str, float]:
        r = json.loads(self._rec.FinalResult())
        words = r.get("result") or []
        conf = sum(w.get("conf", 0.0) for w in words) / len(words) if words else 0.0
        return r.get("text", ""), conf


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
