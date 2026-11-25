import pytest

import secprotocol as proto


def test_validate_job_submit_metasploit_ok():
    body = {
        "op": "metasploit.module", "idempotency_key": "abc-123",
        "params": {"module_type": "auxiliary", "module_path": "scanner/portscan/tcp",
                   "target_host": "127.0.0.1", "options": {"PORTS": "1-100"}},
    }
    out = proto.validate_job_submit(body)
    assert out["op"] == "metasploit.module"
    assert out["timeout_ms"] == 180_000


def test_validate_job_submit_rejects_unknown_op():
    with pytest.raises(proto.ProtocolError) as e:
        proto.validate_job_submit({"op": "shell.run", "idempotency_key": "x", "params": {}})
    assert e.value.code == "unknown_op"


def test_validate_job_submit_rejects_bad_idempotency_key():
    with pytest.raises(proto.ProtocolError) as e:
        proto.validate_job_submit({
            "op": "metasploit.module", "idempotency_key": "has a space",
            "params": {"module_type": "auxiliary", "module_path": "x", "target_host": "h"},
        })
    assert e.value.code == "bad_request"


def test_metasploit_requires_target_host():
    with pytest.raises(proto.ProtocolError):
        proto.validate_job_submit({
            "op": "metasploit.module", "idempotency_key": "k",
            "params": {"module_type": "auxiliary", "module_path": "scanner/portscan/tcp", "target_host": "   "},
        })


def test_metasploit_rejects_unsafe_module_path():
    with pytest.raises(proto.ProtocolError):
        proto.validate_job_submit({
            "op": "metasploit.module", "idempotency_key": "k",
            "params": {"module_type": "auxiliary", "module_path": "scanner; rm -rf /", "target_host": "h"},
        })


def test_setoolkit_supported_vector_ok():
    out = proto.validate_job_submit({
        "op": "setoolkit.attack", "idempotency_key": "k",
        "params": {"attack_vector": "QRCODE_GENERATOR", "target_host": "https://example.com"},
    })
    assert out["params"]["attack_vector"] == "QRCODE_GENERATOR"


def test_setoolkit_sms_spoofing_is_capability_missing_not_unknown():
    """SMS_SPOOFING was a real SET menu item once; this backend must say it is MISSING,
    specifically, not reject it as an unrecognized value - see secset.py's doc comment."""
    with pytest.raises(proto.ProtocolError) as e:
        proto.validate_job_submit({
            "op": "setoolkit.attack", "idempotency_key": "k",
            "params": {"attack_vector": "SMS_SPOOFING", "target_host": "+15555550123"},
        })
    assert e.value.code == "capability_missing"
    assert "removed" in e.value.message.lower()


def test_setoolkit_requires_target():
    with pytest.raises(proto.ProtocolError):
        proto.validate_job_submit({
            "op": "setoolkit.attack", "idempotency_key": "k",
            "params": {"attack_vector": "QRCODE_GENERATOR"},
        })


def test_validate_options_rejects_control_chars():
    with pytest.raises(proto.ProtocolError):
        proto.validate_options({"LHOST": "1.2.3.4\r\nEXTRA=injected"})


def test_validate_options_rejects_too_many():
    with pytest.raises(proto.ProtocolError):
        proto.validate_options({f"K{i}": "v" for i in range(proto.MAX_OPTIONS + 1)})


def test_error_to_dict_shape():
    e = proto.ProtocolError("unknown_job", "no job 'x'")
    d = e.to_dict()
    assert d == {"error": {"code": "unknown_job", "message": "no job 'x'"}}
    assert e.http_status == 404


@pytest.mark.parametrize("frm,to,ok", [
    ("queued", "running", True),
    ("queued", "completed", False),
    ("running", "cancelling", True),
    ("running", "queued", False),
    ("cancelling", "cancelled", True),
    ("completed", "running", False),
])
def test_transitions_table(frm, to, ok):
    allowed = proto.TRANSITIONS.get(frm, set())
    assert (to in allowed) == ok


# ---- sec: a bare newline must not start a second Metasploit console command -------------------------------------------

def _msf(**over):
    p = {"module_type": "auxiliary", "module_path": "scanner/portscan/tcp", "target_host": "127.0.0.1"}
    p.update(over)
    return {"op": "metasploit.module", "idempotency_key": "k1", "params": p}


@pytest.mark.parametrize("params", [
    {"options": {"PORTS": "1\nirb"}},
    {"options": {"PORTS": "1\x1b[2J"}},
    {"options": {"PORTS\n": "x"}},                       # `$` used to accept a trailing newline in the key
    {"target_host": "127.0.0.1\nirb"},
    {"payload": "generic/shell\nirb"},
    {"module_path": "scanner/portscan/tcp\n"},
])
def test_metasploit_params_reject_line_breaks_and_control_characters(params):
    with pytest.raises(proto.ProtocolError) as e:
        proto.validate_job_submit(_msf(**params))
    assert e.value.code == "bad_request"


def test_idempotency_key_rejects_trailing_newline():
    body = _msf()
    body["idempotency_key"] = "abc\n"
    with pytest.raises(proto.ProtocolError):
        proto.validate_job_submit(body)


def test_setoolkit_target_fields_reject_control_characters():
    from secset import SUPPORTED_VECTORS
    vec = sorted(SUPPORTED_VECTORS)[0]
    for field in ("target_host", "target_email"):
        with pytest.raises(proto.ProtocolError):
            proto.validate_job_submit({"op": "setoolkit.attack", "idempotency_key": "k",
                                       "params": {"attack_vector": vec, field: "a@b.c\nexit"}})


def test_plain_values_still_accepted():
    out = proto.validate_job_submit(_msf(options={"PORTS": "1-100,443", "RHOSTS": "10.0.0.0/24 10.0.1.5"},
                                         payload="generic/shell_reverse_tcp"))
    assert out["params"]["options"]["PORTS"] == "1-100,443"
