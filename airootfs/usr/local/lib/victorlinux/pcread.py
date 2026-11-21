"""Detect PC + Read PC screen aloud (the two separate phone/PC actions).

detect(): reuses the hardware facts pccheck already collects and adds display session and audio inputs.
read_selection(): reads the user's selected text, or the authorised ACTIVE window's text, through a backend
(AT-SPI on the desktop). The policy here is what guarantees privacy and is backend-independent:
only the active window is ever queried, password/protected fields are dropped, a selection older than
MAX_SELECTION_AGE_S or from another window is not returned, and nothing is read without opt-in. Reading is
independent of microphone sharing. Text is returned to the caller; it is not logged or stored.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

MAX_SELECTION_AGE_S = 30
MAX_CHARS = 8000
PROTECTED_ROLES = {"password text", "password", "pin"}


def display_session(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    t = (e.get("XDG_SESSION_TYPE") or "").lower()
    if t in ("wayland", "x11"):
        return t
    return "wayland" if e.get("WAYLAND_DISPLAY") else "x11" if e.get("DISPLAY") else "none"


def detect(facts: dict[str, Any], pc_name: str, mics: list[Any], active_input: str, env: dict[str, str] | None = None,
           clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """`facts` is pccheck's dictionary (cpu_name, gpu_names, ram_total); keys are read defensively."""
    gib = round((facts.get("ram_total") or 0) / 2**30, 1)
    return {"name": pc_name, "cpu": facts.get("cpu_name") or "unknown", "gpu": list(facts.get("gpu_names") or []) or ["unknown"],
            "memory_gib": gib, "display_session": display_session(env),
            "microphones": [getattr(m, "label", str(m)) for m in mics], "active_input": active_input or "none",
            "measured_at": clock(), "online": True}


def freshness(report: dict[str, Any] | None, now: float, stale_after_s: float = 60) -> str:
    if not report or not report.get("online", False):
        return "offline"
    age = now - report["measured_at"]
    return "fresh" if age <= stale_after_s else f"stale ({int(age)} s old)"


@dataclass
class Window:
    window_id: str
    app: str
    title: str


@dataclass
class TextRun:
    text: str
    role: str = "text"
    window_id: str = ""
    selected: bool = False
    protected: bool = False
    stamp: float = 0.0               # when the selection was made


class Backend(Protocol):
    def active_window(self) -> Window | None: ...
    def runs(self, window: Window) -> list[TextRun]: ...


@dataclass
class ReadResult:
    ok: bool
    text: str = ""
    pc: str = ""
    window: str = ""
    reason: str = ""                 # not-enabled | no-window | nothing-readable | denied
    skipped_protected: int = 0


def read_selection(backend: Backend | None, pc_name: str, enabled: bool, allowed_windows: set[str] | None = None,
                   now: float | None = None) -> ReadResult:
    """allowed_windows: window ids the owner authorised for this read (None = the active window is authorised by
    the owner invoking the action). Never reads any window other than the active one."""
    if not enabled:
        return ReadResult(False, pc=pc_name, reason="not-enabled")
    if backend is None:
        return ReadResult(False, pc=pc_name, reason="nothing-readable")
    now = time.time() if now is None else now
    win = backend.active_window()
    if win is None:
        return ReadResult(False, pc=pc_name, reason="no-window")
    if allowed_windows is not None and win.window_id not in allowed_windows:
        return ReadResult(False, pc=pc_name, window=win.title, reason="denied")
    skipped = 0
    sel: list[str] = []
    body: list[str] = []
    for r in backend.runs(win):
        if r.window_id and r.window_id != win.window_id:
            continue                 # text from a different (e.g. adjacent private) window
        if r.protected or r.role.lower() in PROTECTED_ROLES:
            skipped += 1
            continue
        if r.selected:
            if r.stamp and now - r.stamp > MAX_SELECTION_AGE_S:
                continue             # previous selection: stale
            sel.append(r.text)
        else:
            body.append(r.text)
    text = "\n".join(sel) if sel else "\n".join(body)
    text = text.strip()[:MAX_CHARS]
    if not text:
        return ReadResult(False, pc=pc_name, window=win.title, reason="nothing-readable", skipped_protected=skipped)
    return ReadResult(True, text, pc_name, win.title, skipped_protected=skipped)


def announce(r: ReadResult) -> str:
    """Spoken/displayed preface naming the PC and window, or the failure reason."""
    if r.ok:
        return f"From {r.pc}, window {r.window}: {r.text}"
    return {"not-enabled": "Reading the PC screen is turned off.", "no-window": f"No active window on {r.pc}.",
            "denied": f"{r.pc} did not authorise reading that window.",
            "nothing-readable": f"No readable text in {r.window or 'the active window'} on {r.pc}."}.get(r.reason, "Cannot read.")


class AtspiBackend:
    """Desktop accessibility backend. UNVERIFIED in this workspace (no AT-SPI session available): requires
    python3-gi + gir1.2-atspi-2.0 and an accessibility-enabled desktop. Wayland compositors only expose the
    active window's AT-SPI tree to the user's own session; that is exactly what we use."""
    def __init__(self) -> None:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self.A = Atspi

    def active_window(self) -> Window | None:
        A = self.A
        desk = A.get_desktop(0)
        for i in range(desk.get_child_count()):
            app = desk.get_child_at_index(i)
            for j in range(app.get_child_count()):
                w = app.get_child_at_index(j)
                if w and w.get_state_set().contains(A.StateType.ACTIVE):
                    return Window(f"{app.get_name()}:{j}", app.get_name(), w.get_name() or "")
        return None

    def runs(self, window: Window) -> list[TextRun]:
        A = self.A
        out: list[TextRun] = []
        app_name, idx = window.window_id.rsplit(":", 1)
        desk = A.get_desktop(0)
        for i in range(desk.get_child_count()):
            app = desk.get_child_at_index(i)
            if app.get_name() != app_name:
                continue
            root = app.get_child_at_index(int(idx))
            stack = [root]
            while stack:
                n = stack.pop()
                role = n.get_role_name() or ""
                prot = role.lower() in PROTECTED_ROLES or n.get_state_set().contains(A.StateType.SENSITIVE) is False and role == "password text"
                try:
                    ti = n.get_text_iface()
                except Exception:
                    ti = None
                if ti is not None:
                    cnt = ti.get_character_count()
                    sels = [ti.get_selection(k) for k in range(ti.get_n_selections())]
                    for s in sels:
                        out.append(TextRun(ti.get_text(s.start_offset, s.end_offset), role, window.window_id, True, prot, time.time()))
                    if not sels and cnt:
                        out.append(TextRun(ti.get_text(0, cnt), role, window.window_id, False, prot))
                for k in range(n.get_child_count()):
                    c = n.get_child_at_index(k)
                    if c:
                        stack.append(c)
        return out
