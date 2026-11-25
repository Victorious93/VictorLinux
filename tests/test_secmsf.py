"""secmsf.py tests. Pure-logic tests always run. The LIVE tests at the bottom are REAL
integration tests against a real msfrpcd - not mocks - and are skipped (not faked) when one
is not reachable. Point them at your own instance with:

    MSF_RPC_HOST=127.0.0.1 MSF_RPC_PORT=55553 MSF_RPC_USER=victor MSF_RPC_PASS=... \
        python3 -m pytest tests/test_secmsf.py -v

This is exactly how the 2026-11-19 verification in docs/security-backend.md was run: a real
`msfrpcd` built from a pinned rapid7/metasploit-framework checkout, started with
`msfrpcd -U <user> -P <pass> -a 127.0.0.1 -p <port> -S`.
"""
import os

import pytest

import secmsf


def test_unwrap_keys_normalizes_bytes():
    assert secmsf._unwrap_keys({b"token": b"abc", "already_str": 1}) == {"token": b"abc", "already_str": 1}


def test_rejects_unsupported_module_type():
    c = object.__new__(secmsf.MetasploitRpcClient)  # avoid __init__'s msgpack import check
    c._token = "t"
    c.host, c.port, c.timeout, c.ssl_context = "127.0.0.1", 1, 1, None
    with pytest.raises(ValueError):
        c.run_module_via_console("not_a_real_type", "x", {})


def _live_client():
    host = os.environ.get("MSF_RPC_HOST")
    if not host:
        return None
    c = secmsf.MetasploitRpcClient(host, int(os.environ.get("MSF_RPC_PORT", secmsf.DEFAULT_PORT)), timeout=15)
    c.login(os.environ["MSF_RPC_USER"], os.environ["MSF_RPC_PASS"])
    return c


live = pytest.mark.skipif(not os.environ.get("MSF_RPC_HOST"),
                           reason="set MSF_RPC_HOST/PORT/USER/PASS to run against a real msfrpcd")


@live
def test_live_auth_and_version():
    c = _live_client()
    v = c.version()
    assert "version" in v
    c.logout()


@live
def test_live_console_runs_real_portscan_and_returns_real_output():
    c = _live_client()
    try:
        out = c.run_module_via_console(
            "auxiliary", "scanner/portscan/tcp",
            {"RHOSTS": "127.0.0.1", "PORTS": "1-100", "THREADS": "5"},
            timeout_s=30,
        )
    finally:
        c.logout()
    assert "Auxiliary module execution completed" in out


@live
def test_live_cancellation_reaches_the_real_console():
    import time
    c = _live_client()
    start = time.monotonic()
    try:
        with pytest.raises(secmsf.MsfRpcError) as e:
            c.run_module_via_console(
                "auxiliary", "scanner/portscan/tcp",
                {"RHOSTS": "127.0.0.1", "PORTS": "1-65000", "THREADS": "1"},
                is_cancelled=lambda: time.monotonic() - start > 1.0,
                timeout_s=30, poll_interval=0.2,
            )
        assert "cancelled" in e.value.message
    finally:
        c.logout()


@live
def test_live_job_info_on_a_finished_job_is_unknown_job_not_a_crash():
    """Documents a real, observed RPC quirk: module.execute's job_id is already invalid by
    the time a fast local scan's job.info is polled, and msfrpcd reports it as a structured
    {"error": true, ...} with message 'Invalid Job' - mapped to ERR_UNKNOWN_JOB, not raised
    as an unhandled transport exception."""
    c = _live_client()
    try:
        with pytest.raises(secmsf.MsfRpcError) as e:
            c._call("job.info", c._token, "999999")
    finally:
        c.logout()
    assert e.value.code == secmsf.ERR_UNKNOWN_JOB


def test_console_write_refuses_embedded_line_breaks():
    c = object.__new__(secmsf.MetasploitRpcClient)
    sent = []
    c._call = lambda *a: sent.append(a)
    c._tok = lambda: "t"
    for bad in ("set X 1\nirb", "set X 1\r", "a\n\nb"):
        with pytest.raises(ValueError):
            c.console_write("1", bad)
    assert sent == []
    c.console_write("1", "set X 1")
    c.console_write("1", "run\n")
    assert [a[3] for a in sent] == ["set X 1\n", "run\n"]
