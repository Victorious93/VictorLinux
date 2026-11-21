"""Versioned coordinator/worker contract (protocol_major 1).

Every message is one JSON envelope. The fixtures in fleet/fixtures/v1 are the shared source of truth;
the DroidCommand worker's JVM tests load byte-identical copies (guarded by fixtures/v1/MANIFEST.sha256).

Compatibility rules: unknown *optional* fields are ignored; unknown message types and a different
protocol major are rejected with a structured error. Minor bumps only add optional fields.
"""
import json, re, time, uuid

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
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
                "cancel_ack", "approval_needed", "artifact_offer", "node_message"}
COORD_TYPES = {"enroll_response", "welcome", "assignment", "cancel", "revoke", "artifact_grant",
               "node_message", "reject"}

ERROR_CODES = {"bad_request", "unauthorized", "revoked", "unsupported_protocol", "unknown_type", "too_large",
               "stale_attempt", "unknown_job", "quota_exceeded", "capability_missing", "permission_denied",
               "approval_required", "device_busy", "deadline_exceeded", "cancelled", "executor_unavailable",
               "internal"}

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
