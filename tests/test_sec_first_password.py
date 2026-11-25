"""F-13: the FIRST owner password is set only on the physical console, never by a process running as the owner.

`victor` has NOPASSWD sudo to victor-priv, so every program the owner runs (browser, model server, ...) can call it. While the
account is locked there is no password to prove, so victor-priv refuses to set one; the root-only victor-owner-setup, started
by a systemd unit on tty1 before the login screen, is the only way."""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEB = os.path.join(ROOT, "debian-edition", "config", "includes.chroot_after_packages")
DEB_LIB = os.path.join(DEB, "usr", "local", "lib", "victorlinux")
PI = os.path.join(ROOT, "pi-edition", "overlay")
PI_DIR = os.path.join(PI, "usr", "local", "lib", "victorlinux", "pi")
sys.path.insert(0, DEB_LIB)
sys.path.insert(1, PI_DIR)

from victorlib import ownertrust  # noqa: E402

UID, GID = os.getuid(), os.getgid()
UUID_A = "11111111-2222-3333-4444-555555555555"


def make_hash(pw):
    return subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=pw + "\n", capture_output=True, text=True).stdout.strip()


OWNER_HASH = make_hash("correct horse 42")


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


@pytest.fixture
def machine(monkeypatch, tmp_path):
    monkeypatch.setattr(ownertrust, "TRUST_DIR", str(tmp_path / "trust"))
    data = tmp_path / "data"
    (data / "secrets").mkdir(parents=True)
    drive = {"mountpoint": str(data), "uuid": UUID_A}
    monkeypatch.setattr(ownertrust, "current_drive", lambda mountpoint=None: drive)
    shadow = tmp_path / "shadow"
    monkeypatch.setattr(ownertrust, "SHADOW", str(shadow))
    return types.SimpleNamespace(tmp=tmp_path, data=data, secrets=data / "secrets", drive=drive, shadow=shadow)


def set_shadow(machine, h):
    machine.shadow.write_text(f"root:*:19000:0:99999:7:::\nvictor:{h}:19000:0:99999:7:::\n")


@pytest.fixture
def priv(monkeypatch, machine):
    m = load("victor_priv_f13", os.path.join(DEB_LIB, "victor-priv"))
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=UID, pw_gid=GID))
    monkeypatch.setattr("builtins.open", _open_shadow_aware(machine), raising=True) if False else None
    calls = []
    monkeypatch.setattr(m, "run", lambda cmd, **kw: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(m, "owner_hash", ownertrust.owner_hash)
    m.calls = calls
    return m


def _open_shadow_aware(machine):
    return open


# ------------------------------------------------------------------ the attack from the audit
def test_a_process_running_as_victor_cannot_set_the_first_password(priv, machine, monkeypatch, capsys):
    """The audit's two-command takeover: `sudo victor-priv set-password` while the account is locked."""
    set_shadow(machine, "!")
    monkeypatch.setattr(sys, "stdin", io.StringIO("attacker-chosen-pw\n"))
    with pytest.raises(SystemExit) as e:
        priv.cmd_password()
    assert e.value.code != 0
    assert priv.calls == []                                   # no usermod -p, no `usermod -aG sudo`
    assert "physical console" in capsys.readouterr().err


@pytest.mark.parametrize("locked", ["!", "*", "", "!$6$salt$hash"])
def test_every_locked_form_is_refused(priv, machine, monkeypatch, locked):
    set_shadow(machine, locked)
    monkeypatch.setattr(sys, "stdin", io.StringIO("attacker-chosen-pw\n"))
    with pytest.raises(SystemExit):
        priv.cmd_password()
    assert priv.calls == []


def test_changing_an_existing_password_still_needs_the_current_one(priv, machine, monkeypatch):
    set_shadow(machine, OWNER_HASH)
    monkeypatch.setattr(sys, "stdin", io.StringIO("wrong\nnewpass1234\n"))
    monkeypatch.setattr(priv, "run", lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, **kw) if cmd[0] == "openssl"
                        else priv.calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""))
    with pytest.raises(SystemExit):
        priv.cmd_password()
    assert priv.calls == []
    monkeypatch.setattr(sys, "stdin", io.StringIO("correct horse 42\nnewpass1234\n"))
    monkeypatch.setattr(priv, "persist_owner_hash", lambda h: None)
    priv.cmd_password()
    assert any(c[:2] == ["usermod", "-p"] for c in priv.calls)


def test_reset_owner_locks_the_account_and_drops_sudo(priv, machine, monkeypatch):
    set_shadow(machine, "!")                       # nothing to prove, nothing to protect: just make sure it is closed
    monkeypatch.setattr(priv, "cmd_revoke", lambda: None)
    priv.cmd_reset_owner()
    assert ["passwd", "-l", "victor"] in priv.calls and ["gpasswd", "-d", "victor", "sudo"] in priv.calls


