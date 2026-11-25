"""Versioned contract for the VictorLinux security-tool execution backend (protocol_major 1).

This is a SEPARATE contract from `fleet/victorfleet/protocol.py` (the Android-fleet
coordinator/worker protocol) and from `gateway.py` (inference-only, says so in its own
docstring: "there is no tool execution, file access or shell on this port"). Running
Metasploit/SET is a different trust boundary and a different process lifecycle from either
of those, so it gets its own small HTTP+JSON contract instead of being bolted onto one of
them. `secbackend.py` is the only process that imports this module to serve it; DroidCommand
AI's Kotlin client (`core-tools-metasploit`/`core-tools-setoolkit`) is the only intended
caller, but any client can implement this contract against the fixtures below.

Canonical schema ownership: this module. `tests/test_secprotocol.py` is the executable
specification; DroidCommand's Kotlin tests build requests/responses shaped exactly like the
dicts validated here (see `docs/security-backend.md` for the field-level reference and the
worked examples both ends test against).

Compatibility rules: unknown *optional* response fields are ignored by a well-behaved
client; an unsupported `protocol_major` or unknown job `op` is rejected with a structured
error, never silently run. Minor versions only add optional fields.
"""
import re
import time
import uuid

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
OP_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
MAX_STRING = 4096
MAX_OPTION_VALUE = 8192
MAX_OPTIONS = 64

# Jobs this contract knows how to run. One tool, one op name - no generic "run a command" op.
OPS = {"metasploit.module", "setoolkit.attack"}

# Job lifecycle. TERMINAL states never transition further; a job that reaches one stays
# there forever in the store. "interrupted" is reached only by the backend itself, on
# restart, for a job it cannot prove either finished or stopped - never by a client request.
JOB_STATES = ("queued", "running", "cancelling", "completed", "failed", "cancelled", "interrupted")
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})

# Allowed state transitions, backend-enforced. A transition not listed here is rejected by
# JobStore.transition() with a RuntimeError, not silently applied.
TRANSITIONS = {
    "queued": {"running", "cancelled", "interrupted"},
    "running": {"cancelling", "completed", "failed", "interrupted"},
    "cancelling": {"cancelled", "completed", "failed", "interrupted"},
}

ERROR_CODES = frozenset({
    "bad_request", "unauthorized", "forbidden", "unknown_op", "unsupported_protocol",
    "unknown_job", "not_job_owner", "idempotency_conflict", "capability_missing",
    "tool_not_installed", "tool_version_unsupported", "approval_required", "device_busy",
    "timeout", "storage_unavailable", "cancelled", "executor_unavailable", "internal",
})

# Separate, and deliberately coarser than execution result: a transport/HTTP failure (bad
# auth, malformed JSON, network error) is never conflated with "the tool ran and failed" or
# "the tool exited nonzero". See docs/security-backend.md#error-taxonomy.
TRANSPORT_ERROR_CODES = frozenset({
    "bad_request", "unauthorized", "forbidden", "unknown_op", "unsupported_protocol",
    "unknown_job", "not_job_owner", "idempotency_conflict", "capability_missing",
    "device_busy", "storage_unavailable", "internal",
})


class ProtocolError(Exception):
    """Raised for anything that must become a structured HTTP error, never a 200 with a lie in it."""

    def __init__(self, code, message, http_status=None):
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status or _DEFAULT_HTTP_STATUS.get(code, 400)

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message}}


_DEFAULT_HTTP_STATUS = {
    "unauthorized": 401, "forbidden": 403, "not_job_owner": 403,
    "unknown_job": 404, "unknown_op": 404,
    "idempotency_conflict": 409, "device_busy": 409,
    "approval_required": 412, "capability_missing": 412, "tool_not_installed": 412,
    "tool_version_unsupported": 412,
    "unsupported_protocol": 426,
    "timeout": 504, "internal": 500, "storage_unavailable": 503, "executor_unavailable": 503,
}


def now_ms():
    return int(time.time() * 1000)


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def _need(d, key, types, code="bad_request"):
    if key not in d or not isinstance(d[key], types) or (types is int and isinstance(d[key], bool)):
        raise ProtocolError(code, f"field '{key}' missing or wrong type")
    return d[key]


def _str(d, key, maxlen=MAX_STRING, optional=False, default=None):
    if key not in d:
        if optional:
            return default
        raise ProtocolError("bad_request", f"field '{key}' is required")
    v = _need(d, key, str)
    if len(v) > maxlen:
        raise ProtocolError("bad_request", f"'{key}' exceeds {maxlen} characters")
    return v


def _no_control(value, name):
    """A value that ends up as one line of a Metasploit console / SET stdin sequence must not carry any control character:
    a bare "\\n" would start a second console command (msfconsole has `irb`/`ruby`, i.e. code execution)."""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ProtocolError("bad_request", f"'{name}' contains a control character")
    return value


def _id(d, key, optional=False, default=None):
    v = _str(d, key, 96, optional, default)
    if v is not None and not ID_RE.fullmatch(v):
        raise ProtocolError("bad_request", f"'{key}' is not a valid identifier")
    return v


