"""Versioned coordinator/worker contract (protocol_major 1).

Every message is one JSON envelope. The fixtures in fleet/fixtures/v1 are the shared source of truth;
the DroidCommand worker's JVM tests load byte-identical copies (guarded by fixtures/v1/MANIFEST.sha256).

Compatibility rules: unknown *optional* fields are ignored; unknown message types and a different
protocol major are rejected with a structured error. Minor bumps only add optional fields.
"""
import json, re, time, uuid

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 1                      # 1.1 adds terminal_*, draft_* messages and capability `status`; 1.0 peers stay valid
MAX_MESSAGE_BYTES = 256 * 1024          # one envelope on the wire
MAX_BATCH = 64                          # messages per exchange
MAX_STRING = 4096
MAX_ARGS_BYTES = 64 * 1024              # job args
MAX_PLAN_JOBS = 25                      # jobs in the optional plan manifest on an assignment
MAX_PLAN_BYTES = 96 * 1024              # the whole manifest, serialised
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
OP_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

WORKER_TYPES = {"enroll_request", "hello", "heartbeat", "ack", "progress", "complete", "fail",
                "cancel_ack", "approval_needed", "artifact_offer", "node_message",
                "terminal_status", "draft_status"}                          # 1.1
COORD_TYPES = {"enroll_response", "welcome", "assignment", "cancel", "revoke", "artifact_grant",
               "node_message", "reject",
               "terminal_open", "terminal_close", "draft_deliver"}          # 1.1

ERROR_CODES = {"bad_request", "unauthorized", "revoked", "unsupported_protocol", "unknown_type", "too_large",
               "stale_attempt", "unknown_job", "quota_exceeded", "capability_missing", "permission_denied",
               "approval_required", "device_busy", "deadline_exceeded", "cancelled", "executor_unavailable",
               "internal", "declined", "revoked_session", "version_outdated", "broker_untrusted"}

# Job states. Terminal: succeeded failed cancelled cancelled_unconfirmed needs_review dep_failed expired
JOB_STATES = ["queued", "assigned", "running", "awaiting_approval", "cancelling", "succeeded", "failed",
              "cancelled", "cancelled_unconfirmed", "needs_review", "dep_failed", "expired"]
TERMINAL = {"succeeded", "failed", "cancelled", "cancelled_unconfirmed", "needs_review", "dep_failed", "expired"}


class ProtocolError(Exception):
    def __init__(self, code, message):
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code, self.message = code, message

    def to_dict(self):
        return {"code": self.code, "message": self.message}


def now_ms():
    return int(time.time() * 1000)


def new_id():
    return uuid.uuid4().hex


def envelope(mtype, sender, target, payload, msg_id=None, sent_at=None, in_reply_to=None):
    e = {"protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}, "msg_id": msg_id or new_id(),
         "type": mtype, "sender": sender, "target": target, "sent_at": sent_at or now_ms(), "payload": payload}
    if in_reply_to:
        e["in_reply_to"] = in_reply_to
    return e


def _need(d, key, typ, code="bad_request"):
    if key not in d or not isinstance(d[key], typ) or isinstance(d[key], bool) and typ is int:
        raise ProtocolError(code, f"field '{key}' missing or not {typ.__name__}")
    return d[key]


def _str(d, key, maxlen=MAX_STRING, optional=False):
    if optional and key not in d:
        return None
    v = _need(d, key, str)
    if len(v) > maxlen:
        raise ProtocolError("too_large", f"'{key}' too long")
    return v


def _id(d, key, optional=False):
    v = _str(d, key, 96, optional)
    if v is not None and not ID_RE.match(v):
        raise ProtocolError("bad_request", f"'{key}' is not a valid identifier")
    return v


