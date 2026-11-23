"""Offline push-to-talk command grammar for the phone dashboard.

Turns a transcript into a typed Command. Nothing here executes anything: commands are dispatched by the GUI to
the SAME action table the manual buttons use, so authorization is identical. Transcripts and screen text never
reach a shell. If a speech model is missing, manual control is unaffected (see model_status()).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

ACTIONS = {"show_all": "Show all phones", "focus": "Focus", "back": "Back", "home": "Home", "recents": "Recents",
           "read_selected": "Read the selected PC text", "disconnect": "Disconnect", "release_input": "Release input"}
MIN_CONFIDENCE = 0.6
ORD = {"one": 1, "first": 1, "1": 1, "two": 2, "second": 2, "2": 2, "three": 3, "third": 3, "3": 3}
FILLER = re.compile(r"\b(please|the|my|a|on|to|of|show me|can you)\b")


@dataclass
class Phone:
    device_id: str
    nickname: str
    slot: int            # 1-based tile order


@dataclass
class Command:
    action: str
    target: str = ""     # device_id, "" for PC/global actions
    implicit_target: bool = False


@dataclass
class Ask:               # the user must choose; nothing is dispatched
    reason: str          # ambiguous | low-confidence | focus-changed | unknown
    choices: list[str] = field(default_factory=list)   # device ids (or empty)
    text: str = ""


def _norm(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _find_targets(t: str, phones: list[Phone]) -> list[str]:
    hits = [p.device_id for p in phones if _norm(p.nickname) and re.search(rf"\b{re.escape(_norm(p.nickname))}\b", t)]
    if hits:
        return hits
    m = re.search(r"\bphone (one|two|three|1|2|3)\b|\b(first|second|third) phone\b", t)
    if m:
        n = ORD[m.group(1) or m.group(2).replace(" phone", "")]
        return [p.device_id for p in phones if p.slot == n]
    return []


def parse(text: str, phones: list[Phone], focus_at_start: str, focus_now: str | None = None,
          confidence: float = 1.0) -> Command | Ask:
    """focus_at_start: device focused when push-to-talk began (the binding for commands with no named target)."""
    t = _norm(text)
    if not t:
        return Ask("unknown", text=text)
    if confidence < MIN_CONFIDENCE:
        return Ask("low-confidence", text=text)
    if re.search(r"\bshow (all|every)\b.*\bphones?\b|\ball (three|3|two|2) phones\b", t):
        return Command("show_all")
    if re.search(r"\bread\b.*\b(selected|selection)\b.*\b(pc|text)?\b|\bread (the )?(pc )?(screen|selected text)\b", t):
        return Command("read_selected")
    action = None
    for pat, a in ((r"\bgo back\b|\bback\b", "back"), (r"\bgo home\b|\bhome\b", "home"), (r"\brecents?\b|\brecent apps\b", "recents"),
                   (r"\bdisconnect\b", "disconnect"), (r"\brelease (the )?input\b|\brelease control\b", "release_input"),
                   (r"\bfocus\b|\benlarge\b|\bswitch to\b", "focus")):
        if re.search(pat, t):
            action = a
            break
    if action is None:
        return Ask("unknown", text=text)
    targets = _find_targets(FILLER.sub(" ", t) if False else t, phones)
    if len(targets) > 1:
        return Ask("ambiguous", targets, text)
    if len(targets) == 1:
        return Command(action, targets[0])
    if action == "focus":
        return Ask("ambiguous", [p.device_id for p in phones], text)   # focus needs a named phone
    if action == "release_input":
        return Command(action)
    # implicit target: the phone focused when the button went down; if focus moved mid-command, ask.
    if not focus_at_start or (focus_now is not None and focus_now != focus_at_start):
        return Ask("focus-changed" if focus_at_start else "ambiguous", [p.device_id for p in phones], text)
    return Command(action, focus_at_start, implicit_target=True)


class EchoGuard:
    """Prevents our own read-aloud TTS or mirrored phone playback from being recognised as commands: audio is
    ignored while output is playing and for a short tail afterwards."""
    def __init__(self, tail_s: float = 0.8, clock=time.monotonic):
        self.tail_s, self.clock = tail_s, clock
        self._playing = 0
        self._until = 0.0

    def start_output(self) -> None:
        self._playing += 1

    def end_output(self) -> None:
        self._playing = max(0, self._playing - 1)
        self._until = self.clock() + self.tail_s

    def accept_input(self) -> bool:
        return self._playing == 0 and self.clock() >= self._until


def model_status(model_dir: str | None = None) -> dict[str, Any]:
    """Offline recogniser presence. The dashboard shows `help` and keeps manual control usable when missing."""
    d = model_dir or os.environ.get("VICTOR_STT_MODEL", os.path.expanduser("~/.local/share/victorlinux/stt"))
    ok = os.path.isdir(d) and any(os.scandir(d))
    return {"installed": ok, "path": d,
            "help": None if ok else f"No offline speech model found. Import one with `victor-model-import` or unpack a Vosk/whisper.cpp model into {d}. Buttons and shortcuts keep working."}