def validate_options(options):
    """Shared validation for `metasploit.module.options` and `setoolkit.attack.menuAnswers`/
    `options` maps: bounded count, bounded value length, no control characters that could
    inject an extra line into a resource script or a stdin sequence."""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ProtocolError("bad_request", "options must be an object")
    if len(options) > MAX_OPTIONS:
        raise ProtocolError("bad_request", f"more than {MAX_OPTIONS} options")
    out = {}
    for k, v in options.items():
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", k):
            raise ProtocolError("bad_request", f"option key '{k}' invalid")
        if not isinstance(v, str):
            raise ProtocolError("bad_request", f"option '{k}' value must be a string")
        if len(v) > MAX_OPTION_VALUE:
            raise ProtocolError("bad_request", f"option '{k}' value too long")
        _no_control(v, f"option {k}")
        out[k] = v
    return out


def validate_job_submit(body):
    """Validates a POST /security/v1/jobs request body. Returns the normalized dict.

    Required: op, idempotency_key, params (op-specific). idempotency_key lets a client retry
    a dropped HTTP response without re-running the job: the SAME key with the SAME params
    returns the already-created job; the SAME key with DIFFERENT params is a 409
    idempotency_conflict, never a silent second run with new parameters.
    """
    if not isinstance(body, dict):
        raise ProtocolError("bad_request", "request body must be a JSON object")
    op = _str(body, "op", 64)
    if op not in OPS:
        raise ProtocolError("unknown_op", f"unknown op '{op}'")
    idem = _id(body, "idempotency_key")
    params = _need(body, "params", dict)
    if op == "metasploit.module":
        _validate_metasploit_params(params)
    elif op == "setoolkit.attack":
        _validate_setoolkit_params(params)
    timeout_ms = body.get("timeout_ms", 180_000)
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1000 <= timeout_ms <= 3_600_000:
        raise ProtocolError("bad_request", "timeout_ms must be an integer between 1000 and 3600000")
    return {"op": op, "idempotency_key": idem, "params": params, "timeout_ms": timeout_ms}


def _validate_metasploit_params(p):
    _str(p, "module_type", 16)
    if p["module_type"] not in ("exploit", "auxiliary", "post"):
        raise ProtocolError("bad_request", "module_type must be exploit|auxiliary|post")
    mod = _str(p, "module_path", 256)
    if not re.fullmatch(r"[a-z0-9_./-]{1,256}", mod):
        raise ProtocolError("bad_request", "module_path has an unsafe shape")
    target = _no_control(_str(p, "target_host", 256), "target_host")
    if not target.strip():
        raise ProtocolError("bad_request", "target_host must not be blank")
    p["target_port"] = p.get("target_port")
    if p["target_port"] is not None and not (isinstance(p["target_port"], int) and 1 <= p["target_port"] <= 65535):
        raise ProtocolError("bad_request", "target_port must be 1-65535")
    validate_options(p.get("options"))
    if "payload" in p and p["payload"] is not None:
        _no_control(_str(p, "payload", 256), "payload")


def _validate_setoolkit_params(p):
    vec = _str(p, "attack_vector", 64)
    from secset import SUPPORTED_VECTORS  # local import: keeps secprotocol importable with zero deps
    if vec not in SUPPORTED_VECTORS and vec not in UNSUPPORTED_SET_VECTOR_REASONS:
        raise ProtocolError("bad_request", f"unknown attack_vector '{vec}'")
    if vec in UNSUPPORTED_SET_VECTOR_REASONS:
        raise ProtocolError("capability_missing", UNSUPPORTED_SET_VECTOR_REASONS[vec])
    host = p.get("target_host")
    email = p.get("target_email")
    if not host and not email:
        raise ProtocolError("bad_request", "setoolkit.attack requires target_host or target_email")
    if host is not None:
        _no_control(_str(p, "target_host", 256), "target_host")
    if email is not None:
        _no_control(_str(p, "target_email", 256), "target_email")
    validate_options(p.get("menu_answers_by_index") or {})


# Attack vectors this backend will not run unattended, and the specific, real reason -
# established 2026-11-19 by reading the actual upstream source at the pinned commit this
# backend was verified against (see docs/security-backend.md#setoolkit-coverage), not guessed.
UNSUPPORTED_SET_VECTOR_REASONS = {
    "SMS_SPOOFING": (
        "SET removed SMS Spoofing from its main attack-vector menu; the vector no longer "
        "exists in the installed version (verified against trustedsec/social-engineer-toolkit "
        "@5e0fbb5, v8.1.3 'Maverick' - menu now has 10 entries ending in Third Party Modules, "
        "none of them SMS). An older installation that still has it needs its own menu digit "
        "supplied via a future per-install override; this backend does not guess one."
    ),
    "SPEAR_PHISHING": (
        "Spear-Phishing's own submenu (template choice, attachment type, SMTP vs sendmail, "
        "recipient list vs single target) branches far enough that a single validated digit "
        "sequence does not cover it safely, and it can send real email as a side effect. "
        "Not run unattended by this backend; use setoolkit interactively for this vector."
    ),
    "WEBSITE_ATTACK": (
        "Website Attack Vectors starts a long-lived local web server/listener as a side "
        "effect and its sub-menu (clone vs template vs custom, which exact page) is not yet "
        "covered by a validated digit sequence. Not run unattended by this backend."
    ),
    "INFECTIOUS_MEDIA_GENERATOR": (
        "Infectious Media Generator's payload/listener sub-menu is not yet covered by a "
        "validated digit sequence and writes payload files with real execution content. "
        "Not run unattended by this backend."
    ),
    "MASS_MAILER": (
        "Mass Mailer can send real email to real addresses as its only observable effect; "
        "this backend does not automate it unattended regardless of menu-mapping accuracy."
    ),
}
