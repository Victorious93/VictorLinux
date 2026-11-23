"""Tile model for the three-phone dashboard: layout, focus, per-tile input epochs and coordinate mapping.

Pure logic. The GUI draws tiles from this model and sends input only through route_input(), which targets
exactly one device and discards events whose epoch is stale (queued before a reconnect/replace/rotation)."""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_TILES = 3


@dataclass
class Tile:
    device_id: str
    nickname: str
    permissions: str = "view"        # view | control
    state: str = "connecting"        # connecting | live | reconnecting | disconnected
    epoch: int = 0                   # bumped on reconnect, replace, rotation; events carry the epoch they were made for
    width: int = 1080
    height: int = 2400
    rotation: int = 0                # 0/90/180/270 as reported by the device


class FourthPhone(Exception):
    def __init__(self, tiles: list[str]):
        super().__init__("three screens are showing; choose which tile to replace")
        self.tiles = tiles


class TileManager:
    def __init__(self) -> None:
        self.tiles: list[Tile] = []
        self.focus: str = ""
        self.input_held: str = ""    # device currently receiving manual input ("" = released)

    def add(self, device_id: str, nickname: str, replace: str = "") -> Tile:
        if any(t.device_id == device_id for t in self.tiles):
            return self.get(device_id)
        if len(self.tiles) >= MAX_TILES:
            if not replace:
                raise FourthPhone([t.device_id for t in self.tiles])
            self.remove(replace)
        t = Tile(device_id, nickname)
        self.tiles.append(t)
        if not self.focus:
            self.focus = device_id
        return t

    def get(self, device_id: str) -> Tile:
        return next(t for t in self.tiles if t.device_id == device_id)

    def remove(self, device_id: str) -> None:
        self.tiles = [t for t in self.tiles if t.device_id != device_id]
        if self.focus == device_id:
            self.focus = self.tiles[0].device_id if self.tiles else ""
        if self.input_held == device_id:
            self.input_held = ""

    def set_state(self, device_id: str, state: str) -> None:
        """A reconnect only touches that tile: its epoch advances (stale input dies) and the others are untouched."""
        t = self.get(device_id)
        if t.state != state:
            t.state = state
            if state in ("reconnecting", "disconnected") or state == "live":
                t.epoch += 1
        if state != "live" and self.input_held == device_id:
            self.input_held = ""

    def rotate(self, device_id: str, rotation: int, width: int, height: int) -> None:
        t = self.get(device_id)
        t.rotation, t.width, t.height = rotation % 360, width, height
        t.epoch += 1

    def layout(self, w: int, h: int, gap: int = 8) -> dict[str, tuple[int, int, int, int]]:
        """(x, y, width, height) per tile in a w x h window. Focused tile is enlarged when there are 3."""
        n = len(self.tiles)
        if n == 0:
            return {}
        if n == 1:
            return {self.tiles[0].device_id: (0, 0, w, h)}
        if n == 2:
            cw = (w - gap) // 2
            return {t.device_id: (i * (cw + gap), 0, cw, h) for i, t in enumerate(self.tiles)}
        main = next((t for t in self.tiles if t.device_id == self.focus), self.tiles[0])
        rest = [t for t in self.tiles if t is not main]
        mw = (w - gap) * 3 // 5
        sw = w - gap - mw
        sh = (h - gap) // 2
        out = {main.device_id: (0, 0, mw, h)}
        for i, t in enumerate(rest):
            out[t.device_id] = (mw + gap, i * (sh + gap), sw, sh)
        return out

    def set_focus(self, device_id: str) -> None:
        self.get(device_id)
        self.focus = device_id

    def grant_input(self, device_id: str) -> None:
        t = self.get(device_id)
        if t.permissions != "control" or t.state != "live":
            raise PermissionError("input is not granted for this phone")
        self.input_held = device_id

    def release_input(self) -> None:
        self.input_held = ""

    def route_input(self, target: str, epoch: int, event: dict) -> tuple[str, dict] | None:
        """Return (device_id, event) to send, or None if it must be dropped. Never fans out to another device."""
        try:
            t = self.get(target)
        except StopIteration:
            return None
        if t.state != "live" or t.permissions != "control" or epoch != t.epoch:
            return None
        if event.get("kind") in ("touch", "swipe", "key", "scroll", "text") and self.input_held != target:
            return None
        return target, event


def map_point(px: float, py: float, rect: tuple[int, int, int, int], dev_w: int, dev_h: int, rotation: int = 0) -> tuple[int, int] | None:
    """Window pixel -> device pixel for a letterboxed tile. dev_w/dev_h are the device's CURRENT (post-rotation)
    dimensions; `rotation` is kept for the caller's bookkeeping since scrcpy/RustDesk already deliver rotated frames."""
    x, y, w, h = rect
    scale = min(w / dev_w, h / dev_h)
    cw, ch = dev_w * scale, dev_h * scale
    ox, oy = x + (w - cw) / 2, y + (h - ch) / 2
    if not (ox <= px < ox + cw and oy <= py < oy + ch):
        return None                    # click in the letterbox bars: ignored
    return int((px - ox) / scale), int((py - oy) / scale)


def scrcpy_tile_argvs(mgr: TileManager, width: int, height: int, origin: tuple[int, int] = (0, 0),
                      view_only: bool = True) -> dict[str, list[str]]:
    """One scrcpy command per tile, placed with scrcpy's own --window-x/-y/-width/-height so the phones appear
    side by side as one dashboard. Each device is addressed by --serial, so input to one window can only reach that
    phone; tiles are view-only (--no-control) unless the owner granted control. scrcpy windows are separate OS windows
    laid out by this model, not widgets inside one Tk window (embedding is not implemented)."""
    from victorphone import scrcpy_argv
    out: dict[str, list[str]] = {}
    for dev, (x, y, w, h) in mgr.layout(width, height).items():
        t = mgr.get(dev)
        extra = [f"--window-x={origin[0] + x}", f"--window-y={origin[1] + y}", f"--window-width={w}", f"--window-height={h}",
                 "--window-borderless"]
        if view_only or t.permissions != "control":
            extra.append("--no-control")
        out[dev] = scrcpy_argv(window_title=f"{t.nickname} · VictorLinux", serial=dev, no_audio=True, extra=extra)
    return out
