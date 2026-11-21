#!/usr/bin/env python3
"""Detect PC and Read PC screen aloud for the VictorLinux Phone dashboard.

Detect PC reuses the existing hardware check (`victorcommon.ram_info` / `gpus`) and adds the display session and the
audio inputs from `victormic`. Read-aloud prefers the owner's current text selection; an explicit window read walks
the accessibility tree, drops protected/hidden fields, and says so when a window exposes no text.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

import victormic


def parse_xrandr(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"^(\S+) connected(?: (primary))? (\d+)x(\d+)\+(\d+)\+(\d+)", text, re.M):
        out.append({"name": m.group(1), "primary": bool(m.group(2)), "width": int(m.group(3)), "height": int(m.group(4)),
                    "x": int(m.group(5)), "y": int(m.group(6))})
    return out


def sharing_capability(env: dict[str, str], have_uinput: bool, tools: dict[str, str | None]) -> dict:
    """What RustDesk (phone -> this PC) can honestly do in this desktop session: view, control or neither."""
    sess = env.get("XDG_SESSION_TYPE", "").lower() or ("wayland" if env.get("WAYLAND_DISPLAY") else "x11" if env.get("DISPLAY") else "none")
    if sess == "none":
        return {"session": sess, "view": False, "control": False, "why": "No graphical session is running; there is nothing to share."}
    if sess == "x11":
        return {"session": sess, "view": True, "control": True, "why": "X11: capture and input both work without a prompt."}
    portal = bool(tools.get("xdg-desktop-portal"))
    if not portal:
        return {"session": sess, "view": False, "control": False,
                "why": "Wayland needs xdg-desktop-portal (and your compositor's portal backend) for screen capture. Install it, or log in with an X11 session."}
    return {"session": sess, "view": True, "control": have_uinput,
            "why": "Wayland: the desktop asks you to approve sharing." + ("" if have_uinput else
                   " Input injection needs /dev/uinput access, so a phone can only view (view-only).")}


def detect_pc(env: dict[str, str] | None = None, *, run=None, listing=None, hw=None) -> dict:
    """Collect the PC facts. Every dependency is injectable so this is tested without a desktop."""
    env = env if env is not None else dict(os.environ)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout)
    listing = listing or victormic.pactl_listing
    info: dict = {"hostname": platform.node(), "os": platform.platform()}
    if hw is None:
        try:
            import victorcommon as tc
            hw = {"ram": tc.ram_info(), "gpus": tc.gpus()}
        except Exception as e:                            # the hardware check is optional detail, not a gate
            hw = {"error": str(e)}
    info["hardware"] = hw
    sess = env.get("XDG_SESSION_TYPE", "") or ("wayland" if env.get("WAYLAND_DISPLAY") else "x11" if env.get("DISPLAY") else "none")
    info["session"] = {"type": sess, "display": env.get("DISPLAY", ""), "wayland": env.get("WAYLAND_DISPLAY", ""), "user": env.get("USER", "")}
    try:
        info["displays"] = parse_xrandr(run(["xrandr", "--query"])) if env.get("DISPLAY") else []
    except (OSError, subprocess.SubprocessError):
        info["displays"] = []
    mgr = victormic.MicManager(listing, victormic.Policy())
    st = mgr.refresh()
    info["audio_inputs"] = [dict(s.as_dict(), active=bool(st.active and s.stable_id == st.active.stable_id)) for s in st.connected]
    info["audio_message"] = st.message
    info["sharing"] = sharing_capability(env, os.access("/dev/uinput", os.W_OK), {"xdg-desktop-portal": shutil.which("xdg-desktop-portal")})
    return info


# ---------------------------------------------------------------------------
# Read aloud
# ---------------------------------------------------------------------------

PROTECTED_ROLES = {"password text", "password", "pin", "secret"}
MAX_READ_CHARS = 8000


@dataclass
class ReadResult:
    ok: bool
    text: str
    explanation: str


def extract_window_text(tree: dict) -> ReadResult:
    """Collect readable text from an accessibility tree dict:
    {role, name, text, visible, sensitive, selected_text, children}. Protected, hidden and sensitive nodes are dropped
    with their entire subtree. If anything is selected inside the window, only the selection is returned."""
    chunks: list[str] = []
    selected: list[str] = []
    dropped = [0]

    def walk(n: dict) -> None:
        role = str(n.get("role", "")).lower()
        if role in PROTECTED_ROLES or n.get("sensitive") or n.get("visible") is False:
            dropped[0] += 1
            return
        if n.get("selected_text"):
            selected.append(n["selected_text"])
        if n.get("text") and role in ("label", "text", "paragraph", "heading", "static", "entry", "document text", "list item", "table cell"):
            chunks.append(n["text"])
        for c in n.get("children", ()):
            walk(c)

    walk(tree)
    body = selected or chunks
    text = re.sub(r"\s+", " ", " ".join(body)).strip()[:MAX_READ_CHARS]
    if not text:
        return ReadResult(False, "", "This window exposes no readable text to assistive technology"
                          + (" (protected fields were skipped)." if dropped[0] else ".")
                          + " Select the text you want read and use Read selected text instead.")
    return ReadResult(True, text, "selection only" if selected else "window text" + (f"; {dropped[0]} protected field(s) skipped" if dropped[0] else ""))


def read_selection(env: dict[str, str] | None = None, run=None) -> ReadResult:
    """The owner's current PRIMARY selection. This is text they selected themselves; nothing else is read."""
    env = env if env is not None else dict(os.environ)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=3).stdout)
    cmds = []
    if env.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        cmds.append(["wl-paste", "--primary", "--no-newline"])
    if env.get("DISPLAY") and shutil.which("xclip"):
        cmds.append(["xclip", "-o", "-selection", "primary"])
    if not cmds:
        return ReadResult(False, "", "No selection tool is installed. Arch: pacman -S xclip wl-clipboard. Debian: apt-get install xclip wl-clipboard.")
    for c in cmds:
        try:
            t = re.sub(r"\s+", " ", run(c)).strip()[:MAX_READ_CHARS]
        except (OSError, subprocess.SubprocessError):
            continue
        if t:
            return ReadResult(True, t, "selection only")
    return ReadResult(False, "", "Nothing is selected. Select the text on screen first.")


def tts_argv(text_via_stdin: bool = True) -> list[str] | None:
    """Local speech synthesis: espeak-ng, else spd-say. Text is passed on stdin, never on a command line."""
    if shutil.which("espeak-ng"):
        return ["espeak-ng", "--stdin"]
    if shutil.which("spd-say"):
        return ["spd-say", "-e", "-w"]
    return None


class Speaker:
    """One utterance at a time. While `busy`, push-to-talk refuses to start (recognition is suspended)."""

    def __init__(self, popen=subprocess.Popen):
        self.popen, self.p = popen, None

    @property
    def busy(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def say(self, text: str) -> None:
        argv = tts_argv()
        if not argv:
            raise RuntimeError("No local speech synthesizer. Arch: pacman -S espeak-ng. Debian: apt-get install espeak-ng.")
        self.stop()
        self.p = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            self.p.stdin.write(text)
            self.p.stdin.close()
        except OSError:
            pass

    def stop(self) -> None:
        if self.p and self.p.poll() is None:
            self.p.terminate()
        self.p = None
