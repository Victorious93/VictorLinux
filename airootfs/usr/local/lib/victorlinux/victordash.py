#!/usr/bin/env python3
"""Multi-phone dashboard logic for VictorLinux Phone (no Tk, no subprocess at import time).

Everything here is pure or takes an injected runner, so it is unit-tested without a phone, scrcpy or a display.

Design (honest about what scrcpy offers):
  * One scrcpy process per tile, each started with `--serial=<exact adb serial>`, so a stream and its input can
    never reach another phone. The dashboard positions each borderless scrcpy window over its tile (X11) and
    draws the tile chrome (name, state, toolbar) itself.
  * View-only is `scrcpy --no-control`. Granting control respawns the tile with input; revoking kills that
    process and respawns view-only, so revocation stops input at once.
  * Toolbar / voice actions that are not mouse or keyboard events (Back, Home, Recents, scroll) go through
    `adb -s <serial> shell input ...` for that tile only, and only while the tile holds the control grant.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

MAX_TILES = 3

# Android key codes used by the toolbar (documented in android.view.KeyEvent).
KEYCODES = {"back": 4, "home": 3, "recents": 187}

PROFILES = {
    # name: (max-size px, max-fps, video bit rate, audio)
    "balanced": (1024, 30, "8M", False),
    "low-load": (720, 15, "2M", False),     # for an 8 GB workstation with three streams
    "sharp": (1600, 60, "16M", False),
}

# ---------------------------------------------------------------------------
# Identity: USB and wireless appearances of one phone are merged only when a probe proves it.
# ---------------------------------------------------------------------------

WIRELESS_SERIAL = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]):\d{2,5}$|^adb-.+\._adb-tls-connect\._tcp\.?$")


def transport_of(serial: str) -> str:
    return "wifi" if WIRELESS_SERIAL.match(serial or "") else "usb"


@dataclass(frozen=True)
class Phone:
    adb_serial: str           # exact serial passed to `-s`
    state: str = "device"     # adb state
    model: str = ""
    stable_id: str = ""       # proven hardware identity ("" = not yet proven)
    nickname: str = ""

    @property
    def transport(self) -> str:
        return transport_of(self.adb_serial)

    @property
    def name(self) -> str:
        return self.nickname or self.model or self.adb_serial

    @property
    def ready(self) -> bool:
        return self.state == "device"


def probe_identity(adb_serial: str, run: Callable[[list[str]], str]) -> str:
    """Return a stable hardware id for the phone, or '' if it cannot be proven.

    ro.serialno plus the build fingerprint's device/model is used; both must be non-empty. A wireless and a USB
    appearance are the same phone only if these strings are equal. An unreadable property means 'unknown', and an
    unknown identity is never merged with anything."""
    try:
        sn = run(["adb", "-s", adb_serial, "shell", "getprop", "ro.serialno"]).strip()
        dev = run(["adb", "-s", adb_serial, "shell", "getprop", "ro.product.device"]).strip()
    except Exception:
        return ""
    if not sn or sn.lower() in ("unknown", "0123456789abcdef") or not dev:
        return ""
    return f"{dev}:{sn}"


def reconcile(phones: Iterable[Phone], run: Callable[[list[str]], str]) -> list[Phone]:
    """Fill in stable ids and collapse USB+wireless appearances of the same proven phone (USB preferred)."""
    probed: list[Phone] = []
    for p in phones:
        sid = p.stable_id or (probe_identity(p.adb_serial, run) if p.ready else "")
        probed.append(Phone(p.adb_serial, p.state, p.model, sid, p.nickname))
    out: dict[str, Phone] = {}
    unknown: list[Phone] = []
    for p in probed:
        if not p.stable_id:
            unknown.append(p)
            continue
        cur = out.get(p.stable_id)
        if cur is None or (cur.transport == "wifi" and p.transport == "usb"):
            out[p.stable_id] = p
    return sorted(out.values(), key=lambda x: x.name) + unknown


# ---------------------------------------------------------------------------
# Tile model
# ---------------------------------------------------------------------------

@dataclass
class Tile:
    slot: int
    phone: Phone
    session: str                    # unique per start; every input event carries it
    mode: str = "view"              # view | control
    state: str = "starting"         # starting | live | dropped | stopped
    epoch: int = 0                  # bumps on every grant/revoke so stale input is discarded
    pid: int | None = None


class PickReplacement(Exception):
    """A fourth phone was requested: the owner must choose which visible tile to replace."""

    def __init__(self, candidates: list[Tile]):
        super().__init__("all three tiles are in use; choose a tile to replace")
        self.candidates = candidates


class InputRejected(Exception):
    pass


@dataclass
class Dashboard:
    clock: Callable[[], float] = time.monotonic
    tiles: dict[int, Tile] = field(default_factory=dict)
    focus_slot: int | None = None
    _seq: int = 0

    # --- lifecycle ---------------------------------------------------------
    def _new_session(self, phone: Phone) -> str:
        self._seq += 1
        return f"s{self._seq}-{abs(hash((phone.stable_id or phone.adb_serial, self._seq))) % 10**6:06d}"

    def tile_for(self, key: str) -> Tile | None:
        for t in self.tiles.values():
            if key in (t.phone.stable_id, t.phone.adb_serial):
                return t
        return None

    def add(self, phone: Phone, replace_slot: int | None = None) -> Tile:
        if not phone.ready:
            raise ValueError(f"{phone.name} is {phone.state}, not ready")
        existing = self.tile_for(phone.stable_id) if phone.stable_id else None
        existing = existing or self.tile_for(phone.adb_serial)
        if existing:
            return existing
        if replace_slot is None:
            free = [s for s in range(MAX_TILES) if s not in self.tiles]
            if not free:
                raise PickReplacement(list(self.tiles.values()))
            slot = free[0]
        else:
            if replace_slot not in self.tiles:
                raise ValueError("no such tile")
            self.close(replace_slot)
            slot = replace_slot
        t = Tile(slot, phone, self._new_session(phone))
        self.tiles[slot] = t
        return t

    def close(self, slot: int) -> None:
        t = self.tiles.pop(slot, None)
        if t:
            t.state = "stopped"
            t.epoch += 1
        if self.focus_slot == slot:
            self.focus_slot = None

    def mark_live(self, slot: int, pid: int | None = None) -> None:
        t = self.tiles[slot]
        t.state, t.pid = "live", pid

    def phone_gone(self, key: str) -> Tile | None:
        """A phone vanished (cable pulled, wireless dropped). Only its own tile changes."""
        t = self.tile_for(key)
        if not t:
            return None
        t.state = "dropped"
        t.mode = "view"
        t.epoch += 1              # control must be granted afresh after it returns
        if self.focus_slot == t.slot:
            self.focus_slot = None
        return t

    def phone_back(self, phone: Phone) -> Tile | None:
        t = self.tile_for(phone.stable_id) if phone.stable_id else None
        t = t or self.tile_for(phone.adb_serial)
        if not t or t.state != "dropped":
            return t
        t.phone = phone
        t.session = self._new_session(phone)     # a fresh stream, a fresh session, view-only
        t.state, t.mode = "starting", "view"
        return t

    # --- focus / control ---------------------------------------------------
    def focus(self, slot: int | None) -> None:
        if slot is not None and slot not in self.tiles:
            raise ValueError("no such tile")
        self.focus_slot = slot

    def grant_control(self, slot: int) -> Tile:
        t = self.tiles[slot]
        if t.state != "live":
            raise InputRejected("tile is not live")
        t.mode = "control"
        t.epoch += 1
        return t

    def revoke_control(self, slot: int) -> Tile:
        t = self.tiles[slot]
        t.mode = "view"
        t.epoch += 1
        return t

    def release_input(self) -> None:
        """'Release input back to the PC': clear focus and drop every control grant."""
        for t in self.tiles.values():
            if t.mode == "control":
                self.revoke_control(t.slot)
        self.focus_slot = None

    # --- input gate --------------------------------------------------------
    def authorize_input(self, session: str, epoch: int) -> Tile:
        """Every input event carries (session, epoch). Raises InputRejected unless it matches a live, controlled tile."""
        for t in self.tiles.values():
            if t.session == session:
                if t.state != "live":
                    raise InputRejected("tile is not live")
                if t.mode != "control":
                    raise InputRejected("tile is view-only")
                if t.epoch != epoch:
                    raise InputRejected("stale control grant")
                return t
        raise InputRejected("unknown session")


# ---------------------------------------------------------------------------
# Layout and coordinate mapping
# ---------------------------------------------------------------------------

def grid(n: int, width: int, height: int, gap: int = 8) -> list[tuple[int, int, int, int]]:
    """Tile rectangles (x, y, w, h) filling width x height for 1..3 tiles, side by side (phones are tall)."""
    if n < 1:
        return []
    n = min(n, MAX_TILES)
    w = (width - gap * (n - 1)) // n
    return [(i * (w + gap), 0, w, height) for i in range(n)]


def fit_rect(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int, int, int]:
    """Letterboxed placement (x, y, w, h) of a src_w x src_h image inside box_w x box_h, aspect preserved."""
    if min(src_w, src_h, box_w, box_h) <= 0:
        return (0, 0, 0, 0)
    scale = min(box_w / src_w, box_h / src_h)
    w, h = int(src_w * scale), int(src_h * scale)
    return ((box_w - w) // 2, (box_h - h) // 2, w, h)


def widget_to_device(px: int, py: int, box_w: int, box_h: int, dev_w: int, dev_h: int) -> tuple[int, int] | None:
    """Map a click inside a letterboxed tile to device pixels. None if the click is on a letterbox bar.
    dev_w/dev_h are the *current* (post-rotation) device dimensions."""
    x0, y0, w, h = fit_rect(dev_w, dev_h, box_w, box_h)
    if w == 0 or not (x0 <= px < x0 + w and y0 <= py < y0 + h):
        return None
    return (min(dev_w - 1, int((px - x0) * dev_w / w)), min(dev_h - 1, int((py - y0) * dev_h / h)))


def rotated_size(w: int, h: int, orientation: int) -> tuple[int, int]:
    """Size after rotation from the natural (`wm size`) dimensions: Surface.ROTATION_90/270 (1, 3) swap the axes."""
    return (h, w) if orientation in (1, 3) else (w, h)


def parse_wm_size(text: str) -> tuple[int, int] | None:
    """`adb shell wm size` -> physical (w, h); an 'Override size' line wins."""
    best = None
    for m in re.finditer(r"(Physical|Override) size:\s*(\d+)x(\d+)", text):
        best = (int(m.group(2)), int(m.group(3)))
        if m.group(1) == "Override":
            return best
    return best


def parse_orientation(text: str) -> int | None:
    """`adb shell dumpsys input` contains 'SurfaceOrientation: N'."""
    m = re.search(r"SurfaceOrientation:\s*(\d)", text)
    return int(m.group(1)) if m else None


def swipe_for_scroll(direction: str, dev_w: int, dev_h: int) -> tuple[int, int, int, int, int]:
    """(x1, y1, x2, y2, duration_ms) for a finger swipe that scrolls content in `direction`."""
    cx, cy = dev_w // 2, dev_h // 2
    dy, dx = dev_h // 4, dev_w // 4
    table = {
        "down": (cx, cy + dy, cx, cy - dy),     # content moves up: finger swipes up
        "up": (cx, cy - dy, cx, cy + dy),
        "right": (cx + dx, cy, cx - dx, cy),
        "left": (cx - dx, cy, cx + dx, cy),
    }
    if direction not in table:
        raise ValueError(direction)
    return (*table[direction], 250)


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def scrcpy_tile_argv(tile: Tile, rect: tuple[int, int, int, int] | None, *, profile: str = "balanced",
                     origin: tuple[int, int] = (0, 0), supports: dict[str, bool] | None = None) -> list[str]:
    """argv for one tile. The serial is always explicit; view-only tiles are `--no-control`."""
    sup = supports or {}
    size, fps, rate, audio = PROFILES[profile]
    argv = ["scrcpy", f"--serial={tile.phone.adb_serial}", f"--window-title=VictorLinux Phone {tile.slot + 1}: {tile.phone.name}",
            f"--max-size={size}", f"--max-fps={fps}", f"--video-bit-rate={rate}"]
    if not audio and sup.get("no-audio", True):
        argv.append("--no-audio")
    if tile.mode != "control":
        argv.append("--no-control")
    if rect and sup.get("window-position", True):
        x, y, w, h = rect
        argv += [f"--window-x={origin[0] + x}", f"--window-y={origin[1] + y}", f"--window-width={w}", f"--window-height={h}",
                 "--window-borderless"]
    return argv


def adb_action_argv(tile: Tile, action: str, *args: int | str) -> list[str]:
    """adb argv for a toolbar/voice action on exactly one phone. Only whitelisted actions exist; nothing here is a shell."""
    base = ["adb", "-s", tile.phone.adb_serial, "shell", "input"]
    if action in KEYCODES:
        return base + ["keyevent", str(KEYCODES[action])]
    if action == "swipe":
        x1, y1, x2, y2, ms = (int(a) for a in args)
        return base + ["swipe", str(x1), str(y1), str(x2), str(y2), str(ms)]
    if action == "tap":
        x, y = (int(a) for a in args)
        return base + ["tap", str(x), str(y)]
    if action == "text":
        txt = str(args[0])
        if not re.fullmatch(r"[A-Za-z0-9 .,@_-]{1,200}", txt):
            raise ValueError("text contains characters that are not typed directly; use the phone keyboard")
        return base + ["text", txt.replace(" ", "%s")]
    raise ValueError(f"unknown action {action!r}")


def adb_pair_argv(host_port: str) -> list[str]:
    """`adb pair HOST:PORT` (the pairing code is entered on stdin by the caller, never put in argv or logs)."""
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}:\d{2,5}", host_port):
        raise ValueError("expected IPv4 HOST:PORT shown on the phone's Wireless debugging pairing screen")
    return ["adb", "pair", host_port]


def adb_connect_argv(host_port: str) -> list[str]:
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}:\d{2,5}", host_port):
        raise ValueError("expected IPv4 HOST:PORT")
    return ["adb", "connect", host_port]


# ---------------------------------------------------------------------------
# Feature probe with specific recovery steps
# ---------------------------------------------------------------------------

def parse_scrcpy_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"scrcpy\s+(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x) for x in m.groups(default="0")) if m else None


def probe_features(scrcpy_help: str, scrcpy_version: str, adb_present: bool) -> dict:
    """Decide what the installed scrcpy/adb can do. Returns {'supports': {...}, 'problems': [...], 'version': ...}."""
    problems: list[str] = []
    if not adb_present:
        problems.append("adb is missing. Arch: pacman -S android-tools android-udev. Debian: apt-get install adb.")
    ver = parse_scrcpy_version(scrcpy_version)
    if not scrcpy_help.strip():
        problems.append("scrcpy is missing, so there is no live video. Arch: pacman -S scrcpy. Debian 13: apt-get install -t "
                        "trixie-backports scrcpy (it is not in trixie itself). RustDesk (paired network session) is the alternative.")
    sup = {
        "no-audio": "--no-audio" in scrcpy_help,
        "no-control": "--no-control" in scrcpy_help,
        "window-position": all(f in scrcpy_help for f in ("--window-x", "--window-width", "--window-borderless")),
        "serial": "--serial" in scrcpy_help,
        "max-fps": "--max-fps" in scrcpy_help,
    }
    if scrcpy_help.strip():
        if not sup["no-control"]:
            problems.append("This scrcpy has no --no-control, so view-only tiles are impossible. Upgrade scrcpy to 2.x.")
        if not sup["serial"]:
            problems.append("This scrcpy has no --serial; it cannot address several phones. Upgrade scrcpy to 2.x.")
        if not sup["window-position"]:
            problems.append("This scrcpy cannot position windows, so tiles open as separate windows. Upgrade scrcpy to 2.x for tiling.")
        if not sup["no-audio"]:
            problems.append("This scrcpy predates --no-audio (added in 2.0); audio flags are skipped and audio may start.")
    return {"supports": sup, "problems": problems, "version": ver, "ready": bool(scrcpy_help.strip()) and adb_present
            and sup["serial"] and sup["no-control"]}


def session_type(env: dict[str, str]) -> str:
    t = (env.get("XDG_SESSION_TYPE") or "").lower()
    if t in ("x11", "wayland"):
        return t
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if env.get("DISPLAY") else "none"