def test_reset_owner_with_a_password_needs_that_password(priv, machine, monkeypatch):
    set_shadow(machine, OWNER_HASH)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not it\n"))
    monkeypatch.setattr(priv, "run", lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, **kw) if cmd[0] == "openssl"
                        else priv.calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""))
    with pytest.raises(SystemExit):
        priv.cmd_reset_owner()
    assert priv.calls == []


# ------------------------------------------------------------------ the console flow (root only)
@pytest.fixture
def setup(monkeypatch, machine):
    m = load("victor_owner_setup_f13", os.path.join(DEB_LIB, "victor-owner-setup"))
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=UID, pw_gid=GID))
    monkeypatch.setattr(m, "switch_to_console", lambda: None)
    monkeypatch.setattr(m, "describe", lambda d: f"UUID {d['uuid']}")
    calls = []
    real = m.run

    def fake_run(cmd, **kw):
        if cmd[0] == "openssl":
            return real(cmd, **kw)
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(m, "run", fake_run)
    m.calls = calls
    return m


class FakeConsole:
    def __init__(self, answers):
        self.answers, self.said, self.asked = list(answers), [], 0

    def say(self, t=""):
        self.said.append(t)

    def ask(self, prompt, secret=False):
        self.asked += 1
        return self.answers.pop(0) if self.answers else None


def test_console_sets_the_first_password_gives_sudo_and_seals_it(setup, machine):
    set_shadow(machine, "!")
    con = FakeConsole(["correct horse 42", "correct horse 42"])
    assert setup.first_password(con) == 0
    p = [c for c in setup.calls if c[:2] == ["usermod", "-p"]]
    assert len(p) == 1 and ownertrust.verify_password(p[0][2], "correct horse 42")
    assert ["usermod", "-aG", "sudo", "victor"] in setup.calls
    state, text = ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"]
    assert state == "trusted" and ownertrust.hash_text_of(text) == p[0][2]


def test_console_rejects_short_and_mismatched_passwords(setup, machine):
    set_shadow(machine, "!")
    con = FakeConsole(["short", "correct horse 42", "different pw 99", "correct horse 42", "correct horse 42"])
    assert setup.first_password(con) == 0
    assert len([c for c in setup.calls if c[:2] == ["usermod", "-p"]]) == 1


def test_enter_skips_and_the_account_stays_locked(setup, machine):
    set_shadow(machine, "!")
    for answers in ([""], [], ["correct horse 42", None]):      # Enter, timeout/EOF, EOF at the repeat prompt
        con = FakeConsole(answers)
        assert setup.first_password(con) == 1
    assert setup.calls == []


def test_require_mode_cannot_be_skipped(setup, machine):
    set_shadow(machine, "!")
    con = FakeConsole(["", "short", "correct horse 42", "typo typo typo", "correct horse 42", "correct horse 42"])
    assert setup.first_password(con, require=True) == 0
    assert len([c for c in setup.calls if c[:2] == ["usermod", "-p"]]) == 1


def test_no_prompt_when_the_owner_already_has_a_password(setup, machine):
    set_shadow(machine, OWNER_HASH)
    con = FakeConsole(["x"])
    assert setup.first_password(con) == 0 and con.asked == 0 and setup.calls == []


def test_first_password_without_a_data_drive_still_works(setup, machine, monkeypatch):
    set_shadow(machine, "!")
    monkeypatch.setattr(ownertrust, "current_drive", lambda mountpoint=None: None)
    assert setup.first_password(FakeConsole(["correct horse 42", "correct horse 42"])) == 0


def test_a_new_password_replaces_an_unapproved_hash_on_a_hostile_drive(setup, machine):
    set_shadow(machine, "!")
    (machine.secrets / "owner.shadow").write_text(json.dumps({"hash": make_hash("attacker-pw-1")}))
    setup.first_password(FakeConsole(["correct horse 42", "correct horse 42"]))
    state, text = ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"]
    assert state == "trusted" and ownertrust.verify_password(ownertrust.hash_text_of(text), "correct horse 42")


def test_a_failing_usermod_is_reported_not_swallowed(setup, machine, monkeypatch):
    set_shadow(machine, "!")
    real = setup.run
    monkeypatch.setattr(setup, "run", lambda cmd, **kw: real(cmd, **kw) if cmd[0] == "openssl" else subprocess.CompletedProcess(cmd, 1, "", "boom"))
    con = FakeConsole(["correct horse 42", "correct horse 42"])
    assert setup.first_password(con) == 2 and any("boom" in s for s in con.said)


