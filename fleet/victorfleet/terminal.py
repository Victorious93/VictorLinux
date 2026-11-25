"""Terminal sessions and one-shot commands relayed between the local desktop and ONE approved phone.

Trust model (protocol 1.1):

* The *desktop* side is the local owner on the coordinator machine (admin Unix socket, mode 0600). The *phone* side is a
  paired device over the existing authenticated HTTPS channel. The coordinator only relays; the shell process is owned by
  VictorSuite on the phone, which asks its owner for approval first.
* Setup/status go through the coordinator's normal exchange as `terminal_open` / `terminal_status`. Live bytes go through the
  per-session relay below. Nothing here is ever written to the durable outbox, the job tables or the log: contents are kept
  in memory only, bounded, and dropped when the session ends.
* An approval is bound to (session, target device, desktop identity, privilege, mode, command, nonce) by `binding`. The phone
  must echo it; any mismatch fails the session.
* No replay: chunks are sequence-numbered per direction and de-duplicated; input typed while the phone is not connected is
  dropped and the session is ended (`disconnected`), never buffered for later. A session never moves to another device.
"""
import base64, hmac, secrets, threading, time
from .protocol import (ProtocolError, envelope, new_id, now_ms, validate_chunk, terminal_binding_hash, TERMINAL_FINAL, PRIVILEGES,
                       TERMINAL_MODES, MAX_COMMAND_CHARS)

DEFAULTS = {
    "approval_timeout_s": 120,      # owner must act on the phone within this long
    "ack_timeout_s": 30,            # phone must at least acknowledge terminal_open (state awaiting_approval) in this time
    "phone_grace_s": 12,            # open session with a silent phone is ended (never buffered)
    "max_sessions": 4,
    "max_unacked_bytes": 1 << 20,   # backpressure bound per direction
    "oneshot_max_s": 300,
    "idle_close_s": 1800,
}


class Pipe:
    """One direction of a session: sequenced chunks held only until the receiver acknowledges them."""

    def __init__(self):
        self.next_seq = 1
        self.acked = 0
        self.chunks = []              # [(seq, dict)]
        self.bytes = 0

    def push(self, chunk):
        chunk = dict(chunk, seq=self.next_seq)
        self.next_seq += 1
        self.chunks.append((chunk["seq"], chunk))
        self.bytes += len(chunk.get("data", "")) + 32
        return chunk["seq"]

    def ack(self, upto):
        if upto > self.acked:
            self.acked = min(upto, self.next_seq - 1)
        keep = []
        for s, c in self.chunks:
            if s > self.acked:
                keep.append((s, c))
            else:
                self.bytes -= len(c.get("data", "")) + 32
        self.chunks = keep

    def pending(self, limit=64):
        return [c for s, c in self.chunks if s > self.acked][:limit]


class Session:
    def __init__(self, sid, device, desktop, mode, privilege, command, cols, rows, now):
        self.sid, self.device, self.desktop, self.mode, self.privilege, self.command = sid, device, desktop, mode, privilege, command
        self.cols, self.rows = cols, rows
        self.nonce = secrets.token_hex(16)
        self.attach_token = secrets.token_urlsafe(24)
        self.binding = terminal_binding_hash(sid, device, desktop, privilege, mode, command, self.nonce)
        self.state = "requested"
        self.reason = None
        self.created = self.last_phone = self.last_activity = now
        self.opened = None
        self.exit_status = None
        self.to_phone, self.to_desktop = Pipe(), Pipe()
        self.phone_seen = 0           # highest phone->relay seq accepted (dedup)
        self.desktop_seen = 0
        self.lock = threading.RLock()

    def public(self):
        return {"session_id": self.sid, "device": self.device, "desktop": self.desktop, "mode": self.mode, "privilege": self.privilege,
                "state": self.state, "reason": self.reason, "exit_status": self.exit_status, "created_ms": int(self.created * 1000)}


