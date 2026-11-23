"""Dispatch for voice AND manual controls: one action table, so a spoken command is authorised exactly like a click.

Transcripts and screen text are never passed to a shell: an action maps to a fixed adb key code on one named
serial (argv list, no shell), or to a TileManager/pcread call. Commands that need input control require the tile
to hold the input grant, same as manual buttons."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

import victorscreens as vs
import victorvoice as vv

KEYCODES = {"back": 4, "home": 3, "recents": 187}


@dataclass
class Outcome:
    ok: bool
    message: str
    ask: vv.Ask | None = None


def adb_key(serial: str, action: str) -> list[str]:
    return ["adb", "-s", serial, "shell", "input", "keyevent", str(KEYCODES[action])]


def dispatch(cmd: "vv.Command | vv.Ask", mgr: vs.TileManager, read_fn: Callable[[], str] | None = None,
             run: Callable[..., subprocess.CompletedProcess] | None = None) -> Outcome:
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    if isinstance(cmd, vv.Ask):
        return Outcome(False, "Please choose: " + cmd.reason, cmd)
    a = cmd.action
    if a == "show_all":
        return Outcome(True, f"Showing {len(mgr.tiles)} phone(s)")
    if a == "read_selected":
        return Outcome(True, read_fn() if read_fn else "Reading is not available")
    if a == "release_input":
        mgr.release_input()
        return Outcome(True, "Input released")
    try:
        tile = mgr.get(cmd.target)
    except StopIteration:
        return Outcome(False, "That phone is not on the dashboard")
    if a == "focus":
        mgr.set_focus(tile.device_id)
        return Outcome(True, f"Focused {tile.nickname}")
    if a == "disconnect":
        mgr.set_state(tile.device_id, "disconnected")
        return Outcome(True, f"Disconnected {tile.nickname}")
    if a in KEYCODES:
        # same gate as a manual click: the tile must be live, control-granted and holding input
        if mgr.route_input(tile.device_id, tile.epoch, {"kind": "key", "code": a}) is None:
            return Outcome(False, f"{tile.nickname} is view-only or input is released")
        r = run(adb_key(tile.device_id, a))
        return Outcome(r.returncode == 0, f"{a.title()} sent to {tile.nickname}" if r.returncode == 0 else "adb failed")
    return Outcome(False, "Unsupported action")