def validate_envelope(e, expect_sender=None):
    """Structural validation shared by both directions. Returns the envelope. Raises ProtocolError."""
    if not isinstance(e, dict):
        raise ProtocolError("bad_request", "envelope must be an object")
    proto = _need(e, "protocol", dict)
    if _need(proto, "major", int) != PROTOCOL_MAJOR:
        raise ProtocolError("unsupported_protocol", f"coordinator speaks protocol major {PROTOCOL_MAJOR}")
    _need(proto, "minor", int)
    _id(e, "msg_id"); _id(e, "sender"); _id(e, "target")
    t = _str(e, "type", 64)
    _need(e, "sent_at", int)
    _need(e, "payload", dict)
    if t not in WORKER_TYPES | COORD_TYPES:
        raise ProtocolError("unknown_type", f"unknown message type '{t}'")
    if expect_sender is not None and e["sender"] != expect_sender:
        raise ProtocolError("unauthorized", "sender does not match the authenticated identity")
    return e


def validate_worker_payload(t, p):
    """Per-type payload validation for worker->coordinator messages."""
    if t == "enroll_request":
        _str(p, "device_name", 80); _str(p, "public_key_pem", 2048); _str(p, "pairing_proof", 256)
        _str(p, "signature_b64", 512); _str(p, "nonce", 128)
    elif t == "hello":
        validate_capabilities(_need(p, "capabilities", dict))
        _str(p, "app_version", 64)
        for r in p.get("running", []):                      # reconnect reconciliation
            _id(r, "job_id"); _need(r, "attempt", int)
    elif t == "heartbeat":
        d = _need(p, "device_state", dict)
        validate_device_state(d)
    elif t == "ack":
        _id(p, "job_id"); _need(p, "attempt", int)
    elif t == "progress":
        _id(p, "job_id"); _need(p, "attempt", int)
        pct = p.get("percent")
        if pct is not None and (not isinstance(pct, (int, float)) or not 0 <= pct <= 100):
            raise ProtocolError("bad_request", "percent out of range")
        _str(p, "note", 512, optional=True)
    elif t == "complete":
        _id(p, "job_id"); _need(p, "attempt", int)
        _need(p, "result", dict)
        for a in p.get("artifacts", []):
            validate_artifact_ref(a)
    elif t == "fail":
        _id(p, "job_id"); _need(p, "attempt", int)
        err = _need(p, "error", dict)
        if err.get("code") not in ERROR_CODES:
            raise ProtocolError("bad_request", "error.code is not a known code")
        _str(err, "message", 512)
        if "side_effects" in p and p["side_effects"] not in ("none", "possible", "certain"):
            raise ProtocolError("bad_request", "side_effects must be none|possible|certain")
    elif t == "cancel_ack":
        _id(p, "job_id"); _need(p, "attempt", int)
        if p.get("state") not in ("stopped", "completed_before_cancel", "not_running"):
            raise ProtocolError("bad_request", "cancel_ack.state invalid")
    elif t == "approval_needed":
        _id(p, "job_id"); _need(p, "attempt", int); _str(p, "reason", 256)
    elif t == "artifact_offer":
        _id(p, "job_id"); _need(p, "attempt", int); validate_artifact_ref(_need(p, "artifact", dict))
    elif t == "node_message":
        _id(p, "to_device"); _str(p, "topic", 64)
        _need(p, "body", dict)
    elif t == "terminal_status":
        validate_terminal_status(p)
    elif t == "draft_status":
        validate_draft_status(p)


def validate_plan_manifest(plan):
    """The optional `plan` object an assignment may carry (see docs/FLEET.md, "Plan manifest"). Raises ProtocolError.

    A phone must IGNORE a manifest it cannot validate and run the job as usual; the manifest only lets the owner approve
    a reviewed batch once, it is never needed to run anything."""
    if not isinstance(plan, dict):
        raise ProtocolError("bad_request", "plan must be an object")
    plan_id = _id(plan, "plan_id")
    jobs = _need(plan, "jobs", list)
    if not 1 <= len(jobs) <= MAX_PLAN_JOBS:
        raise ProtocolError("too_large" if len(jobs) > MAX_PLAN_JOBS else "bad_request", f"plan must list 1..{MAX_PLAN_JOBS} jobs")
    seen = set()
    for j in jobs:
        if not isinstance(j, dict):
            raise ProtocolError("bad_request", "plan job must be an object")
        jid = _id(j, "job_id")
        if not jid.startswith(plan_id + "."):
            raise ProtocolError("bad_request", "plan lists a job that is not part of this plan")
        if jid in seen:
            raise ProtocolError("bad_request", "plan lists the same job twice")
        seen.add(jid)
        if not OP_RE.match(_str(j, "op", 64)):
            raise ProtocolError("bad_request", "plan job op invalid")
        if len(json.dumps(_need(j, "args", dict))) > MAX_ARGS_BYTES:
            raise ProtocolError("too_large", "plan job args too large")
        if "requires_root" in j:
            _need(j, "requires_root", bool)
    if len(json.dumps(plan)) > MAX_PLAN_BYTES:
        raise ProtocolError("too_large", "plan manifest too large")


