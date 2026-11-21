#!/usr/bin/env python3
"""PC microphone handling for the VictorLinux Phone dashboard.

Sources come from the running desktop audio system through `pactl` (PipeWire's pipewire-pulse or PulseAudio).
Parsing and policy are pure; subprocess access is injected so everything is tested with recorded fixtures.
Reference for the PipeWire side: https://pipewire.pages.freedesktop.org/wireplumber/man/wpctl.html
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

CONFIG_NAME = "phone-dash.json"
PROFILE_SUFFIX = re.compile(r"\.(analog-stereo|analog-mono|mono-fallback|iec958-stereo|multichannel-input|"
                            r"headset-head-unit(?:-msbc)?|handsfree-head-unit|a2dp-source|stereo-fallback|"
                            r"pro-input-\d+|analog-surround-\d+|digital-stereo|hdmi-stereo[\w-]*)$")


@dataclass(frozen=True)
class Source:
    name: str                 # pulse source name; what capture uses
    stable_id: str            # survives reboot and re-plug
    description: str
    kind: str                 # usb | bluetooth | builtin | headset-jack | other
    muted: bool
    state: str
    is_default: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def is_monitor(src: dict) -> bool:
    """Playback monitors are never microphones and are never auto-selected."""
    props = src.get("properties") or {}
    return (str(src.get("name", "")).endswith(".monitor") or props.get("device.class") == "monitor"
            or src.get("monitor_of_sink") not in (None, "", "n/a"))


def stable_id(src: dict) -> str:
    """Identity independent of the numeric index and of the active card profile.

    USB: vendor/product/serial (falls back to the pulse name, which already embeds them). Bluetooth: MAC.
    Built-in: the PCI/ALSA path plus the port, so the headset jack and the internal mic stay distinct."""
    props = src.get("properties") or {}
    name = str(src.get("name", ""))
    bus = props.get("device.bus", "")
    if name.startswith("bluez_input.") or props.get("api.bluez5.address"):
        mac = props.get("api.bluez5.address") or name.split(".")[1]
        return "bt:" + mac.replace("_", ":").upper()
    if bus == "usb" or ".usb-" in name:
        vid, pid, ser = props.get("device.vendor.id", ""), props.get("device.product.id", ""), props.get("device.serial", "")
        if vid and pid:
            return f"usb:{vid}:{pid}:{ser}" if ser else f"usb:{vid}:{pid}:{props.get('api.alsa.path', '')}"
        return "usb:" + PROFILE_SUFFIX.sub("", name.split("alsa_input.", 1)[-1])
    port = src.get("active_port") or ""
    return "pci:" + PROFILE_SUFFIX.sub("", name.split("alsa_input.", 1)[-1]) + (f"#{port}" if port else "")


def kind_of(src: dict) -> str:
    props = src.get("properties") or {}
    name = str(src.get("name", ""))
    port = str(src.get("active_port") or "")
    if name.startswith("bluez_input.") or props.get("api.bluez5.address"):
        return "bluetooth"
    if props.get("device.bus") == "usb" or ".usb-" in name:
        return "usb"
    if "headset" in port or "headphone" in port:
        return "headset-jack"
    if props.get("device.form_factor") == "internal" or name.startswith("alsa_input.pci"):
        return "builtin"
    return "other"


def parse_pactl_sources(text: str, default_source: str = "") -> list[Source]:
    """Parse `pactl -f json list sources`. Monitors are dropped. Raises ValueError on garbage."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"pactl output is not JSON: {e}") from e
    out: list[Source] = []
    for s in raw:
        if is_monitor(s):
            continue
        name = s.get("name", "")
        desc = s.get("description") or name
        out.append(Source(name=name, stable_id=stable_id(s), description=desc, kind=kind_of(s), muted=bool(s.get("mute")),
                          state=str(s.get("state", "")).lower(), is_default=bool(default_source) and name == default_source))
    return out


def parse_subscribe_line(line: str) -> tuple[str, str] | None:
    """`Event 'new' on source #52` -> ('new', 'source'). Only source/card events matter for hot-plug."""
    m = re.match(r"Event '(\w+)' on (source|card|server|source-output)\b", line.strip())
    if not m:
        return None
    ev, kind = m.group(1), m.group(2)
    return (ev, kind) if kind in ("source", "card", "server") else None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

FOLLOW_DEFAULT = "follow_default"
PREFERRED = "preferred"


@dataclass
class Policy:
    mode: str = FOLLOW_DEFAULT
    preferred_id: str = ""
    use_preferred_when_connected: bool = True

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def choose_active(sources: list[Source], policy: Policy) -> tuple[Source | None, str]:
    """Pick the capture source and say why. Never returns a monitor (they are filtered at parse time)."""
    if not sources:
        return None, "No microphone is connected."
    if policy.preferred_id and (policy.mode == PREFERRED or policy.use_preferred_when_connected):
        for s in sources:
            if s.stable_id == policy.preferred_id:
                return s, "preferred microphone connected"
        if policy.mode == PREFERRED:
            return None, "The preferred microphone is not connected."
    for s in sources:
        if s.is_default:
            return s, "system default"
    return sources[0], "first available input (the system has no default input)"


