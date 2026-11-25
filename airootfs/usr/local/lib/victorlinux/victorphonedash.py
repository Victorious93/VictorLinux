"""Phone GUI dashboard: push-to-talk and up to three live Android screens in one window.

Work by Victorious. Logic here is GUI-free and unit-tested; victorphonedashgui.py draws it with Tk.

Push-to-talk
  * The source is the one chosen in Settings > Audio > Microphone (victormic.resolve): a PC input is captured with
    victorptt.pc_capture_argv(); a phone microphone is read from the receiver's local virtual source
    (`victor-mic serve --virtual-mic` publishes `victor_phone_mic`), so audio never leaves the VMIC1 session path.
  * The command is bound to the tile focused when the button/shortcut went down (victorptt.PttSession) and is
    dispatched ONLY through victordispatch.dispatch, the same gate the manual buttons use.
  * Audio is held in memory only and is never logged. A missing recogniser/model shows install steps.

Screens
  * victorscreens.TileManager owns layout, focus, input grant and per-tile epochs.
  * TileRunner keeps one scrcpy process per tile. Only the tile holding the input grant runs with control; every
    other tile is started with --no-control, so keyboard/click/drag/scroll can reach exactly one phone.
  * A tile whose stream drops is restarted alone (its epoch advances, its input grant is dropped); the other
    streams keep running.
  * Embedding: on X11 the borderless scrcpy window is reparented into the tile frame with xdotool (scrcpy has no
    parent-window option). On Wayland, or without xdotool, the tiles are placed as separate windows with scrcpy's
    own --window-x/-y/-width/-height (victorscreens.scrcpy_tile_argvs). See docs/phone-microphone.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import victordispatch as vd
import victorptt as vp
import victorscreens as vs
import victorvoice as vv

PHONE_VIRTUAL_SOURCE = "victor_phone_mic"

# Load profiles. "low" is meant for an 8 GB PC driving three phones.
PROFILES: dict[str, list[str]] = {
    "normal": ["--max-size=1024", "--max-fps=30", "--video-bit-rate=4M"],
    "low": ["--max-size=720", "--max-fps=15", "--video-bit-rate=2M", "--video-codec=h264"],
}

STT_INSTALL_HELP = (
    "No offline speech model is installed, so push-to-talk is off. Buttons and shortcuts keep working.\n"
    "To enable it (nothing is downloaded automatically):\n"
    "  1. Install the recogniser:  pip install --user vosk\n"
    "  2. Download a model from the official Vosk page https://alphacephei.com/vosk/models\n"
    "     (for example vosk-model-small-en-us-0.15.zip, about 40 MB)\n"
    "  3. Unzip it into ~/.local/share/victorlinux/stt/  (or set VICTOR_STT_MODEL to the model folder)\n"
    "  4. Reopen this window."
)


# ------------------------------------------------------------------------------------------------ PTT source

@dataclass
class CapturePlan:
    argv: list[str] | None      # None => push-to-talk cannot capture right now
    kind: str                   # pc | phone | none
    label: str                  # human description of the delivering source
    help: list[str]             # recovery steps when argv is None


def virtual_source_present(run: Callable[..., subprocess.CompletedProcess] | None = None) -> bool:
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5))
    try:
        r = run(["pactl", "list", "short", "sources"])
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and any(line.split("\t")[1:2] == [PHONE_VIRTUAL_SOURCE] for line in (r.stdout or "").splitlines())


def capture_plan(settings: dict[str, Any], pc_sources: list, default_name: str, reg: Any,
                 phone_source_present: bool) -> CapturePlan:
    """Map the owner's microphone choice to a capture command. Never substitutes another source silently."""
    import victormic as vm
    res = vm.resolve(settings, pc_sources, default_name, reg)
    if res.delivering_kind == "pc":
        src = next((p for p in pc_sources if p.stable_id == res.delivering_ref), None)
        if src is None:
            return CapturePlan(None, "none", "", ["The chosen PC microphone is not available"])
        return CapturePlan(vp.pc_capture_argv(src.name), "pc", res.delivering, [])
    if res.delivering_kind == "phone":
        if not phone_source_present:
            return CapturePlan(None, "none", res.delivering,
                               ["The phone microphone is connected, but its local feed is off.",
                                "Start the receiver with --virtual-mic (victor-mic serve … --virtual-mic) and try again."])
        return CapturePlan(vp.pc_capture_argv(PHONE_VIRTUAL_SOURCE), "phone", res.delivering, [])
    return CapturePlan(None, "none", "", res.recovery or ["No microphone is available"])


