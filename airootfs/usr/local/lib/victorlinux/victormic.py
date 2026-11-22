"""VictorLinux microphone selection: one settings model for the GUI and the CLI (victor-mic).

Choices: follow the system default, a specific PC input, a phone via DroidCommand-AI, or a phone via
Victorious Keyboard. The microphone source is independent of whichever phone is being controlled.

Phone audio arrives over the VMIC1 stream (TLS, pinned certificate, HMAC session bound to PC, phone and
provider; see docs/phone-microphone.md). Frames are kept in a bounded in-memory buffer only: never written to
disk, never logged, never put in the fleet job queue.

Pure logic and subprocess wrappers only (no GUI imports) so everything is unit-testable.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

PROVIDERS = {"droidcommand": "DroidCommand-AI", "keyboard": "Victorious Keyboard"}
SAMPLE_RATE = 16000                    # mono, signed 16-bit little endian
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
MAX_BUFFER_MS = 400                    # live buffer bound; older audio is dropped, never queued
MAX_FRAME_PAYLOAD = 64 * 1024
IDLE_TIMEOUT_S = 5.0                   # no frame for this long => the phone is treated as disconnected
SESSION_MAX_TTL_S = 8 * 3600           # consent never outlives this

# ---------------------------------------------------------------------------------------------- settings

DEFAULTS: dict[str, Any] = {
    "mode": "default",                 # default | pc | phone
    "pc_stable_id": "",                # for mode=pc
    "phone": {"device_id": "", "provider": ""},   # for mode=phone
    "preferred_phone": {"device_id": "", "provider": ""},
    "fallback_to_preferred_phone": False,
    "virtual_mic": False,              # publish the phone as a user-session virtual microphone
    "virtual_mic_set_default": False,  # separate owner choice; never implied by virtual_mic
    "muted": False,
}


def settings_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "victorlinux", "mic.json")


def _clean_pair(v: Any) -> dict[str, str]:
    if not isinstance(v, dict):
        return {"device_id": "", "provider": ""}
    prov = str(v.get("provider", ""))
    return {"device_id": str(v.get("device_id", ""))[:96], "provider": prov if prov in PROVIDERS else ""}


def normalize(raw: Any) -> dict[str, Any]:
    s = json.loads(json.dumps(DEFAULTS))
    if isinstance(raw, dict):
        if raw.get("mode") in ("default", "pc", "phone"):
            s["mode"] = raw["mode"]
        s["pc_stable_id"] = str(raw.get("pc_stable_id", ""))[:256]
        s["phone"] = _clean_pair(raw.get("phone"))
        s["preferred_phone"] = _clean_pair(raw.get("preferred_phone"))
        for k in ("fallback_to_preferred_phone", "virtual_mic", "virtual_mic_set_default", "muted"):
            s[k] = bool(raw.get(k, False))
    if s["mode"] == "phone" and not (s["phone"]["device_id"] and s["phone"]["provider"]):
        s["mode"] = "default"
    if s["mode"] == "pc" and not s["pc_stable_id"]:
        s["mode"] = "default"
    return s


def load_settings(path: str | None = None) -> dict[str, Any]:
    try:
        with open(path or settings_path(), encoding="utf-8") as fh:
            return normalize(json.load(fh))
    except (OSError, ValueError):
        return normalize(None)


def save_settings(s: dict[str, Any], path: str | None = None) -> None:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(normalize(s), fh, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------- PC sources

@dataclass(frozen=True)
class PcSource:
    stable_id: str          # survives re-plugging: serial/bus path, not the volatile numeric index
    name: str               # audio-system node name
    label: str              # human description
    kind: str               # builtin | usb | headset | bluetooth | other
    is_default: bool = False


def _classify(props: dict[str, str], name: str, desc: str) -> str:
    bus = (props.get("device.bus") or "").lower()
    api = (props.get("device.api") or "").lower()
    ff = (props.get("device.form_factor") or "").lower()
    if api == "bluez" or bus == "bluetooth" or name.startswith("bluez"):
        return "bluetooth"
    if ff in ("headset", "headphone", "handsfree") or "headset" in desc.lower() or "headphone" in props.get("port.name", "").lower():
        return "headset" if bus != "usb" else "usb"
    if bus == "usb":
        return "usb"
    if ff == "internal" or bus in ("pci", "") and "analog" in name:
        return "builtin"
    return "other"


def parse_pactl_sources(text: str, default_name: str = "") -> list[PcSource]:
    """Parse `pactl -f json list sources`. Monitor sources (loopbacks of outputs) and our own virtual mic are skipped."""
    try:
        items = json.loads(text)
    except ValueError:
        return []
    out: list[PcSource] = []
    for it in items if isinstance(items, list) else []:
        name = str(it.get("name", ""))
        props = it.get("properties") or {}
        if not name or name.endswith(".monitor") or it.get("monitor_of_sink") not in (None, "n/a", 4294967295):
            continue
        if name == VirtualMic.SOURCE_NAME or props.get("device.class") == "monitor":
            continue
        desc = str(it.get("description") or props.get("device.description") or name)
        serial = props.get("device.serial") or props.get("device.bus-path") or props.get("api.bluez5.address")
        stable = f"{props.get('device.vendor.id', '')}:{props.get('device.product.id', '')}:{serial}" if serial else name
        out.append(PcSource(stable, name, desc, _classify(props, name, desc), name == default_name))
    return out


def list_pc_sources(run: Callable[..., subprocess.CompletedProcess] | None = None) -> tuple[list[PcSource], str]:
    """(sources, default_source_name). Empty list when no audio server answers (headless box, no PC mic)."""
    run = run or _run
    try:
        r = run(["pactl", "-f", "json", "list", "sources"])
        d = run(["pactl", "get-default-source"])
    except (OSError, subprocess.SubprocessError):
        return [], ""
    default = (d.stdout or "").strip() if d.returncode == 0 else ""
    return (parse_pactl_sources(r.stdout, default) if r.returncode == 0 else []), default


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=kw.pop("timeout", 10), **kw)


# ------------------------------------------------------------------------------------- phone sessions / consent

@dataclass
class PhoneSession:
    session_id: str
    device_id: str
    nickname: str
    provider: str
    pc_id: str
    secret: bytes = field(repr=False, default=b"")
    expires_at: float = 0.0            # consent expiry (monotonic-independent wall time)
    live: bool = False                 # an authenticated stream is currently connected
    sharing: str = "idle"              # idle | sharing | muted | silenced | stopped
    last_frame_at: float = 0.0
    revoked: bool = False
    allow_read: bool = False           # separate opt-in: phone may ask this PC to read its screen text aloud

    def consent_valid(self, now: float) -> bool:
        return not self.revoked and now < self.expires_at

    def label(self) -> str:
        return f"{self.nickname} · {PROVIDERS.get(self.provider, self.provider)}"


class SessionRegistry:
    """In-memory only. Consent granted by the phone owner (via fleet messaging) lives here until it expires;
    a stored preference never creates a session, so it cannot restart capture after expiry or reboot."""

    def __init__(self, pc_id: str, clock: Callable[[], float] = time.time):
        self.pc_id = pc_id
        self.clock = clock
        self._s: dict[str, PhoneSession] = {}
        self._lock = threading.Lock()

    def offer(self, device_id: str, nickname: str, provider: str, ttl_s: float = 3600, allow_read: bool = False) -> PhoneSession:
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        ttl = max(30.0, min(float(ttl_s), SESSION_MAX_TTL_S))
        with self._lock:
            for old in self._s.values():
                # Explicit handover: one handset has one microphone, so a new offer for the same phone releases the
                # previous owner (either the same app re-offering or the other app). The old stream is cut at once.
                if old.device_id == device_id and not old.revoked:
                    old.revoked = True
                    old.sharing = "stopped"
            s = PhoneSession(secrets.token_hex(12), device_id, nickname, provider, self.pc_id,
                             secrets.token_bytes(32), self.clock() + ttl, allow_read=allow_read)
            self._s[s.session_id] = s
            return s

    def get(self, session_id: str) -> PhoneSession | None:
        with self._lock:
            return self._s.get(session_id)

    def active(self) -> list[PhoneSession]:
        now = self.clock()
        with self._lock:
            return [s for s in self._s.values() if s.consent_valid(now)]

    def find(self, device_id: str, provider: str) -> PhoneSession | None:
        for s in self.active():
            if s.device_id == device_id and s.provider == provider:
                return s
        return None

    def revoke(self, session_id: str) -> None:
        with self._lock:
            s = self._s.get(session_id)
            if s:
                s.revoked = True
                s.live = False
                s.sharing = "stopped"

    def revoke_phone(self, device_id: str) -> None:
        for s in self.active():
            if s.device_id == device_id:
                self.revoke(s.session_id)

    def sweep(self) -> list[PhoneSession]:
        """Mark stale live sessions disconnected and expired consent stopped. Returns sessions that just dropped."""
        now = self.clock()
        dropped = []
        with self._lock:
            for s in self._s.values():
                if s.live and (not s.consent_valid(now) or now - s.last_frame_at > IDLE_TIMEOUT_S):
                    s.live = False
                    s.sharing = "stopped" if not s.consent_valid(now) else "idle"
                    dropped.append(s)
        return dropped


def status_path() -> str:
    return os.path.join(os.path.dirname(settings_path()), "mic-sessions.json")


def write_status(reg: SessionRegistry, path: str | None = None) -> None:
    """Secret-free snapshot so the GUI/Control Center can show phone sources owned by `victor-mic serve`."""
    path = path or status_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    rows = [{"session_id": x.session_id, "device_id": x.device_id, "nickname": x.nickname, "provider": x.provider,
             "pc_id": x.pc_id, "expires_at": x.expires_at, "live": x.live, "sharing": x.sharing,
             "last_frame_at": x.last_frame_at} for x in reg.active()]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"written_at": reg.clock(), "sessions": rows}, fh)
    os.replace(tmp, path)


def read_status_registry(pc_id: str = "local", path: str | None = None, max_age_s: float = 5.0,
                         clock: Callable[[], float] = time.time) -> SessionRegistry:
    """Rebuild a read-only registry from the snapshot. A stale or missing snapshot (serve not running) yields no
    phones, so a remembered preference cannot appear live."""
    reg = SessionRegistry(pc_id, clock)
    try:
        with open(path or status_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if clock() - float(d["written_at"]) > max_age_s:
            return reg
        for r in d["sessions"]:
            if r.get("provider") in PROVIDERS:
                ses = PhoneSession(str(r["session_id"]), str(r["device_id"]), str(r["nickname"]), r["provider"], str(r["pc_id"]),
                                   b"", float(r["expires_at"]), bool(r["live"]), str(r["sharing"]), float(r["last_frame_at"]))
                reg._s[ses.session_id] = ses
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return reg


# ---------------------------------------------------------------------------------------------- resolution

@dataclass
class Resolution:
    selected: str                       # what the owner chose, human readable
    delivering: str                     # what is actually delivering audio ("" if nothing)
    delivering_kind: str                # pc | phone | none
    delivering_ref: str                 # stable id or session id
    state: str                          # ok | no-source | missing | needs-consent | disconnected | muted | silenced
    recovery: list[str] = field(default_factory=list)
    fallback_used: bool = False


def _phone_state(s: PhoneSession | None, now: float) -> str:
    if s is None or not s.consent_valid(now):
        return "needs-consent"
    if not s.live or s.sharing == "idle":      # a read-only connection is not a microphone
        return "disconnected"
    if s.sharing == "muted":
        return "muted"
    if s.sharing == "silenced":
        return "silenced"
    return "ok"


def resolve(settings: dict[str, Any], pc: list[PcSource], default_name: str, reg: SessionRegistry) -> Resolution:
    """Decide which source delivers audio. A manual PC or phone choice is never silently replaced by hot-plugged
    hardware; only the explicit 'preferred phone' fallback may substitute, and only with live, unexpired consent."""
    now = reg.clock()
    s = normalize(settings)
    pref = s["preferred_phone"]

    def phone_res(pair: dict[str, str], selected_label: str, fallback: bool = False) -> Resolution:
        ses = reg.find(pair["device_id"], pair["provider"])
        st = _phone_state(ses, now)
        label = ses.label() if ses else f"{pair['device_id']} · {PROVIDERS.get(pair['provider'], '?')}"
        rec = {"needs-consent": ["Connect a phone microphone: open the app on the phone and tap Use this phone as microphone / PC microphone"],
               "disconnected": ["Reconnect the phone on the same network", "Or choose a PC microphone"],
               "muted": ["Unmute on the PC or the phone"],
               "silenced": ["Another app or a call is using the phone microphone; end it, then restart sharing"]}.get(st, [])
        return Resolution(selected_label, label if st == "ok" else "", "phone" if st == "ok" else "none",
                          ses.session_id if ses and st == "ok" else "", st, rec, fallback)

    def preferred_phone_ok() -> bool:
        return s["fallback_to_preferred_phone"] and bool(pref["device_id"]) and \
            _phone_state(reg.find(pref["device_id"], pref["provider"]), now) == "ok"

    if s["mode"] == "phone":
        r = phone_res(s["phone"], "phone")
        r.selected = r.delivering or r.selected
        if r.state != "ok":
            r.recovery = r.recovery + (["Or choose Follow system default"] if pc else [])
        return r
    if s["mode"] == "pc":
        src = next((p for p in pc if p.stable_id == s["pc_stable_id"]), None)
        if src:
            return Resolution(src.label, src.label, "pc", src.stable_id, "ok")
        if preferred_phone_ok():
            r = phone_res(pref, "the chosen PC microphone", True)
            r.selected = "the chosen PC microphone (not connected)"
            return r
        return Resolution("the chosen PC microphone", "", "none", "", "missing",
                          ["Plug the microphone back in", "Or choose another microphone in Settings → Audio → Microphone"])
    # default
    d = next((p for p in pc if p.name == default_name), None) or (pc[0] if pc else None)
    if d:
        return Resolution(f"System default ({d.label})", d.label, "pc", d.stable_id, "ok")
    if preferred_phone_ok():
        r = phone_res(pref, "System default (no PC microphone)", True)
        return r
    return Resolution("System default (no input available)", "", "none", "", "no-source",
                      ["Connect a phone microphone", "Or plug in a USB or headset microphone"])


# ---------------------------------------------------------------------------------------------- wire protocol

T_CHALLENGE, T_HELLO, T_HELLO_OK, T_AUDIO, T_CTRL, T_BYE, T_READ_REQ, T_READ_RESP = 0, 1, 2, 3, 4, 5, 6, 7


def pack_frame(ftype: int, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError("frame too large")
    return struct.pack(">IB", len(payload), ftype) + payload


def _recv_exact(sock: Any, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock: Any) -> tuple[int, bytes]:
    n, t = struct.unpack(">IB", _recv_exact(sock, 5))
    if n > MAX_FRAME_PAYLOAD:
        raise ValueError("frame too large")
    return t, _recv_exact(sock, n)


def hello_mac(secret: bytes, pc_id: str, device_id: str, provider: str, session_id: str, nonce: bytes) -> str:
    msg = "|".join(["VMIC1", pc_id, device_id, provider, session_id]).encode() + b"|" + nonce
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


class LiveBuffer:
    """Bounded jitter buffer. push() drops the OLDEST frames past MAX_BUFFER_MS; clear() on disconnect."""

    def __init__(self, max_ms: int = MAX_BUFFER_MS):
        self.max_frames = max(1, max_ms // FRAME_MS)
        self._q: collections.deque[bytes] = collections.deque()
        self._lock = threading.Lock()
        self.dropped = 0

    def push(self, frame: bytes) -> None:
        with self._lock:
            self._q.append(frame)
            while len(self._q) > self.max_frames:
                self._q.popleft()
                self.dropped += 1

    def pop(self) -> bytes | None:
        with self._lock:
            return self._q.popleft() if self._q else None

    def clear(self) -> None:
        with self._lock:
            self._q.clear()

    def __len__(self) -> int:
        return len(self._q)


def peak_level(frame: bytes) -> float:
    """0..1 input-meter value for one PCM16 frame."""
    n = len(frame) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack("<%dh" % n, frame[: n * 2])
    return min(1.0, max(abs(v) for v in vals) / 32768.0)


class MicReceiver:
    """Handles one authenticated phone connection at a time per session. Sinks receive PCM16 frames only while
    the session is live, consented and unmuted. On disconnect or revocation the buffer is cleared and
    `on_utterance_cancel` is called so voice control discards a partial utterance."""

    def __init__(self, reg: SessionRegistry, sinks: Iterable[Callable[[PhoneSession, bytes], None]] = (),
                 on_event: Callable[[str, PhoneSession | None], None] | None = None,
                 on_utterance_cancel: Callable[[PhoneSession], None] | None = None,
                 read_handler: Callable[[], dict] | None = None):
        self.read_handler = read_handler
        self.reg = reg
        self.sinks = list(sinks)
        self.on_event = on_event or (lambda *_: None)
        self.on_utterance_cancel = on_utterance_cancel or (lambda *_: None)
        self.buffers: dict[str, LiveBuffer] = {}
        self.level = 0.0
        self.pc_muted = False

    def serve(self, conn: Any) -> PhoneSession | None:
        """Run the protocol on an already-TLS-wrapped connection. Returns the session (None if auth failed)."""
        ses: PhoneSession | None = None
        try:
            nonce = secrets.token_bytes(16)
            conn.sendall(pack_frame(T_CHALLENGE, nonce))
            t, body = read_frame(conn)
            if t != T_HELLO:
                return None
            h = json.loads(body.decode("utf-8"))
            cand = self.reg.get(str(h.get("session_id", "")))
            if not cand or not cand.consent_valid(self.reg.clock()):
                self.on_event("rejected", None)
                return None
            if h.get("pc_id") != cand.pc_id or h.get("device_id") != cand.device_id or h.get("provider") != cand.provider:
                self.on_event("rejected", None)
                return None
            want = hello_mac(cand.secret, cand.pc_id, cand.device_id, cand.provider, cand.session_id, nonce)
            if not hmac.compare_digest(want, str(h.get("mac", ""))):
                self.on_event("rejected", None)
                return None
            ses = cand
            ses.live, ses.sharing, ses.last_frame_at = True, "idle", self.reg.clock()   # becomes "sharing" on the first audio frame
            self.buffers[ses.session_id] = LiveBuffer()
            conn.sendall(pack_frame(T_HELLO_OK, b"{}"))
            self.on_event("connected", ses)
            self._loop(conn, ses)
        except (EOFError, OSError, ValueError, KeyError):
            pass
        finally:
            if ses is not None:
                self._drop(ses)
        return ses

    def _loop(self, conn: Any, ses: PhoneSession) -> None:
        last_seq = -1
        while True:
            t, body = read_frame(conn)
            now = self.reg.clock()
            if not ses.consent_valid(now):
                return                      # revoked/expired mid-stream: stop at once
            if t == T_AUDIO:
                if len(body) < 4 or (len(body) - 4) % 2:
                    continue
                seq = struct.unpack(">I", body[:4])[0]
                if seq <= last_seq:         # replayed or reordered: drop, never play stale audio
                    continue
                last_seq = seq
                ses.last_frame_at = now
                if ses.sharing == "idle":
                    ses.sharing = "sharing"
                pcm = body[4:]
                if ses.sharing != "sharing" or self.pc_muted:
                    continue
                self.level = peak_level(pcm)
                self.buffers[ses.session_id].push(pcm)
                for sink in self.sinks:
                    sink(ses, pcm)
            elif t == T_CTRL:
                c = json.loads(body.decode("utf-8"))
                st = c.get("state")
                if st in ("sharing", "muted", "silenced"):
                    ses.sharing = st
                    if st != "sharing":
                        self.buffers[ses.session_id].clear()
                        self.on_utterance_cancel(ses)
                self.on_event("state:" + str(st), ses)
            elif t == T_READ_REQ:
                # Reading is its own opt-in, independent of microphone sharing, and uses the same authenticated session.
                if not ses.allow_read or self.read_handler is None:
                    resp = {"ok": False, "reason": "not-enabled"}
                else:
                    resp = self.read_handler()
                conn.sendall(pack_frame(T_READ_RESP, json.dumps(resp).encode("utf-8")))
            elif t == T_BYE:
                return

    def _drop(self, ses: PhoneSession) -> None:
        ses.live = False
        if ses.sharing != "stopped":
            ses.sharing = "idle"
        buf = self.buffers.pop(ses.session_id, None)
        if buf:
            buf.clear()
        self.level = 0.0
        self.on_utterance_cancel(ses)       # a partial utterance must never be completed from other audio
        self.on_event("disconnected", ses)


def client_hello(sock: Any, ses: PhoneSession) -> None:
    """Reference client side (used by tests and by `victor-mic selftest`); the Android apps implement the same."""
    t, nonce = read_frame(sock)
    assert t == T_CHALLENGE
    sock.sendall(pack_frame(T_HELLO, json.dumps({
        "session_id": ses.session_id, "pc_id": ses.pc_id, "device_id": ses.device_id, "provider": ses.provider,
        "mac": hello_mac(ses.secret, ses.pc_id, ses.device_id, ses.provider, ses.session_id, nonce)}).encode()))
    t, _ = read_frame(sock)
    if t != T_HELLO_OK:
        raise PermissionError("rejected")


def audio_frame(seq: int, pcm: bytes) -> bytes:
    return pack_frame(T_AUDIO, struct.pack(">I", seq) + pcm)


# ---------------------------------------------------------------------------------------------- virtual mic

class VirtualMic:
    """Optional user-session virtual microphone ('Make available to other Linux apps').

    Uses module-null-sink + module-remap-source, which both PulseAudio and pipewire-pulse support, plus `pacat`
    to feed it. It never touches the default source unless set_default=True is requested separately, and
    stop() unloads exactly the modules this object loaded."""

    SINK_NAME = "victor_phone_sink"
    SOURCE_NAME = "victor_phone_mic"

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] | None = None,
                 popen: Callable[..., Any] | None = None):
        self._run = run or _run
        self._popen = popen or (lambda argv: subprocess.Popen(argv, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL))
        self.modules: list[str] = []
        self.proc: Any = None
        self.prev_default = ""
        self.default_changed = False
        self.active = False

    def start(self, description: str, set_default: bool = False) -> None:
        if self.active:
            return
        desc = "".join(c for c in description if c.isalnum() or c in " ·-_")[:60] or "Phone microphone"
        try:
            r = self._run(["pactl", "load-module", "module-null-sink", f"sink_name={self.SINK_NAME}",
                           "channels=1", "rate=16000", f"sink_properties=device.description=Victor_phone_feed"])
            if r.returncode != 0:
                raise RuntimeError("virtual microphone unsupported: " + (r.stderr or "pactl failed").strip())
            self.modules.append(r.stdout.strip())
            r = self._run(["pactl", "load-module", "module-remap-source", f"master={self.SINK_NAME}.monitor",
                           f"source_name={self.SOURCE_NAME}", "channels=1",
                           "source_properties=device.description=" + desc.replace(" ", "\\ ")])
            if r.returncode != 0:
                raise RuntimeError("virtual microphone unsupported: " + (r.stderr or "pactl failed").strip())
            self.modules.append(r.stdout.strip())
            self.proc = self._popen(["pacat", "--playback", f"--device={self.SINK_NAME}", "--format=s16le",
                                     f"--rate={SAMPLE_RATE}", "--channels=1", "--latency-msec=40"])
            self.active = True
            if set_default:
                self.prev_default = (self._run(["pactl", "get-default-source"]).stdout or "").strip()
                self._run(["pactl", "set-default-source", self.SOURCE_NAME])
                self.default_changed = True
        except Exception:
            self.stop()
            raise

    def write(self, pcm: bytes) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.stop()

    def stop(self) -> None:
        if self.default_changed and self.prev_default:
            self._run(["pactl", "set-default-source", self.prev_default])
        self.default_changed = False
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        for m in reversed(self.modules):
            self._run(["pactl", "unload-module", m])
        self.modules = []
        self.active = False

    @property
    def mode(self) -> str:
        return "virtual microphone active" + (" (system default)" if self.default_changed else " (not default)") if self.active else "off"


# ---------------------------------------------------------------------------------------------- offer + serve

OFFER_PREFIX = "vmic1"
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def make_offer(ses: PhoneSession, host: str, port: int, spki_sha256: str) -> str:
    """One-line pairing offer the phone app accepts (paste or QR). Carries the one-time secret: show it only to
    the phone owner, never log it. Fleet messaging delivers the same string when available."""
    for v in (ses.pc_id, ses.device_id, ses.provider):
        if not v or not set(v) <= _SAFE:
            raise ValueError("identifiers may only contain letters, digits, '.', '_' and '-'")
    return ";".join([OFFER_PREFIX, host, str(port), ses.session_id, ses.secret.hex(), ses.pc_id, ses.device_id,
                     ses.provider, spki_sha256, str(int(ses.expires_at))])


def _import_tlsutil():
    for d in (os.environ.get("VICTORFLEET_LIB", ""), "/usr/local/lib/victorfleet",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "fleet")):
        if d and os.path.isdir(os.path.join(d, "victorfleet")):
            sys.path.insert(0, d)
            from victorfleet import tlsutil
            return tlsutil
    raise RuntimeError("victorfleet (TLS helper) not found; set VICTORFLEET_LIB")


class WavWriter:
    """Receiving-side capture used for acceptance tests (`serve --record`). Written only when explicitly asked."""
    def __init__(self, path: str):
        import wave
        self._w = wave.open(path, "wb")
        self._w.setnchannels(1); self._w.setsampwidth(2); self._w.setframerate(SAMPLE_RATE)

    def __call__(self, ses: PhoneSession, pcm: bytes) -> None:
        self._w.writeframes(pcm)

    def close(self) -> None:
        self._w.close()


def serve(args: argparse.Namespace) -> int:
    import ssl
    tls = _import_tlsutil()
    reg = SessionRegistry(args.pc_id)
    tls_dir = args.tls_dir or os.path.join(os.path.dirname(settings_path()), "mic-tls")
    crt, key = tls.ensure_cert(tls_dir, [args.bind, args.advertise or args.bind])
    spki = tls.spki_fingerprint(crt)
    host = args.advertise or args.bind
    sinks: list = []
    vmic = None
    wav = WavWriter(args.record) if args.record else None
    if wav:
        sinks.append(wav)
    for spec in args.offer or []:
        nick, dev, prov, *rest = spec.split(":")
        ses = reg.offer(dev, nick, prov, float(rest[0]) if rest and rest[0] != "read" else 3600, allow_read="read" in rest)
        print(make_offer(ses, host, args.port, spki), flush=True)
    if args.virtual_mic:
        vmic = VirtualMic()
        first = next(iter(reg.active()), None)
        vmic.start(first.label() if first else "Phone microphone", set_default=args.set_default)
        sinks.append(lambda ses, pcm: vmic.write(pcm))
    ptt = None
    if args.ptt:
        import victorptt, victorvoice
        phones = [victorvoice.Phone(x.device_id, x.nickname, i + 1) for i, x in enumerate(reg.active())]
        ptt = victorptt.PttSession(victorptt.VoskRecognizer(), phones, lambda: phones[0].device_id if phones else "")
        sinks.append(lambda ses, pcm: ptt.feed(pcm))

        def stdin_loop() -> None:   # "p" = button down, "r" = button up (prints the parsed command; nothing is executed here)
            for line in sys.stdin:
                if line.strip() == "p":
                    ptt.press()
                elif line.strip() == "r":
                    res = ptt.release()
                    print(json.dumps({"result": type(res).__name__, **vars(res)}), flush=True)
        threading.Thread(target=stdin_loop, daemon=True).start()
    def do_read() -> dict:
        import pcread
        if args.read_test_text:
            be: Any = type("B", (), {"active_window": lambda self: pcread.Window("w", "test", "Test window"),
                                     "runs": lambda self, w: [pcread.TextRun(args.read_test_text, "text", "w", True, False, time.time())]})()
        else:
            try:
                be = pcread.AtspiBackend()
            except Exception:
                be = None
        r = pcread.read_selection(be, args.pc_id, True)
        return {"ok": r.ok, "text": r.text, "pc": r.pc, "window": r.window, "reason": r.reason}

    rx = MicReceiver(reg, sinks, lambda e, s: print(f"event {e} {s.label() if s else ''}", file=sys.stderr, flush=True),
                     (lambda s: ptt.cancel()) if ptt else None, do_read)
    ctx = tls.server_context(crt, key)
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((args.bind, args.port)); ls.listen(4); ls.settimeout(0.5)
    print(f"listening {args.bind}:{ls.getsockname()[1]} spki={spki}", file=sys.stderr, flush=True)
    deadline = time.time() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.time() < deadline:
            try:
                conn, _ = ls.accept()
            except socket.timeout:
                reg.sweep()
                write_status(reg)
                continue
            conn.settimeout(IDLE_TIMEOUT_S)
            try:
                tc = ctx.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                conn.close()
                continue
            threading.Thread(target=rx.serve, args=(tc,), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        if vmic:
            vmic.stop()
        if wav:
            wav.close()
        ls.close()
    return 0


# ---------------------------------------------------------------------------------------------- CLI

def describe(res: Resolution) -> dict[str, Any]:
    return {"selected": res.selected, "delivering": res.delivering or None, "state": res.state,
            "fallback_used": res.fallback_used, "recovery": res.recovery}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="victor-mic", description="Choose the microphone VictorLinux listens to.")
    ap.add_argument("--settings", help="settings file (default ~/.config/victorlinux/mic.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list PC inputs and the system default")
    sub.add_parser("status", help="show selected and delivering source")
    u = sub.add_parser("use", help="select a source")
    u.add_argument("what", choices=["default", "pc", "phone"])
    u.add_argument("ref", nargs="?", help="pc: stable id; phone: DEVICE_ID")
    u.add_argument("--provider", choices=sorted(PROVIDERS))
    p = sub.add_parser("prefer-phone", help="use my preferred phone when no PC mic is available")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--device")
    p.add_argument("--provider", choices=sorted(PROVIDERS))
    v = sub.add_parser("virtual", help="publish the phone mic to other Linux apps")
    v.add_argument("state", choices=["on", "off"])
    v.add_argument("--set-default", action="store_true", help="ALSO make it the system default (separate choice)")
    sv = sub.add_parser("serve", help="listen for phone microphones (TLS); prints one offer line per --offer")
    sv.add_argument("--bind", default="127.0.0.1", help="address to listen on (use your LAN/VPN address; never a public one)")
    sv.add_argument("--advertise", help="host the phone should connect to (default: --bind)")
    sv.add_argument("--port", type=int, default=47821)
    sv.add_argument("--pc-id", default=socket.gethostname().replace(" ", "-")[:48] or "pc")
    sv.add_argument("--tls-dir")
    sv.add_argument("--offer", action="append", metavar="NICK:DEVICE:PROVIDER[:TTL_S][:read]")
    sv.add_argument("--virtual-mic", action="store_true")
    sv.add_argument("--set-default", action="store_true")
    sv.add_argument("--record", metavar="FILE.wav", help="write received audio to a WAV file (acceptance testing)")
    sv.add_argument("--ptt", action="store_true", help="offline push-to-talk on the phone audio: stdin 'p' down, 'r' up")
    sv.add_argument("--read-test-text", help=argparse.SUPPRESS)
    sv.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        return serve(a)
    path = a.settings
    s = load_settings(path)
    reg = SessionRegistry("local")   # the CLI has no live sessions; the GUI/daemon owns them
    if a.cmd == "list":
        pc, d = list_pc_sources()
        print(json.dumps({"system_default": d, "inputs": [vars(x) for x in pc]}, indent=2))
        return 0
    if a.cmd == "status":
        pc, d = list_pc_sources()
        print(json.dumps(describe(resolve(s, pc, d, reg)), indent=2))
        return 0
    if a.cmd == "use":
        if a.what == "default":
            s["mode"] = "default"
        elif a.what == "pc":
            if not a.ref:
                ap.error("pc needs a stable id from `victor-mic list`")
            s.update(mode="pc", pc_stable_id=a.ref)
        else:
            if not (a.ref and a.provider):
                ap.error("phone needs DEVICE_ID and --provider")
            s.update(mode="phone", phone={"device_id": a.ref, "provider": a.provider})
    elif a.cmd == "prefer-phone":
        s["fallback_to_preferred_phone"] = a.state == "on"
        if a.state == "on":
            if not (a.device and a.provider):
                ap.error("--device and --provider are required to turn this on")
            s["preferred_phone"] = {"device_id": a.device, "provider": a.provider}
    elif a.cmd == "virtual":
        s["virtual_mic"] = a.state == "on"
        s["virtual_mic_set_default"] = bool(a.state == "on" and a.set_default)
    save_settings(s, path)
    print("saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