def validate_artifact_ref(a):
    _str(a, "name", 128)
    if not re.match(r"^[A-Za-z0-9._-]{1,128}$", a["name"]) or a["name"] in (".", ".."):
        raise ProtocolError("bad_request", "artifact name must be a plain filename")
    _need(a, "size", int)
    if not SHA256_RE.match(_str(a, "sha256", 64)):
        raise ProtocolError("bad_request", "artifact sha256 must be 64 lowercase hex chars")


def validate_capabilities(c):
    """Live capability report. Each operation states availability, whether its permission is currently
    granted, and whether owner interaction is needed. A flag alone is never an executable capability."""
    ops = _need(c, "operations", dict)
    if len(ops) > 64:
        raise ProtocolError("too_large", "too many operations")
    for name, o in ops.items():
        if not OP_RE.match(name):
            raise ProtocolError("bad_request", f"operation name '{name}' invalid")
        if not isinstance(o, dict) or not isinstance(o.get("available"), bool):
            raise ProtocolError("bad_request", f"operation '{name}' needs boolean 'available'")
        if "granted" in o and not isinstance(o["granted"], bool):
            raise ProtocolError("bad_request", f"operation '{name}' granted must be boolean")
        if "status" in o and o["status"] not in OP_STATUSES:            # 1.1, optional: lets a UI explain *why* an op is unavailable
            raise ProtocolError("bad_request", f"operation '{name}' status invalid")
        for k in ("detail", "reason", "min_version", "installed_version"):
            if k in o and (not isinstance(o[k], str) or len(o[k]) > 200):
                raise ProtocolError("bad_request", f"operation '{name}' {k} must be a string up to 200 chars")
    root = c.get("root", {})
    if root and not isinstance(root.get("available"), bool):
        raise ProtocolError("bad_request", "root.available must be boolean")
    validate_device_state(_need(c, "device_state", dict))


def validate_device_state(d):
    for k in ("battery_percent", "free_storage_bytes"):
        if k in d and (not isinstance(d[k], int) or isinstance(d[k], bool) or d[k] < 0):
            raise ProtocolError("bad_request", f"{k} invalid")
    if "battery_percent" in d and d["battery_percent"] > 100:
        raise ProtocolError("bad_request", "battery_percent > 100")
    if "charging" in d and not isinstance(d["charging"], bool):
        raise ProtocolError("bad_request", "charging must be boolean")
    if "thermal" in d and d["thermal"] not in ("none", "light", "moderate", "severe", "critical", "emergency", "shutdown", "unknown"):
        raise ProtocolError("bad_request", "thermal status invalid")


# ---------------------------------------------------------------- protocol 1.1: terminal + messaging
# Ephemeral, owner-approved features. None of these messages is ever placed in the durable outbox or a job queue:
# a missed `terminal_open` / `draft_deliver` is never replayed (see scheduler.Coordinator.enqueue_ephemeral).
TERMINAL_OPS = {"terminal": "terminal.session", "shell": "shell.oneshot",
                "sms": "message.draft.sms", "email": "message.draft.email"}
OP_STATUSES = {"ready", "permission_needed", "owner_interaction_required", "version_outdated", "not_installed",
               "untrusted", "revoked", "unavailable"}