def load_policy(path: str) -> Policy:
    try:
        d = json.load(open(path))["mic"]
        return Policy(d.get("mode", FOLLOW_DEFAULT), d.get("preferred_id", ""), bool(d.get("use_preferred_when_connected", True)))
    except (OSError, ValueError, KeyError, TypeError):
        return Policy()


def save_policy(path: str, p: Policy) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        cur = json.load(open(path))
    except (OSError, ValueError):
        cur = {}
    cur["mic"] = p.as_dict()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=2)
    os.replace(tmp, path)


def config_path(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", "~"), ".config")
    return os.path.join(os.path.expanduser(base), "victorlinux", CONFIG_NAME)


# ---------------------------------------------------------------------------
# Live state machine
# ---------------------------------------------------------------------------

@dataclass
class MicStatus:
    active: Source | None
    reason: str
    connected: list[Source]
    muted: bool
    ready_for_ptt: bool
    message: str


class MicManager:
    """Tracks the active microphone as sources come and go and cancels an utterance whose mic disappears.

    `listing` returns (pactl_json_text, default_source_name). It is called on every hot-plug event."""

    def __init__(self, listing: Callable[[], tuple[str, str]], policy: Policy, on_change: Callable[[MicStatus], None] | None = None,
                 on_cancel: Callable[[str], None] | None = None):
        self.listing, self.policy = listing, policy
        self.on_change, self.on_cancel = on_change, on_cancel
        self.status = MicStatus(None, "", [], False, False, "No microphone is connected.")
        self.utterance_source: str | None = None       # stable_id of the mic an utterance is using
        self._first = True                              # always publish the first status so the UI shows it
        self.lock = threading.Lock()

    def refresh(self) -> MicStatus:
        try:
            text, default = self.listing()
            sources = parse_pactl_sources(text, default)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            st = MicStatus(None, "", [], False, False, f"The audio system is not reachable: {e}. Is PipeWire or PulseAudio running?")
            self._publish(st)
            return st
        act, why = choose_active(sources, self.policy)
        if act is None:
            msg = why
        else:
            msg = f"{act.description} ({act.kind}) - {why}" + (" - MUTED" if act.muted else "")
        st = MicStatus(act, why, sources, bool(act and act.muted), bool(act and not act.muted), msg)
        with self.lock:
            used = self.utterance_source
        if used and (act is None or act.stable_id != used):
            with self.lock:
                self.utterance_source = None
            if self.on_cancel:
                self.on_cancel("The microphone was disconnected, so the command was cancelled.")
        self._publish(st)
        return st

    def _publish(self, st: MicStatus) -> None:
        changed = (st.active.stable_id if st.active else None, st.muted, tuple(s.stable_id for s in st.connected)) != (
            self.status.active.stable_id if self.status.active else None, self.status.muted, tuple(s.stable_id for s in self.status.connected))
        first, self._first = self._first, False
        self.status = st
        if (changed or first) and self.on_change:
            self.on_change(st)

    def begin_utterance(self) -> Source:
        st = self.refresh()
        if not st.ready_for_ptt or st.active is None:
            raise RuntimeError(st.message if not st.muted else "The microphone is muted.")
        with self.lock:
            self.utterance_source = st.active.stable_id
        return st.active

    def end_utterance(self) -> None:
        with self.lock:
            self.utterance_source = None


# ---------------------------------------------------------------------------
# System access (thin; exercised on a real host, faked in tests)
# ---------------------------------------------------------------------------

def pactl_listing() -> tuple[str, str]:
    src = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=5, check=True).stdout
    try:
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=5).stdout.strip()
    except subprocess.SubprocessError:
        default = ""
    return src, default


def set_mute(source_name: str, muted: bool) -> None:
    subprocess.run(["pactl", "set-source-mute", source_name, "1" if muted else "0"], check=True, timeout=5)


def capture_argv(source_name: str, rate: int = 16000) -> list[str]:
    return ["parec", f"--device={source_name}", "--format=s16le", f"--rate={rate}", "--channels=1", "--latency-msec=50"]


def pcm_level(chunk: bytes) -> float:
    """0..1 RMS level of s16le PCM, for the input meter."""
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, chunk[: n * 2])
    return min(1.0, math.sqrt(sum(s * s for s in samples) / n) / 32768.0 * 4)


class HotplugWatcher(threading.Thread):
    """Runs `pactl subscribe` and calls `on_event` on source/card/server changes. Falls back to polling every 2 s."""

    def __init__(self, on_event: Callable[[], None], popen=subprocess.Popen):
        super().__init__(daemon=True)
        self.on_event, self.popen, self._halt = on_event, popen, threading.Event()
        self.proc = None
        self.mode = "subscribe"

    def run(self) -> None:
        try:
            self.proc = self.popen(["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self.mode = "poll"
        if self.proc and self.proc.stdout:
            for line in self.proc.stdout:
                if self._halt.is_set():
                    return
                if parse_subscribe_line(line):
                    self.on_event()
            if not self._halt.is_set():
                self.mode = "poll"
        while not self._halt.wait(2.0):
            self.on_event()

    def stop(self) -> None:
        self._halt.set()
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