def recognizer_status(model_dir: str | None = None) -> tuple[bool, str]:
    """(ready, message). ready=False comes with install steps; the dashboard stays usable."""
    st = vv.model_status(model_dir)
    if not st["installed"]:
        return False, STT_INSTALL_HELP
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False, STT_INSTALL_HELP
    return True, f"Speech model: {os.path.basename(st['path'].rstrip('/'))}"


def phones_from_tiles(mgr: vs.TileManager) -> list[vv.Phone]:
    return [vv.Phone(t.device_id, t.nickname, i + 1) for i, t in enumerate(mgr.tiles)]


class PttController:
    """Button/shortcut down -> capture from the selected source; up -> recognise -> victordispatch."""

    def __init__(self, mgr: vs.TileManager, recognizer: vp.Recognizer | None, plan_fn: Callable[[], CapturePlan],
                 popen: Callable[..., Any] | None = None, guard: vv.EchoGuard | None = None,
                 read_fn: Callable[[], str] | None = None, run: Callable[..., Any] | None = None):
        self.mgr, self.rec, self.plan_fn = mgr, recognizer, plan_fn
        self.popen = popen or (lambda argv: subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
        self.guard = guard or vv.EchoGuard()
        self.read_fn, self.run = read_fn, run
        self.session: vp.PttSession | None = None
        self.proc: Any = None
        self._reader: threading.Thread | None = None
        self.source_label = ""

    @property
    def active(self) -> bool:
        return bool(self.session and self.session.active)

    def press(self) -> str:
        """Returns a status line for the GUI. Binds the focus at this instant."""
        if self.active:
            return "Listening…"
        if self.rec is None:
            return STT_INSTALL_HELP.splitlines()[0]
        plan = self.plan_fn()
        if plan.argv is None:
            return "Push-to-talk unavailable: " + " ".join(plan.help)
        self.session = vp.PttSession(self.rec, phones_from_tiles(self.mgr), lambda: self.mgr.focus, self.guard)
        self.session.press()
        self.source_label = plan.label
        try:
            self.proc = self.popen(plan.argv)
        except OSError as e:
            self.session.cancel()
            return f"Push-to-talk unavailable: cannot start capture ({e.__class__.__name__})"
        self._reader = threading.Thread(target=self._pump, args=(self.proc, self.session), daemon=True)
        self._reader.start()
        tgt = next((t.nickname for t in self.mgr.tiles if t.device_id == self.session.focus_at_start), "no phone")
        return f"Listening on {plan.label} for {tgt}…"

    def _pump(self, proc: Any, ses: vp.PttSession) -> None:
        out = getattr(proc, "stdout", None)
        if out is None:
            return
        while ses.active:
            chunk = out.read(640)            # 20 ms at 16 kHz mono s16le
            if not chunk:
                if ses.active and proc is self.proc:
                    ses.cancel()             # source vanished mid-utterance: never complete from other audio
                return
            ses.feed(chunk)

    def _stop_capture(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def cancel(self) -> None:
        if self.session:
            self.session.cancel()
        self._stop_capture()

    def release(self) -> vd.Outcome:
        ses = self.session
        if ses is None or not ses.active:
            self._stop_capture()
            return vd.Outcome(False, "Nothing was recorded")
        self._stop_capture()
        if self._reader:
            self._reader.join(timeout=1)
        result = ses.release()
        return vd.dispatch(result, self.mgr, self.read_fn, self.run)


# ------------------------------------------------------------------------------------------------ screens

def tile_title(t: vs.Tile) -> str:
    return f"{t.nickname} · VictorLinux [{t.device_id}]"


def tile_argv(t: vs.Tile, control: bool, profile: str = "normal", placement: list[str] | None = None) -> list[str]:
    from victorphone import scrcpy_argv
    extra = ["--window-borderless", *PROFILES.get(profile, PROFILES["normal"])]
    if not control:
        extra.append("--no-control")
    if placement:
        extra.extend(placement)
    return scrcpy_argv(window_title=tile_title(t), serial=t.device_id, no_audio=True, extra=extra)


class TileRunner:
    """One scrcpy per tile. Control only for the input holder. Restarts a dropped tile without touching others."""

    RESTART_BACKOFF_S = (1.0, 2.0, 4.0, 8.0)

    def __init__(self, mgr: vs.TileManager, popen: Callable[..., Any] | None = None, profile: str = "normal",
                 clock: Callable[[], float] = time.monotonic,
                 placement_fn: Callable[[vs.Tile], list[str]] | None = None):
        self.mgr = mgr
        self.popen = popen or (lambda argv: subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.profile, self.clock, self.placement_fn = profile, clock, placement_fn
        self.procs: dict[str, Any] = {}
        self.control: dict[str, bool] = {}
        self.started_at: dict[str, float] = {}
        self.retry_at: dict[str, float] = {}
        self.failures: dict[str, int] = {}

    def wants_control(self, dev: str) -> bool:
        t = self.mgr.get(dev)
        return self.mgr.input_held == dev and t.permissions == "control" and t.state == "live"

    def _kill(self, dev: str) -> None:
        p = self.procs.pop(dev, None)
        self.control.pop(dev, None)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def _start(self, t: vs.Tile, control: bool) -> None:
        placement = self.placement_fn(t) if self.placement_fn else None
        self.procs[t.device_id] = self.popen(tile_argv(t, control, self.profile, placement))
        self.control[t.device_id] = control
        self.started_at[t.device_id] = self.clock()

    def sync(self) -> list[str]:
        """Reconcile processes with the model. Returns device ids whose process was (re)started."""
        changed: list[str] = []
        ids = {t.device_id for t in self.mgr.tiles}
        for dev in list(self.procs):
            if dev not in ids:
                self._kill(dev)
        now = self.clock()
        for t in list(self.mgr.tiles):
            dev = t.device_id
            if t.state == "disconnected":
                self._kill(dev)
                continue
            p = self.procs.get(dev)
            if p is not None and p.poll() is not None:          # stream dropped: only this tile reconnects
                self.procs.pop(dev, None)
                self.control.pop(dev, None)
                self.mgr.set_state(dev, "reconnecting")          # epoch++, input grant dropped
                n = self.failures.get(dev, 0)
                self.retry_at[dev] = now + self.RESTART_BACKOFF_S[min(n, len(self.RESTART_BACKOFF_S) - 1)]
                self.failures[dev] = n + 1
                p = None
            if p is None:
                if now < self.retry_at.get(dev, 0.0):
                    continue
                if t.state == "live":
                    t.state = "connecting"
                self._start(t, False if t.state != "live" else self.wants_control(dev))
                changed.append(dev)
                continue
            want = self.wants_control(dev)
            if self.control.get(dev) != want:                    # grant or release: restart that tile only
                self._kill(dev)
                t.epoch += 1                                     # input queued for the old process is stale
                self._start(t, want)
                changed.append(dev)
            elif t.state in ("connecting", "reconnecting") and now - self.started_at.get(dev, now) >= 1.5:
                self.mgr.set_state(dev, "live")
                self.failures[dev] = 0
        return changed

    def stop_all(self) -> None:
        for dev in list(self.procs):
            self._kill(dev)


def add_phone(mgr: vs.TileManager, device_id: str, nickname: str, replace: str = "") -> vs.Tile:
    """Wrapper so the GUI and CLI share the fourth-phone rule: raises vs.FourthPhone listing the tiles to choose."""
    return mgr.add(device_id, nickname, replace)


# ------------------------------------------------------------------------------------------------ X11 embedding

def embedding_supported(env: dict[str, str] | None = None, which: Callable[[str], str | None] = shutil.which) -> tuple[bool, str]:
    env = os.environ if env is None else env
    if env.get("WAYLAND_DISPLAY") and not env.get("VICTOR_FORCE_X11"):
        return False, "Wayland session: compositors do not let one app place another app's window inside its own, so phones open as separate, tiled windows."
    if not env.get("DISPLAY"):
        return False, "No X11 display."
    if not which("xdotool"):
        return False, "xdotool is not installed, so phones open as separate, tiled windows (install xdotool to embed them)."
    return True, "X11: phone windows are embedded in the dashboard."


def find_window(title: str, run: Callable[..., subprocess.CompletedProcess] | None = None) -> str:
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=3))
    import re
    try:
        r = run(["xdotool", "search", "--name", "^" + re.escape(title) + "$"])
    except (OSError, subprocess.SubprocessError):
        return ""
    ids = (r.stdout or "").split()
    return ids[-1] if r.returncode == 0 and ids else ""


def embed_argvs(wid: str, parent: int, w: int, h: int) -> list[list[str]]:
    return [["xdotool", "windowreparent", wid, str(parent)],
            ["xdotool", "windowmove", wid, "0", "0"],
            ["xdotool", "windowsize", wid, str(max(1, w)), str(max(1, h))]]
