#!/usr/bin/env python3
"""Offline push-to-talk voice commands for the VictorLinux Phone dashboard.

Recognised text is mapped by a closed grammar onto dashboard Actions - the same actions a mouse or keyboard
produces. There is no free-form path: unknown text yields `Unrecognized`, and nothing here can reach a shell.
Spoken words and screen text are data only.
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

# Action kinds. Layout actions may touch all tiles; device actions address exactly one phone; pc actions touch the PC.
LAYOUT = {"show_all", "show_n", "full_screen_toggle", "release_input"}
DEVICE = {"focus", "back", "home", "recents", "scroll", "disconnect", "view_only", "control"}
PC = {"read_selection", "stop_reading"}
KINDS = LAYOUT | DEVICE | PC

NUMBER_WORDS = {"one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3, "first": 1, "second": 2, "third": 3}


@dataclass(frozen=True)
class Action:
    kind: str
    target: str | None = None          # phone name as spoken (device actions); resolved later
    param: str | int | None = None
    scope: str = ""

    def __post_init__(self):
        assert self.kind in KINDS


@dataclass(frozen=True)
class Unrecognized:
    text: str


def _norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


_TARGET_TAIL = re.compile(r"^(?P<cmd>.+?)(?:\s+(?:on|of|for|in)\s+(?P<target>.+))?$")


def _tgt(t: str | None) -> str | None:
    return re.sub(r"^the\s+", "", t).strip() if t else None


def parse_command(text: str) -> Action | Unrecognized:
    """Map recognised text to an Action, or Unrecognized. Word order is fixed; extra filler is tolerated."""
    t = _norm(text)
    t = re.sub(r"^(please|ok|okay|hey|computer)\s+", "", t)
    if not t:
        return Unrecognized(text)

    if re.fullmatch(r"show all (?:three )?phones?|show all|show all screens|show every phone", t):
        return Action("show_all", scope="layout")
    m = re.fullmatch(r"show (?:me )?(one|two|three|1|2|3) phones?", t)
    if m:
        return Action("show_n", param=NUMBER_WORDS[m.group(1)], scope="layout")
    if t in ("full screen", "go full screen", "toggle full screen", "exit full screen"):
        return Action("full_screen_toggle", scope="layout")
    if t in ("release input", "release the input", "release keyboard", "give me my keyboard back"):
        return Action("release_input", scope="layout")
    if re.fullmatch(r"(read|speak) (?:the )?(?:selected|selection) (?:pc )?(?:text)?", t) or re.fullmatch(r"read (?:the )?selected pc text", t):
        return Action("read_selection", scope="pc")
    if t in ("stop reading", "stop talking", "be quiet"):
        return Action("stop_reading", scope="pc")

    m = re.fullmatch(r"focus (?:on )?(?P<target>.+)", t)
    if m:
        return Action("focus", target=_tgt(m.group("target")), scope="device")

    m = re.fullmatch(r"(?:go )?(?P<cmd>back|home|recents|recent apps|overview)(?: (?:on|in|for) (?P<target>.+))?", t)
    if m:
        cmd = {"recent apps": "recents", "overview": "recents"}.get(m.group("cmd"), m.group("cmd"))
        return Action(cmd, target=_tgt(m.group("target")), scope="device")

    m = re.fullmatch(r"scroll (?P<dir>up|down|left|right)(?: (?:on|in|for) (?P<target>.+))?", t)
    if m:
        return Action("scroll", target=_tgt(m.group("target")), param=m.group("dir"), scope="device")

    m = re.fullmatch(r"disconnect (?P<target>.+)", t)
    if m:
        return Action("disconnect", target=_tgt(m.group("target")), scope="device")
    m = re.fullmatch(r"(?:make )?(?P<target>.+?) view only", t)
    if m:
        return Action("view_only", target=_tgt(m.group("target")), scope="device")
    return Unrecognized(text)


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Named:
    stable_key: str          # stable identity (never the display name)
    names: tuple[str, ...]   # nickname, model, "phone one"...


@dataclass(frozen=True)
class Resolved:
    key: str


@dataclass(frozen=True)
class NeedTarget:
    reason: str
    candidates: tuple[str, ...]


CONFIDENCE = 0.75


def resolve_target(spoken: str | None, phones: list[Named], focus_at_ptt: str | None, focus_now: str | None) -> Resolved | NeedTarget:
    """Resolve a device action's target.

    * Named phone: matched against names by stable identity; duplicates or low confidence ask.
    * Unnamed: binds to the focus at push-to-talk start; if focus moved since, ask instead of guessing."""
    if not phones:
        return NeedTarget("No phone is connected.", ())
    if not spoken:
        if focus_at_ptt is None:
            return NeedTarget("No phone is focused. Say which phone.", tuple(p.names[0] for p in phones))
        if focus_now != focus_at_ptt:
            return NeedTarget("Focus changed while you were speaking. Which phone?", tuple(p.names[0] for p in phones))
        return Resolved(focus_at_ptt)
    q = _norm(spoken)
    scored: list[tuple[float, Named]] = []
    for p in phones:
        best = 0.0
        for n in p.names:
            nn = _norm(n)
            if not nn:
                continue
            r = 1.0 if (q == nn or q in nn.split() or nn in q.split()) else difflib.SequenceMatcher(None, q, nn).ratio()
            best = max(best, r)
        scored.append((best, p))
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    if top < CONFIDENCE:
        return NeedTarget(f"I could not match '{spoken}' to a phone.", tuple(p.names[0] for p in phones))
    tied = [p for s, p in scored if abs(s - top) < 0.05]
    if len(tied) > 1:
        return NeedTarget(f"More than one phone matches '{spoken}'.", tuple(p.names[0] for p in tied))
    return Resolved(tied[0].stable_key)


def ordinal_names(slot: int, model: str, nickname: str = "") -> tuple[str, ...]:
    word = ["one", "two", "three"][slot]
    names = [f"phone {word}", f"phone {slot + 1}"]
    if nickname:
        names.insert(0, nickname)
    if model:
        names.append(model)
    return tuple(names)


# ---------------------------------------------------------------------------
# Push-to-talk controller and recognizers
# ---------------------------------------------------------------------------

@dataclass
class Utterance:
    focus_at_start: str | None
    cancelled: str | None = None
    audio: bytearray = field(default_factory=bytearray)


class SpeechUnavailable(Exception):
    """No recognizer or model: the message tells the owner exactly how to fix it."""


MODEL_HELP = (
    "Offline voice needs the Python 'vosk' package and a Vosk model directory.\n"
    "  Install:  pip install --user vosk   (Arch: the AUR package python-vosk; Debian: pip in a venv)\n"
    "  Model:    download a small model (for example vosk-model-small-en-us-0.15) from https://alphacephei.com/vosk/models\n"
    "            on a connected machine, unzip it, and copy the folder to {path}\n"
    "Nothing is downloaded automatically. The mouse and keyboard dashboard works without it."
)


def model_dir(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    base = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME", "~"), ".local", "share")
    return os.path.join(os.path.expanduser(base), "victorlinux", "stt")


def find_model(env: dict[str, str] | None = None) -> str | None:
    d = model_dir(env)
    try:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(os.path.join(p, "am", "final.mdl")) or os.path.isfile(os.path.join(p, "final.mdl")):
                return p
    except OSError:
        pass
    return None


def speech_status(env: dict[str, str] | None = None, have_module=lambda n: importlib.util.find_spec(n) is not None) -> dict:
    mod, model = have_module("vosk"), find_model(env)
    ok = bool(mod and model)
    return {"ready": ok, "module": mod, "model": model,
            "help": "" if ok else MODEL_HELP.format(path=model_dir(env))}


class VoskRecognizer:
    """Offline recognition of 16 kHz mono s16le PCM. Constructed only when `speech_status()['ready']`."""

    def __init__(self, model_path: str, rate: int = 16000):
        try:
            import vosk                                  # type: ignore
        except ImportError as e:
            raise SpeechUnavailable(MODEL_HELP.format(path=model_dir())) from e
        vosk.SetLogLevel(-1)
        self._rec = vosk.KaldiRecognizer(vosk.Model(model_path), rate)

    def feed(self, pcm: bytes) -> None:
        self._rec.AcceptWaveform(pcm)

    def finish(self) -> str:
        return json.loads(self._rec.FinalResult() or "{}").get("text", "")


class PushToTalk:
    """Open capture on press, close it on release, recognise, then parse.

    `capture_factory(source_name)` -> object with .read(n)->bytes and .close(); `recognizer_factory()` -> .feed/.finish."""

    def __init__(self, capture_factory, recognizer_factory, mic_name, focus_getter, tts_busy=lambda: False):
        self.capture_factory, self.recognizer_factory = capture_factory, recognizer_factory
        self.mic_name, self.focus_getter, self.tts_busy = mic_name, focus_getter, tts_busy
        self.utt: Utterance | None = None
        self.cap = None
        self.rec = None

    def press(self) -> Utterance:
        if self.tts_busy():
            raise RuntimeError("Reading aloud is in progress; recognition is suspended.")
        if self.utt:
            raise RuntimeError("already listening")
        self.rec = self.recognizer_factory()
        self.cap = self.capture_factory(self.mic_name())
        self.utt = Utterance(self.focus_getter())
        return self.utt

    def pump(self, n: int = 3200) -> None:
        if self.utt and self.cap and not self.utt.cancelled:
            chunk = self.cap.read(n)
            if chunk:
                self.rec.feed(chunk)

    def cancel(self, why: str) -> None:
        if self.utt:
            self.utt.cancelled = why
        self._close()

    def release(self) -> tuple[Utterance, Action | Unrecognized | None]:
        utt = self.utt
        if utt is None:
            raise RuntimeError("not listening")
        text = "" if utt.cancelled else self.rec.finish()
        self._close()
        self.utt = None
        if utt.cancelled:
            return utt, None
        return utt, parse_command(text)

    def _close(self) -> None:
        if self.cap:
            try:
                self.cap.close()
            except Exception:
                pass
            self.cap = None


class ProcessCapture:
    """`parec` stream; closed when push-to-talk ends so the capture stream never lingers."""

    def __init__(self, argv: list[str], popen=subprocess.Popen):
        self.p = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, n: int) -> bytes:
        return self.p.stdout.read(n)

    def close(self) -> None:
        try:
            self.p.terminate()
            self.p.wait(timeout=2)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
