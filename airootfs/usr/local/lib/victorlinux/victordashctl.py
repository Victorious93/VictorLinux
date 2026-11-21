#!/usr/bin/env python3
"""Controller for the multi-phone dashboard: owns the scrcpy processes and executes dashboard Actions.

Tk-free so it is tested with fake `adb` / `scrcpy` executables. The GUI, the voice commands and the toolbar all go
through `execute()`, so a spoken command and a mouse click take exactly the same authorized path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

import victordash as vd
import victorvoice as vv
import victorphone as vp


@dataclass
class Result:
    ok: bool
    message: str


class DashController:
    def __init__(self, *, popen=subprocess.Popen, run: Callable[[list[str]], str] | None = None, env: dict | None = None,
                 profile: str = "balanced"):
        self.popen = popen
        self.run = run or self._run
        self.env = env if env is not None else dict(os.environ)
        self.profile = profile
        self.dash = vd.Dashboard()
        self.procs: dict[int, subprocess.Popen] = {}
        self.dev_size: dict[int, tuple[int, int]] = {}
        self.geometry: dict[int, tuple[int, int, int, int]] = {}      # slot -> absolute screen rect
        self.lock = threading.RLock()
        self.features: dict = {}
        self.fullscreen = False

    # ---- system helpers -----------------------------------------------------
    @staticmethod
    def _run(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=8).stdout

    def probe(self) -> dict:
        tools = vp.which_tools()
        h = v = ""
        if tools["scrcpy"]:
            try:
                h = self.run(["scrcpy", "--help"])
                v = self.run(["scrcpy", "--version"])
            except Exception:
                pass
        self.features = vd.probe_features(h, v, bool(tools["adb"]))
        return self.features

    def scan(self) -> list[vd.Phone]:
        """Ready phones, with USB and wireless appearances of one phone merged only on proven identity."""
        devs, _ = vp.list_devices()
        raw = [vd.Phone(d.serial, d.state, d.model) for d in devs]
        return vd.reconcile(raw, self.run)

    # ---- tiles -----------------------------------------------------------------
    def add_phone(self, phone: vd.Phone, replace_slot: int | None = None) -> vd.Tile:
        with self.lock:
            tile = self.dash.add(phone, replace_slot)
            if replace_slot is not None:
                self._kill(replace_slot)
            self._probe_size(tile)
            self._spawn(tile)
            return tile

    def _probe_size(self, tile: vd.Tile) -> None:
        try:
            sz = vd.parse_wm_size(self.run(["adb", "-s", tile.phone.adb_serial, "shell", "wm", "size"]))
            if sz:
                self.dev_size[tile.slot] = sz
        except Exception:
            pass

    def _spawn(self, tile: vd.Tile) -> None:
        self._kill(tile.slot)
        argv = vd.scrcpy_tile_argv(tile, self.geometry.get(tile.slot), profile=self.profile, supports=self.features.get("supports"))
        self.procs[tile.slot] = self.popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                           env=self.env, start_new_session=True)
        self.dash.mark_live(tile.slot, self.procs[tile.slot].pid)

    def _kill(self, slot: int) -> None:
        p = self.procs.pop(slot, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    def close_tile(self, slot: int) -> None:
        with self.lock:
            self._kill(slot)
            self.dash.close(slot)
            self.dev_size.pop(slot, None)

    def shutdown(self) -> None:
        for s in list(self.dash.tiles):
            self.close_tile(s)

    def grant_control(self, slot: int) -> Result:
        with self.lock:
            tile = self.dash.tiles.get(slot)
            if not tile or tile.state != "live":
                return Result(False, "That tile is not live.")
            if not self.features.get("supports", {}).get("no-control", True):
                return Result(False, "This scrcpy cannot start view-only, so control cannot be separated from viewing. Upgrade scrcpy.")
            self.dash.grant_control(slot)
            self._spawn(tile)                                # respawn WITH input, for this phone only
            return Result(True, f"Control granted for {tile.phone.name}.")

    def revoke_control(self, slot: int) -> Result:
        with self.lock:
            tile = self.dash.tiles.get(slot)
            if not tile:
                return Result(False, "No such tile.")
            self._kill(slot)                                 # input stops now: the process that carried it is gone
            self.dash.revoke_control(slot)
            self._spawn(tile)
            return Result(True, f"{tile.phone.name} is view-only.")

    def poll(self) -> list[str]:
        """Detect dropped phones and exited streams. Returns human messages for the status bar."""
        notes: list[str] = []
        with self.lock:
            try:
                present = {p.adb_serial for p in self.scan() if p.ready}
            except FileNotFoundError:
                present = set()
            for slot, tile in list(self.dash.tiles.items()):
                proc = self.procs.get(slot)
                exited = proc is not None and proc.poll() is not None
                if tile.state == "live" and (tile.phone.adb_serial not in present or exited):
                    self._kill(slot)
                    self.dash.phone_gone(tile.phone.adb_serial)
                    notes.append(f"{tile.phone.name} disconnected. Its tile is paused; the other phones are unaffected.")
                elif tile.state == "dropped" and tile.phone.adb_serial in present:
                    self.dash.phone_back(tile.phone)
                    self._spawn(tile)
                    notes.append(f"{tile.phone.name} is back (view-only; grant control again to use it).")
        return notes

    # ---- actions -------------------------------------------------------------
    def keys_for(self) -> list[vv.Named]:
        return [vv.Named(t.phone.stable_id or t.phone.adb_serial, vv.ordinal_names(t.slot, t.phone.model, t.phone.nickname))
                for t in sorted(self.dash.tiles.values(), key=lambda t: t.slot)]

    def _tile_by_key(self, key: str) -> vd.Tile | None:
        return self.dash.tile_for(key)

    def execute(self, action: vv.Action, *, target_key: str | None = None) -> Result:
        """Run an Action. Device actions need an explicit resolved target key and a control grant (except focus/disconnect/view-only)."""
        k = action.kind
        with self.lock:
            if k in vv.LAYOUT:
                return self._layout(action)
            if k in vv.PC:
                return Result(True, "pc")                    # handled by the GUI (selection / speaker)
            tile = self._tile_by_key(target_key) if target_key else None
            if tile is None:
                return Result(False, "No target phone. Say or click which phone.")
            if k == "focus":
                self.dash.focus(tile.slot)
                return Result(True, f"Focused {tile.phone.name}.")
            if k == "disconnect":
                self.close_tile(tile.slot)
                return Result(True, f"Disconnected {tile.phone.name}.")
            if k == "view_only":
                return self.revoke_control(tile.slot)
            if k == "control":
                return self.grant_control(tile.slot)
            if tile.state != "live":
                return Result(False, f"{tile.phone.name} is not connected.")
            if tile.mode != "control":
                return Result(False, f"{tile.phone.name} is view-only. Grant control first; nothing was sent.")
            try:
                if k in vd.KEYCODES:
                    argv = vd.adb_action_argv(tile, k)
                elif k == "scroll":
                    w, h = self.dev_size.get(tile.slot, (1080, 2400))
                    argv = vd.adb_action_argv(tile, "swipe", *vd.swipe_for_scroll(str(action.param), w, h))
                else:
                    return Result(False, f"Unsupported action {k}.")
            except ValueError as e:
                return Result(False, str(e))
            try:
                self.run(argv)
            except Exception as e:
                return Result(False, f"adb failed for {tile.phone.name}: {e}")
            return Result(True, f"{k} sent to {tile.phone.name}.")

    def _layout(self, a: vv.Action) -> Result:
        if a.kind == "release_input":
            for s, t in list(self.dash.tiles.items()):
                if t.mode == "control":
                    self.revoke_control(s)               # kills the input-capable process first
            self.dash.release_input()
            return Result(True, "Input released to the PC.")
        if a.kind == "full_screen_toggle":
            self.fullscreen = not self.fullscreen
            return Result(True, "Full screen " + ("on." if self.fullscreen else "off."))
        if a.kind in ("show_all", "show_n"):
            return Result(True, "layout")                    # the GUI adds phones from the scan; this reports intent only
        return Result(False, "Unsupported layout action.")


def launch_dashboard(env: dict | None = None, *, popen=subprocess.Popen, which=shutil.which, euid=os.geteuid) -> Result:
    """Start the dashboard as the logged-in desktop user. Used by both Control Centers and the desktop launcher.

    Refuses to run as root (the dashboard needs the user's display session and adb keys), refuses without a display,
    and never passes anything but its own fixed argv."""
    env = dict(os.environ if env is None else env)
    if euid() == 0:
        return Result(False, "The Control Center is running as root. Start it from your desktop session as your normal user.")
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return Result(False, "No graphical session is visible to this process (DISPLAY/WAYLAND_DISPLAY is empty). "
                             "Open VictorLinux Phone dashboard from the desktop menu instead.")
    exe = which("victor-phone-dash")
    if not exe:
        return Result(False, "victor-phone-dash is not installed. Run it from the VictorLinux image or add it to PATH.")
    popen([exe], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return Result(True, "Phone dashboard started on your desktop.")
