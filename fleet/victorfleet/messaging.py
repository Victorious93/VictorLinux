"""Desktop -> phone message drafts (SMS and email), reviewed and sent only by the phone's owner.

Contract (protocol_major 1, additive; op name `message.draft`):
  assignment args : {draft_id, kind: "sms"|"email", to: [..], cc: [..], bcc: [..], subject, body, identity_hint}
  phone -> coord  : `progress` with note "draft_received" once the draft is on screen in VictorSMS (job state running)
                    `approval_needed` while the owner is reviewing (job state awaiting_approval)
                    `complete` result {draft_id, status: "submitted"|"declined"|"uncertain"}
                    `fail` error + side_effects: none (nothing sent) | possible (unknown) | certain
The coordinator NEVER retries a draft (max_attempts 1, not idempotent). The phone refuses a draft_id it has already
shown, so a re-delivered assignment cannot send twice. Mail credentials never leave the phone: args carry only an
identity *hint* (account label), never a password or token. Bodies are scrubbed from the job record when it ends and
never appear in the audit events or in admin job listings.
"""
import hashlib, json, re
from .protocol import ProtocolError, new_id

OP = "message.draft"
MAX_BODY = 20000
MAX_RCPT = 50
EMAIL_RE = re.compile(r"^[^@\s<>\",;]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{2,30}$")
DRAFT_STATES = ("queued", "draft_received", "declined", "submitted", "failed", "unknown")


def _clean(addr, kind):
    a = str(addr).strip()
    if kind == "email" and not EMAIL_RE.match(a):
        raise ProtocolError("bad_request", f"'{a}' is not a valid email address")
    if kind == "sms" and not PHONE_RE.match(a):
        raise ProtocolError("bad_request", f"'{a}' is not a valid phone number")
    return a


def build_draft(kind, to, body, cc=(), bcc=(), subject="", identity_hint="", draft_id=None):
    if kind not in ("sms", "email"):
        raise ProtocolError("bad_request", "kind must be sms or email")
    if not to or len(to) + len(cc) + len(bcc) > MAX_RCPT:
        raise ProtocolError("bad_request", f"1..{MAX_RCPT} recipients required")
    if kind == "sms" and (cc or bcc or subject):
        raise ProtocolError("bad_request", "an SMS has no Cc, Bcc or subject")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        raise ProtocolError("bad_request", f"body must be 1..{MAX_BODY} characters")
    if kind == "email" and not subject.strip():
        raise ProtocolError("bad_request", "an email needs a subject")
    if re.search(r"(?i)(password|token|secret)\s*[:=]", identity_hint or ""):
        raise ProtocolError("bad_request", "identity_hint is a label such as 'work@example.com', never a credential")
    return {"draft_id": draft_id or "dr-" + new_id()[:16], "kind": kind, "to": [_clean(a, kind) for a in to],
            "cc": [_clean(a, kind) for a in cc], "bcc": [_clean(a, kind) for a in bcc], "subject": subject,
            "body": body, "identity_hint": identity_hint or ""}


def draft_plan(device_id, draft):
    """One pinned, non-retryable job. `ui_exclusive` because the owner must look at the phone."""
    return {"title": f"message draft {draft['draft_id']}", "jobs": [{
        "key": "draft", "op": OP, "args": draft, "device": device_id, "max_attempts": 1, "idempotent": False,
        "ui_exclusive": True, "deadline_s": 3600}]}


def redact_args(args_json):
    """Admin/API view of a message job's args: recipients count and body hash only."""
    try:
        a = json.loads(args_json or "{}")
    except ValueError:
        return {}
    return {"draft_id": a.get("draft_id"), "kind": a.get("kind"), "recipients": len(a.get("to", [])) + len(a.get("cc", [])) + len(a.get("bcc", [])),
            "body_sha256": hashlib.sha256(str(a.get("body", "")).encode()).hexdigest()[:16]}


def scrub_job(db, job_id):
    """Replace a finished message job's stored args with the redacted form (and the plan's spec copy)."""
    row = db.execute("SELECT args_json, plan_id, op FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not row["op"].startswith("message."):
        return
    db.execute("UPDATE jobs SET args_json=? WHERE id=?", (json.dumps(redact_args(row["args_json"])), job_id))
    db.execute("UPDATE plans SET spec_json=? WHERE id=?", (json.dumps({"redacted": True}), row["plan_id"]))


def draft_status(job):
    """Map a job row (dict-like) to one of DRAFT_STATES plus a human detail. Never says 'submitted' without the phone saying so."""
    st = job["state"]
    result = json.loads(job["result_json"]) if job["result_json"] else {}
    err = json.loads(job["error_json"]) if job["error_json"] else {}
    if st == "succeeded":
        r = result.get("status")
        if r == "submitted":
            return "submitted", "The owner pressed Send on the phone."
        if r == "declined":
            return "declined", "The owner declined the draft; nothing was sent."
        return "unknown", "The phone reported an uncertain result; check the phone before resending."
    if st in ("needs_review", "cancelled_unconfirmed"):
        return "unknown", "The phone stopped answering after receiving the draft; it may or may not have been sent. Check the phone."
    if st == "failed":
        return "failed", err.get("message", "The phone could not process the draft.")
    if st in ("cancelled", "expired", "dep_failed"):
        return "failed", f"Draft {st} before the owner acted."
    if st in ("running", "awaiting_approval", "cancelling"):
        return "draft_received", "The draft is on the phone, waiting for the owner."
    return "queued", "Waiting for the phone to come online."


# ---- per-device feature availability for the Control Center / CLI ------------------------------

FEATURES = {
    "messaging": ("message.draft", "VictorSMS"),
    "screen_view": ("screen.view", "scrcpy/rustdeskV"),
    "manual_control": ("screen.control", "scrcpy/rustdeskV"),
    "terminal": ("terminal.open", "VictorSuite"),
}
MIN_MINOR = {"messaging": 1, "screen_view": 1, "manual_control": 1, "terminal": 1}


def feature_states(device):
    """Independent state per feature: available | needs_permission | unsupported | offline. Each carries an action to take."""
    caps = device.get("capabilities") or {}
    ops = caps.get("operations", {})
    online = bool(device.get("connected"))
    out = {}
    for name, (op, provider) in FEATURES.items():
        o = ops.get(op)
        if not online:
            out[name] = {"state": "offline", "fix": "Turn the phone on and connect it to the fleet; it reports capabilities when it reconnects."}
        elif o is None:
            out[name] = {"state": "unsupported", "fix": f"Update {provider} (and DroidCommand) on the phone; this version does not offer {op}."}
        elif not o.get("available"):
            out[name] = {"state": "unsupported", "fix": f"{provider} reports {op} unavailable. Open it on the phone to see why."}
        elif o.get("granted") is False:
            out[name] = {"state": "needs_permission", "fix": f"Grant the permission for {op} on the phone."}
        else:
            out[name] = {"state": "available", "fix": ""}
    return out
