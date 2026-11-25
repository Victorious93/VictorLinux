"""victor-voip: generated Asterisk config carries SIP passwords, so it must be owner-only and the trunk password must not
be able to add config lines. (The real-Asterisk integration tests live in voip/tests and are skipped without asterisk.)"""
import io
import os
import shutil
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "voip"))

import victor_voip as V  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def vdir(tmp_path, monkeypatch):
    d = tmp_path / "voip"
    monkeypatch.setenv("VICTOR_VOIP_DIR", str(d))
    monkeypatch.setattr(V, "moddir", lambda: "/usr/lib/asterisk/modules")
    old = os.umask(0o022)                     # a normal desktop umask: files would be world-readable by default
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            V.main(["init", "--host", "127.0.0.1", "--port", "15099"])
            V.main(["add-device", "alice", "--pstn"])
    finally:
        os.umask(old)
    return d


def test_data_dir_etc_and_tls_are_private(vdir):
    for sub in ("", "etc", "tls"):
        assert _mode(vdir / sub) == 0o700, sub


def test_generated_config_is_owner_only_and_holds_the_password(vdir):
    pj = vdir / "etc" / "pjsip.conf"
    assert pj.exists() and _mode(pj) == 0o600
    assert "password=" in pj.read_text()
    assert _mode(vdir / "secrets.json") == 0o600


def test_render_does_not_write_through_a_planted_symlink(vdir, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    pj = vdir / "etc" / "pjsip.conf"
    pj.unlink()
    (vdir / "etc" / f"pjsip.conf.{os.getpid()}.tmp").symlink_to(victim)
    with pytest.raises(OSError):
        V.render(str(vdir))
    assert victim.read_text() == "keep"


@pytest.mark.parametrize("pw", ["ok\nendpoint=evil", "a b", "x;y", "", "p" * 129, "tab\there"])
def test_trunk_password_rejected_when_it_could_alter_the_config(pw):
    assert not V.valid_secret(pw)


def test_trunk_password_accepts_normal_secrets(vdir, monkeypatch):
    assert V.valid_secret("Correct-Horse_Battery.Staple!42")
    monkeypatch.setenv("VOIP_TRUNK_PASSWORD", "bad\nendpoint=evil")
    with pytest.raises(SystemExit):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            V.main(["trunk", "--host", "sip.example.com", "--user", "u1"])
    assert "evil" not in (vdir / "etc" / "pjsip.conf").read_text()