def test_console_runs_drive_approval_before_asking_for_a_new_password(setup, machine, monkeypatch):
    set_shadow(machine, "!")
    order = []
    monkeypatch.setattr(setup, "approve", lambda con, run_priv=None: order.append("approve") or 0)
    monkeypatch.setattr(setup, "first_password", lambda con, require=False: order.append(("first", require)) or 0)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    assert setup.main(["x", "console"]) == 0
    assert order == ["approve", ("first", False)]
    order.clear()
    assert setup.main(["x", "console", "--require"]) == 0 and order == ["approve", ("first", True)]
    order.clear()
    assert setup.main(["x", "approve"]) == 0 and order == ["approve"]


def test_a_skipped_console_is_not_an_error_but_a_failed_require_is(setup, monkeypatch):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.setattr(setup, "approve", lambda con, run_priv=None: 0)
    monkeypatch.setattr(setup, "first_password", lambda con, require=False: 1)
    assert setup.main(["x", "console"]) == 0
    assert setup.main(["x", "console", "--require"]) == 2


# ------------------------------------------------------------------ nothing else can set the password
def test_nothing_but_victor_priv_changes_calls_set_password():
    """The wizards and first-run window used to call `victor-priv set-password`; none may any more."""
    import re
    pat = re.compile(r"(PRIV|VPRIV)[^\n]*set-password|victor-priv[^\n]*set-password")
    callers = []
    for base in (DEB, PI):
        for dp, _, files in os.walk(base):
            if "__pycache__" in dp:
                continue
            for f in files:
                if f in ("victor-priv", "victor-owner-setup", "victor-pi-priv", "vm_test.py"):
                    continue          # the verb's own definition, and prose/attack text that names it
                p = os.path.join(dp, f)
                try:
                    text = open(p, errors="replace").read()
                except OSError:
                    continue
                if pat.search(text):
                    callers.append(os.path.relpath(p, ROOT))
    assert callers == []


def test_priv_set_password_has_no_unauthenticated_branch():
    src = open(os.path.join(DEB_LIB, "victor-priv")).read()
    body = src[src.index("def require_current_password"):src.index("def cmd_password")]
    assert "return" not in body.split('h = owner_hash()')[1].split("cur = ")[0].replace("die(FIRST_PASSWORD_MSG)", "")


def test_pi_wizards_no_longer_set_the_password_themselves():
    tui = open(os.path.join(PI, "usr/local/bin/victor-pi-setup-tui")).read()
    tk = open(os.path.join(PI, "usr/local/bin/victor-pi-setup")).read()
    firstrun = open(os.path.join(DEB, "usr/local/bin/victor-firstrun")).read()
    assert "victor-owner-setup" in tui and "console --require" in tui and "set-password" not in tui
    assert "set-password" not in tk and '"p1"' not in tk
    assert "set-password" not in firstrun and "victor-owner-setup" in firstrun


def test_pi_mark_done_refuses_while_the_owner_has_no_password(monkeypatch, tmp_path):
    pi = load("victor_pi_priv_f13", os.path.join(PI_DIR, "victor-pi-priv"))
    monkeypatch.setattr(pi, "owner_locked", lambda: True)
    monkeypatch.setattr(pi, "STATE", str(tmp_path)); monkeypatch.setattr(pi, "DONE", str(tmp_path / "firstboot.done"))
    with pytest.raises(SystemExit):
        pi.cmd_mark_done()
    assert not (tmp_path / "firstboot.done").exists()
    monkeypatch.setattr(pi, "owner_locked", lambda: False)
    pi.cmd_mark_done()
    assert (tmp_path / "firstboot.done").exists()


def test_headless_pi_console_service_only_approves_and_the_wizard_sets_the_password():
    hook = open(os.path.join(ROOT, "pi-edition/hooks/10-pi-config.sh")).read()
    assert "victor-owner-console.service.d/headless.conf" in hook and "victor-owner-setup approve --timeout 120" in hook
    unit = open(os.path.join(DEB, "etc/systemd/system/victor-owner-console.service")).read()
    assert "victor-owner-setup console --timeout 120" in unit and "TTYPath=/dev/tty1" in unit and "StandardInput=tty" in unit


def test_owner_setup_is_still_root_only_and_not_sudoers_reachable():
    for rel in ("debian-edition/config/includes.chroot_after_packages/etc/sudoers.d/victor-priv",
                "pi-edition/overlay/etc/sudoers.d/victor-pi-priv", "airootfs/etc/sudoers.d/10-victor"):
        assert "owner-setup" not in open(os.path.join(ROOT, rel)).read()
    # the sudoers lines name exact files; neither names a shell, ALL, or a wildcard on the Debian/Pi side
    for rel in ("debian-edition/config/includes.chroot_after_packages/etc/sudoers.d/victor-priv", "pi-edition/overlay/etc/sudoers.d/victor-pi-priv"):
        for line in open(os.path.join(ROOT, rel)):
            if line.startswith("victor "):
                assert "NOPASSWD: ALL" not in line and "*" not in line