PRIVILEGES = {"user", "root"}
TERMINAL_MODES = {"interactive", "oneshot"}
TERMINAL_STATES = {"requested", "awaiting_approval", "open", "denied", "closed", "failed", "revoked", "expired", "disconnected"}
TERMINAL_FINAL = {"denied", "closed", "failed", "revoked", "expired", "disconnected"}
TERMINAL_WORKER_STATES = {"awaiting_approval", "open", "denied", "closed", "failed", "revoked"}
DRAFT_CHANNELS = {"sms", "email"}
DRAFT_STATES = {"pending", "received", "declined", "submitted", "failed", "unknown"}
DRAFT_FINAL = {"declined", "submitted", "failed"}
DRAFT_WORKER_STATES = {"received", "declined", "submitted", "failed"}
CHUNK_KINDS = {"data", "resize", "signal", "eof", "exit", "close"}
MAX_CHUNK_BYTES = 16 * 1024
MAX_DRAFT_BYTES = 192 * 1024
MAX_COMMAND_CHARS = 4096
SIGNALS = {"INT", "TERM", "HUP", "QUIT", "TSTP", "CONT"}


def terminal_binding_hash(session_id, device_id, desktop, privilege, mode, command, nonce):
    """What the phone's owner approval is bound to. The phone echoes this in `terminal_status`; the coordinator compares it with its own
    computation, so an approval can never be reused for another peer, target, session, privilege or command."""
    import hashlib, json as _j
    blob = _j.dumps([session_id, device_id, desktop, privilege, mode, command, nonce], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("victorfleet-terminal-v1\n" + blob).encode()).hexdigest()


def draft_content_hash(channel, fields):
    import hashlib, json as _j
    return hashlib.sha256(("victorfleet-draft-v1\n" + channel + "\n" + _j.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)).encode()).hexdigest()


def validate_chunk(c):
    """One relay chunk (either direction). `seq` is per direction, starts at 1 and is contiguous."""
    import base64
    if not isinstance(c, dict):
        raise ProtocolError("bad_request", "chunk must be an object")
    n = _need(c, "seq", int)
    if n < 1:
        raise ProtocolError("bad_request", "seq starts at 1")
    kind = _str(c, "kind", 16)
    if kind not in CHUNK_KINDS:
        raise ProtocolError("bad_request", "unknown chunk kind")
    if kind == "data":
        raw = c.get("data")
        if not isinstance(raw, str):
            raise ProtocolError("bad_request", "data chunk needs base64 'data'")
        try:
            b = base64.b64decode(raw.encode(), validate=True)
        except Exception:
            raise ProtocolError("bad_request", "data is not valid base64")
        if len(b) > MAX_CHUNK_BYTES:
            raise ProtocolError("too_large", "chunk over 16 KiB")
        if c.get("channel", "tty") not in ("tty", "stdout", "stderr"):
            raise ProtocolError("bad_request", "bad channel")
    elif kind == "resize":
        for k in ("cols", "rows"):
            v = _need(c, k, int)
            if not 1 <= v <= 1000:
                raise ProtocolError("bad_request", f"{k} out of range")
    elif kind == "signal":
        if c.get("name") not in SIGNALS:
            raise ProtocolError("bad_request", "unknown signal")
    elif kind == "exit":
        _need(c, "status", int)
    elif kind == "close":
        _str(c, "reason", 128, optional=True)
    return c


def validate_terminal_open(p):
    _id(p, "session_id"); _str(p, "desktop", 128)
    if _str(p, "mode", 16) not in TERMINAL_MODES:
        raise ProtocolError("bad_request", "mode must be interactive|oneshot")
    if _str(p, "privilege", 8) not in PRIVILEGES:
        raise ProtocolError("bad_request", "privilege must be user|root")
    _need(p, "cols", int); _need(p, "rows", int); _str(p, "nonce", 64); _need(p, "expires_ms", int)
    cmd = p.get("command")
    if p["mode"] == "oneshot":
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_COMMAND_CHARS:
            raise ProtocolError("bad_request", "oneshot needs a non-empty command (<= 4096 chars)")
    elif cmd is not None:
        raise ProtocolError("bad_request", "interactive sessions carry no command; input is typed live")
    _str(p, "binding", 64)


def validate_terminal_status(p):
    _id(p, "session_id")
    if _str(p, "state", 24) not in TERMINAL_WORKER_STATES:
        raise ProtocolError("bad_request", "terminal_status.state invalid")
    _str(p, "binding", 64, optional=True); _str(p, "reason", 256, optional=True)
    if "exit_status" in p:
        _need(p, "exit_status", int)
    if p["state"] == "open" and "binding" not in p:
        raise ProtocolError("bad_request", "an 'open' status must echo the approval binding")