class TerminalHub:
    def __init__(self, coord, store, clock=time.time, cfg=None):
        self.coord, self.store, self.clock = coord, store, clock
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.sessions = {}
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def _event(self, kind, s, level="info", detail=None):
        # Never contents: ids, state and sizes only.
        self.store.event(kind, level, device_id=s.device, detail=f"{s.sid} {detail or ''}".strip())

    def _get(self, sid):
        s = self.sessions.get(sid)
        if not s:
            raise ProtocolError("unknown_job", "no such terminal session")
        return s

    def _finish(self, s, state, reason=None):
        with s.lock:
            if s.state in TERMINAL_FINAL:
                return
            s.state, s.reason = state, reason
            s.to_desktop.push({"kind": "close", "reason": state if not reason else f"{state}: {reason}"})
            s.to_phone.push({"kind": "close", "reason": reason or state})
        self._event("terminal_" + state, s, "info" if state in ("closed", "denied") else "warn", reason)

    # --------------------------------------------------------------- operations
    def open(self, device_id, desktop, mode="interactive", privilege="user", command=None, cols=80, rows=24):
        if mode not in TERMINAL_MODES or privilege not in PRIVILEGES:
            raise ProtocolError("bad_request", "bad mode or privilege")
        if mode == "oneshot" and (not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS):
            raise ProtocolError("bad_request", "one-shot needs a non-empty command up to 4096 chars")
        if mode == "interactive" and command is not None:
            raise ProtocolError("bad_request", "interactive sessions take no pre-supplied command")
        if not (isinstance(cols, int) and isinstance(rows, int) and 1 <= cols <= 1000 and 1 <= rows <= 1000):
            raise ProtocolError("bad_request", "bad terminal size")
        dev = self.store.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not dev or dev["state"] != "paired":
            raise ProtocolError("unknown_job", "target is not a paired device")
        feat = self.coord.features(dev)["shell_oneshot" if mode == "oneshot" else "terminal"]
        if feat["state"] != "ready":
            raise ProtocolError("capability_missing", f"{feat['state']}: {feat['detail']}")
        if privilege == "root":
            rf = self.coord.features(dev)["terminal_root"]
            if rf["state"] != "ready":
                raise ProtocolError("permission_denied", f"privileged shell is not available on this phone ({rf['state']}); it is granted separately from ordinary shell access")
        now = self.clock()
        with self.lock:
            self._expire(now)
            live = [s for s in self.sessions.values() if s.state not in TERMINAL_FINAL]
            if len(live) >= self.cfg["max_sessions"]:
                raise ProtocolError("quota_exceeded", "too many open terminal sessions")
            if any(s.device == device_id for s in live):
                raise ProtocolError("device_busy", "that phone already has an open terminal session; close it first")
            sid = "term-" + new_id()[:16]
            s = Session(sid, device_id, desktop, mode, privilege, command, cols, rows, now)
            self.sessions[sid] = s
        self.coord.enqueue_ephemeral(device_id, envelope("terminal_open", "coordinator", device_id, {
            "session_id": sid, "desktop": desktop, "mode": mode, "privilege": privilege, "cols": cols, "rows": rows,
            "nonce": s.nonce, "expires_ms": int((now + self.cfg["approval_timeout_s"]) * 1000), "binding": s.binding,
            **({"command": command} if command is not None else {})}), ttl_s=self.cfg["ack_timeout_s"])
        self._event("terminal_requested", s, detail=f"{mode}/{privilege} from {desktop}")
        return {"session_id": sid, "attach_token": s.attach_token, "state": s.state}

    def _expire(self, now):
        for s in list(self.sessions.values()):
            if s.state in TERMINAL_FINAL:
                if now - s.last_activity > 120:      # keep the final state briefly so a client can read the reason
                    self.sessions.pop(s.sid, None)
                continue
            if s.state == "requested" and now - s.created > self.cfg["ack_timeout_s"]:
                self._finish(s, "expired", "the phone never acknowledged the request")
            elif s.state == "awaiting_approval" and now - s.created > self.cfg["approval_timeout_s"]:
                self._finish(s, "expired", "the owner did not approve in time")
            elif s.state == "open" and now - s.last_phone > self.cfg["phone_grace_s"]:
                self._finish(s, "disconnected", "phone connection lost; input is never buffered or replayed")
            elif s.state == "open" and s.mode == "oneshot" and now - s.opened > self.cfg["oneshot_max_s"]:
                self._finish(s, "expired", "one-shot command time limit")
            elif s.state == "open" and now - s.last_activity > self.cfg["idle_close_s"]:
                self._finish(s, "closed", "idle")

    def tick(self):
        with self.lock:
            self._expire(self.clock())

    def on_status(self, dev, p):
        """Worker `terminal_status`: the only way a session becomes approved/open. Verifies the approval binding."""
        s = self._get(p["session_id"])
        if s.device != dev["id"]:
            raise ProtocolError("unauthorized", "that session belongs to another device")
        now = self.clock()
        with s.lock:
            s.last_phone = s.last_activity = now
            if s.state in TERMINAL_FINAL:
                return
            st = p["state"]
            if st == "awaiting_approval" and s.state == "requested":
                s.state = "awaiting_approval"
            elif st == "open":
                if s.state not in ("requested", "awaiting_approval"):
                    return
                if not hmac.compare_digest(str(p.get("binding", "")).encode(), str(s.binding).encode()):
                    self._finish(s, "failed", "approval binding mismatch (phone approved something else)")
                    return
                s.state, s.opened = "open", now
                self._event("terminal_open", s, detail=f"{s.mode}/{s.privilege}")
            elif st == "denied":
                self._finish(s, "denied", p.get("reason") or "the owner declined")
            elif st == "failed":
                self._finish(s, "failed", p.get("reason") or "the phone could not start the shell")
            elif st == "revoked":
                self._finish(s, "revoked", p.get("reason") or "access was revoked on the phone")
            elif st == "closed":
                if "exit_status" in p:
                    s.exit_status = p["exit_status"]
                self._finish(s, "closed", p.get("reason"))

    # ---- relay endpoints -------------------------------------------------------------------------------------------
    def phone_exchange(self, dev, sid, body):
        """Phone <-> relay: body {ack, chunks:[...]}. Returns chunks for the phone and the current state."""
        s = self._get(sid)
        if s.device != dev["id"]:
            raise ProtocolError("unauthorized", "that session belongs to another device")
        now = self.clock()
        with s.lock:
            s.last_phone = now
            if s.state in TERMINAL_FINAL and not s.to_phone.pending():
                return {"state": s.state, "reason": s.reason, "ack": s.phone_seen, "chunks": []}
            s.to_phone.ack(int(body.get("ack", 0) or 0))
            if s.state != "open":
                # Not open (yet): do NOT consume the phone's chunks. They are retried with the same seq once the relay says `open`, so
                # nothing is lost to a status/relay race, and nothing from an unapproved or finished session is ever forwarded.
                return {"state": s.state, "reason": s.reason, "ack": s.phone_seen, "chunks": s.to_phone.pending()}
            for c in body.get("chunks", [])[:64]:
                validate_chunk(c)
                if c["seq"] <= s.phone_seen:
                    continue                                # duplicate: dropped, never delivered twice
                if c["seq"] != s.phone_seen + 1:
                    self._finish(s, "failed", "phone chunk sequence gap")
                    break
                s.phone_seen = c["seq"]
                if s.state != "open":
                    continue
                s.last_activity = now
                if c["kind"] == "exit":
                    s.exit_status = c["status"]
                s.to_desktop.push({k: v for k, v in c.items() if k != "seq"})
                if s.to_desktop.bytes > self.cfg["max_unacked_bytes"]:
                    self._finish(s, "failed", "desktop is not reading output (buffer limit)")
                    break
            return {"state": s.state, "reason": s.reason, "ack": s.phone_seen, "chunks": s.to_phone.pending()}

    def desktop_exchange(self, sid, token, body):
        s = self._get(sid)
        if not hmac.compare_digest(str(token or "").encode(), str(s.attach_token).encode()):
            raise ProtocolError("unauthorized", "not the client that opened this session")
        now = self.clock()
        with self.lock:
            self._expire(now)
        with s.lock:
            s.to_desktop.ack(int(body.get("ack", 0) or 0))
            for c in body.get("chunks", [])[:64]:
                validate_chunk(c)
                if c["seq"] <= s.desktop_seen:
                    continue
                if c["seq"] != s.desktop_seen + 1:
                    self._finish(s, "failed", "desktop chunk sequence gap")
                    break
                s.desktop_seen = c["seq"]
                if s.state != "open":
                    continue                                # typed before approval: dropped, never queued
                if s.mode == "oneshot" and c["kind"] not in ("signal", "close"):
                    continue                                # a one-shot takes no live input
                s.last_activity = now
                if c["kind"] == "close":
                    s.to_phone.push({"kind": "close", "reason": "desktop closed"})
                    self._finish(s, "closed", "closed by the desktop")
                    break
                s.to_phone.push({k: v for k, v in c.items() if k != "seq"})
                if s.to_phone.bytes > self.cfg["max_unacked_bytes"]:
                    self._finish(s, "failed", "phone is not reading input (buffer limit)")
                    break
            return {"state": s.state, "reason": s.reason, "exit_status": s.exit_status, "chunks": s.to_desktop.pending()}

    def close(self, sid, reason="closed by the owner"):
        s = self._get(sid)
        if s.state in TERMINAL_FINAL:
            return s.public()
        with s.lock:
            if s.state in ("requested", "awaiting_approval", "open"):
                self.coord.enqueue_ephemeral(s.device, envelope("terminal_close", "coordinator", s.device,
                                                                {"session_id": sid, "reason": reason}), ttl_s=30)
        self._finish(s, "closed", reason)
        return s.public()

    def close_for_device(self, device_id, reason, state="revoked"):
        for s in list(self.sessions.values()):
            if s.device == device_id and s.state not in TERMINAL_FINAL:
                self._finish(s, state, reason)

    def list(self):
        self.tick()
        return [s.public() for s in self.sessions.values()]

    def info(self, sid):
        return self._get(sid).public()
