"""Control Center (Arch web) fleet terminal / draft actions: strict input mapping to CLI arguments, no shell, no credentials."""
import json

import pytest

import control_center as cc


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cache" / "cc.token"))
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    a = cc.App()
    calls = []
    monkeypatch.setattr(a, "fleet", lambda args, timeout=30: calls.append(list(args)) or {"id": "draft-1", "state": "pending"})
    a.calls = calls
    return a


def test_sms_draft_maps_fields_to_cli_args_without_a_shell(app):
    code, r = app.fleet_draft({"device": "dev-abc123", "channel": "sms", "to": ["+15550100"], "body": "hi there"})
    assert code == 200 and r["state"] == "pending"
    assert app.calls == [["message", "draft-sms", "dev-abc123", "--to", "+15550100", "--body", "hi there"]]


def test_email_draft_includes_cc_bcc_subject(app):
    code, _ = app.fleet_draft({"device": "dev-1", "channel": "email", "to": ["a@example.test"], "cc": ["b@example.test"], "bcc": ["c@example.test"],
                               "subject": "Hello", "body": "text"})
    assert code == 200
    assert app.calls[0] == ["message", "draft-email", "dev-1", "--to", "a@example.test", "--cc", "b@example.test", "--bcc", "c@example.test",
                            "--body", "text", "--subject", "Hello"]


@pytest.mark.parametrize("bad", [
    {"device": "../x", "channel": "sms", "to": ["+1"], "body": "x"},
    {"device": "dev-1", "channel": "fax", "to": ["+1"], "body": "x"},
    {"device": "dev-1", "channel": "sms", "to": [], "body": "x"},
    {"device": "dev-1", "channel": "sms", "to": ["--body"], "body": "x"},              # option injection
    {"device": "dev-1", "channel": "sms", "to": ["+1"], "cc": ["a@b.c"], "body": "x"},
    {"device": "dev-1", "channel": "email", "to": ["a@b.c"], "subject": "x\nBcc: e@x", "body": "x"},
    {"device": "dev-1", "channel": "email", "to": ["a@b.c"], "body": 5},
])
def test_invalid_drafts_are_refused_before_reaching_the_cli(app, bad):
    code, r = app.fleet_draft(bad)
    assert code == 400 and "error" in r
    assert app.calls == []


def test_terminal_requires_a_valid_device_id(app):
    assert app.fleet_terminal({"device": "x y; rm -rf /"})[0] == 400
    assert app.fleet_terminal({})[0] == 400