def _recipient_list(d, key, required=False):
    v = d.get(key, [])
    if not isinstance(v, list) or (required and not v) or len(v) > 50:
        raise ProtocolError("bad_request", f"'{key}' must be a list of up to 50 addresses" + (" and not empty" if required else ""))
    for a in v:
        if not isinstance(a, str) or not 1 <= len(a) <= 320 or "\n" in a or "\r" in a:
            raise ProtocolError("bad_request", f"'{key}' has an invalid address")
    return v


def validate_draft(channel, fields):
    """Content of a desktop-prepared draft. Never contains credentials: the phone chooses the sending identity."""
    import base64
    if channel not in DRAFT_CHANNELS:
        raise ProtocolError("bad_request", "channel must be sms|email")
    if not isinstance(fields, dict):
        raise ProtocolError("bad_request", "draft must be an object")
    body = fields.get("body")
    if not isinstance(body, str) or len(body) > 100_000:
        raise ProtocolError("bad_request", "body must be text up to 100000 chars")
    if channel == "sms":
        _recipient_list(fields, "to", required=True)
        if not body.strip():
            raise ProtocolError("bad_request", "an SMS needs a body")
        extra = set(fields) - {"to", "body", "sim_hint"}
    else:
        _recipient_list(fields, "to", required=True); _recipient_list(fields, "cc"); _recipient_list(fields, "bcc")
        if not isinstance(fields.get("subject", ""), str) or len(fields.get("subject", "")) > 998 or "\n" in fields.get("subject", "") or "\r" in fields.get("subject", ""):
            raise ProtocolError("bad_request", "subject must be one line up to 998 chars")
        if "account_hint" in fields:
            _str(fields, "account_hint", 320)
        atts = fields.get("attachments", [])
        if not isinstance(atts, list) or len(atts) > 8:
            raise ProtocolError("bad_request", "at most 8 attachments")
        total = 0
        for a in atts:
            _str(a, "name", 128); _str(a, "mime", 128)
            if not re.match(r"^[A-Za-z0-9._ -]{1,128}$", a["name"]) or a["name"] in (".", ".."):
                raise ProtocolError("bad_request", "attachment name must be a plain filename")
            try:
                total += len(base64.b64decode(_str(a, "data_b64", MAX_DRAFT_BYTES * 2).encode(), validate=True))
            except ProtocolError:
                raise
            except Exception:
                raise ProtocolError("bad_request", "attachment is not valid base64")
        if total > MAX_DRAFT_BYTES:
            raise ProtocolError("too_large", "attachments over 192 KiB; larger files belong in an artifact hand-off")
        extra = set(fields) - {"to", "cc", "bcc", "subject", "body", "account_hint", "attachments"}
    if extra:
        raise ProtocolError("bad_request", f"unknown draft fields: {sorted(extra)}")
    return fields


def validate_draft_deliver(p):
    _id(p, "draft_id"); _str(p, "content_hash", 64); _str(p, "origin", 128); _need(p, "expires_ms", int)
    validate_draft(_str(p, "channel", 8), _need(p, "draft", dict))
    if draft_content_hash(p["channel"], p["draft"]) != p["content_hash"]:
        raise ProtocolError("bad_request", "content_hash does not match the draft")


def validate_draft_status(p):
    _id(p, "draft_id")
    if _str(p, "state", 16) not in DRAFT_WORKER_STATES:
        raise ProtocolError("bad_request", "draft_status.state invalid")
    _str(p, "content_hash", 64, optional=True); _str(p, "detail", 256, optional=True)


def validate_any_payload(t, p):
    """Fixture/test helper: payload validation for every message type this version knows (both directions)."""
    if t in WORKER_TYPES:
        validate_worker_payload(t, p)
    elif t == "terminal_open":
        validate_terminal_open(p)
    elif t == "terminal_close":
        _id(p, "session_id"); _str(p, "reason", 256, optional=True)
    elif t == "draft_deliver":
        validate_draft_deliver(p)
