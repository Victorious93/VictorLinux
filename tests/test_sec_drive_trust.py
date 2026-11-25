"""F-10: a data drive's saved credentials are trusted only after THIS machine sealed them.

A drive labelled VICTORDATA/TORIDATA is untrusted storage (anyone can label a partition; the owner's own session owns the
secrets folder on a mounted drive). owner.shadow and ssh_authorized_keys are restored at boot only when they carry an HMAC
made with a root-only key that never sits on the drive, bound to the drive's filesystem UUID."""
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEB_LIB = os.path.join(ROOT, "debian-edition", "config", "includes.chroot_after_packages", "usr", "local", "lib", "victorlinux")
PI_DIR = os.path.join(ROOT, "pi-edition", "overlay", "usr", "local", "lib", "victorlinux", "pi")
sys.path.insert(0, DEB_LIB)
sys.path.insert(1, PI_DIR)

from victorlib import ownertrust  # noqa: E402

UID, GID = os.getuid(), os.getgid()
UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "99999999-2222-3333-4444-555555555555"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAM phone"


def make_hash(password):
    return subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n", capture_output=True, text=True).stdout.strip()


OWNER_HASH = make_hash("correct horse 42")
ATTACKER_HASH = make_hash("attacker-known-pw")


def shadow_text(h):
    return json.dumps({"hash": h})


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A 'machine' with a private trust dir and a mounted data drive at tmp/data (UUID_A)."""
    trust = tmp_path / "trust"
    monkeypatch.setattr(ownertrust, "TRUST_DIR", str(trust))
    data = tmp_path / "data"
    (data / "secrets").mkdir(parents=True)
    monkeypatch.setattr(ownertrust.common, "DATA_MP", str(data))
    drive = {"mountpoint": str(data), "uuid": UUID_A}
    monkeypatch.setattr(ownertrust, "current_drive", lambda mountpoint=None: drive)
    return types.SimpleNamespace(trust=trust, data=data, secrets=data / "secrets", drive=drive, tmp=tmp_path)


# ------------------------------------------------------------------ the seal itself
def test_mac_binds_uuid_name_and_content():
    k = b"k" * 32
    base = ownertrust.mac(k, UUID_A, "owner.shadow", "x")
    assert base != ownertrust.mac(k, UUID_B, "owner.shadow", "x")
    assert base != ownertrust.mac(k, UUID_A, "ssh_authorized_keys", "x")
    assert base != ownertrust.mac(k, UUID_A, "owner.shadow", "y")
    assert base != ownertrust.mac(b"j" * 32, UUID_A, "owner.shadow", "x")


def test_a_sealed_file_is_trusted_and_returned_as_checked(machine):
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    state, text = ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"]
    assert state == "trusted" and ownertrust.hash_text_of(text) == OWNER_HASH
    assert oct(os.stat(machine.secrets / "owner.shadow").st_mode & 0o777) == "0o600"


def test_a_forged_unsealed_drive_is_unapproved(machine):
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    got = ownertrust.inspect(machine.drive, ownertrust.load_key(create=True))
    assert got["owner.shadow"] == ("unapproved", None) and got["ssh_authorized_keys"] == ("unapproved", None)


def test_editing_a_sealed_file_breaks_the_seal(machine):
    """The owner's own session owns the secrets folder, so a compromised session can rewrite owner.shadow."""
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    assert ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"] == ("unapproved", None)


def test_a_seal_made_with_another_key_is_rejected(machine):
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    forged = ownertrust.mac(b"\x01" * 32, UUID_A, "owner.shadow", shadow_text(ATTACKER_HASH))
    (machine.secrets / ownertrust.SEAL_NAME).write_text(json.dumps({"v": 1, "uuid": UUID_A, "macs": {"owner.shadow": forged}}))
    ownertrust.load_key(create=True)
    assert ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"][0] == "unapproved"


def test_sealed_files_copied_to_another_drive_are_rejected(machine):
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    other = machine.tmp / "other"
    (other / "secrets").mkdir(parents=True)
    for n in ("owner.shadow", ownertrust.SEAL_NAME):
        (other / "secrets" / n).write_text((machine.secrets / n).read_text())
    drive_b = {"mountpoint": str(other), "uuid": UUID_B}
    assert ownertrust.inspect(drive_b, ownertrust.load_key())["owner.shadow"][0] == "unapproved"


def test_a_machine_without_the_key_trusts_nothing(machine):
    """Debian live USB: the key is new every boot, so last boot's seals no longer verify; the owner re-approves at the console."""
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    for p in machine.trust.iterdir():
        p.unlink()
    assert ownertrust.load_key() is None
    assert ownertrust.inspect(machine.drive, None)["owner.shadow"][0] == "unapproved"


def test_symlinked_credential_files_are_never_followed(machine):
    victim = machine.tmp / "etc-shadow"
    victim.write_text("root:$6$salt$hash:19000\n")
    os.symlink(victim, machine.secrets / "owner.shadow")
    got = ownertrust.inspect(machine.drive, ownertrust.load_key(create=True))
    assert got["owner.shadow"] == ("unapproved", None)
    assert victim.read_text() == "root:$6$salt$hash:19000\n"


def test_symlinked_secrets_directory_is_not_followed(machine):
    real = machine.tmp / "elsewhere"
    real.mkdir()
    (real / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    machine.secrets.rmdir()
    os.symlink(real, machine.secrets)
    got = ownertrust.inspect(machine.drive, ownertrust.load_key(create=True))
    assert all(v == ("unapproved", None) for v in got.values())


def test_absent_files_are_just_absent(machine):
    got = ownertrust.inspect(machine.drive, ownertrust.load_key(create=True))
    assert got["owner.shadow"] == ("absent", None) and got["ssh_authorized_keys"] == ("absent", None)


def test_no_drive_means_nothing_is_offered():
    assert all(v == ("absent", None) for v in ownertrust.inspect(None, None).values())


def test_remove_deletes_the_file_and_its_seal(machine):
    ownertrust.seal(machine.drive, "ssh_authorized_keys", KEY + "\n", UID, GID)
    ownertrust.remove(machine.drive, "ssh_authorized_keys")
    assert not (machine.secrets / "ssh_authorized_keys").exists()
    assert "ssh_authorized_keys" not in json.loads((machine.secrets / ownertrust.SEAL_NAME).read_text())["macs"]


def test_seal_refuses_a_read_only_drive_cleanly(machine, monkeypatch):
    def boom(*a, **k):
        raise OSError(30, "Read-only file system")
    ownertrust.load_key(create=True)                  # the key is root-side and fine; the DRIVE is the read-only part
    monkeypatch.setattr(ownertrust.safefs, "write_file", boom)
    with pytest.raises(ownertrust.TrustError):
        ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)


# ------------------------------------------------------------------ the machine key
def test_key_is_created_private_and_stable(machine):
    k1 = ownertrust.load_key(create=True)
    assert len(k1) == 32 and oct(os.stat(machine.trust / "drive.key").st_mode & 0o777) == "0o600"
    assert oct(os.stat(machine.trust).st_mode & 0o777) == "0o700"
    assert ownertrust.load_key() == k1


def test_key_with_loose_permissions_is_refused(machine):
    ownertrust.load_key(create=True)
    os.chmod(machine.trust / "drive.key", 0o644)
    with pytest.raises(ownertrust.TrustError):
        ownertrust.load_key()


def test_trust_dir_with_loose_permissions_is_refused(machine):
    ownertrust.load_key(create=True)
    os.chmod(machine.trust, 0o755)
    with pytest.raises(ownertrust.TrustError):
        ownertrust.load_key()


def test_key_symlink_is_refused(machine):
    machine.trust.mkdir()
    os.chmod(machine.trust, 0o700)
    target = machine.tmp / "k"
    target.write_text("a" * 64 + "\n")
    os.chmod(target, 0o600)
    os.symlink(target, machine.trust / "drive.key")
    with pytest.raises(OSError):
        ownertrust.load_key()


def test_key_is_never_stored_on_the_drive(machine):
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    key_hex = ownertrust.load_key().hex()
    for p in machine.data.rglob("*"):
        if p.is_file():
            assert key_hex not in p.read_text()


# ------------------------------------------------------------------ what counts as a drive
def test_a_plain_directory_at_the_mount_path_is_not_a_drive(tmp_path, monkeypatch):
    monkeypatch.undo()
    (tmp_path / "data").mkdir()
    assert ownertrust.current_drive(str(tmp_path / "data")) is None


def test_current_drive_reads_the_filesystem_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(ownertrust.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(ownertrust, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, UUID_A.upper() + "\n", ""))
    assert ownertrust.current_drive("/x") == {"mountpoint": "/x", "uuid": UUID_A}
    monkeypatch.setattr(ownertrust, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "not-a-uuid\n", ""))
    assert ownertrust.current_drive("/x") is None


def test_hash_validation_accepts_only_sha512_crypt():
    assert ownertrust.valid_hash(OWNER_HASH)
    for bad in ("", "!", "*", "$1$abc$def", "$6$salt$short", OWNER_HASH + ":x", OWNER_HASH + "\n", "$y$j9T$salt$" + "a" * 43):
        assert not ownertrust.valid_hash(bad)
    assert ownertrust.hash_text_of("not json") is None and ownertrust.hash_text_of('{"hash": "!"}') is None


def test_verify_password_checks_the_real_hash():
    assert ownertrust.verify_password(OWNER_HASH, "correct horse 42")
    assert not ownertrust.verify_password(OWNER_HASH, "wrong")
    assert not ownertrust.verify_password("!", "anything")


# ------------------------------------------------------------------ victor-priv (Debian/Pi helper)
@pytest.fixture
def priv(monkeypatch, machine):
    loader = importlib.machinery.SourceFileLoader("victor_priv_deb", os.path.join(DEB_LIB, "victor-priv"))
    spec = importlib.util.spec_from_loader("victor_priv_deb", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=UID, pw_gid=GID))
    monkeypatch.setattr(m, "LIVE_KEYS", str(machine.tmp / "home" / ".ssh" / "authorized_keys"))
    (machine.tmp / "home").mkdir()
    monkeypatch.setattr(m.victorssh, "keygen_ok", lambda line, keygen=None: "256 SHA256:test phone (ED25519)")
    calls = []
    monkeypatch.setattr(m, "run", lambda cmd, **kw: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""))
    m.calls = calls
    return m


def test_access_apply_ignores_an_unapproved_drive(priv, machine, capsys):
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    priv.cmd_access_apply()
    assert priv.calls == []                                           # no usermod -p, no sudo group
    assert not os.path.exists(priv.LIVE_KEYS)                         # no planted login key
    out = capsys.readouterr().out
    assert "NOT restored" in out and "owner.shadow" in out


def test_access_apply_restores_from_a_sealed_drive(priv, machine, capsys):
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    ownertrust.seal(machine.drive, "ssh_authorized_keys", KEY + "\n", UID, GID)
    priv.cmd_access_apply()
    assert ["usermod", "-p", OWNER_HASH, "victor"] in priv.calls and ["usermod", "-aG", "sudo", "victor"] in priv.calls
    assert open(priv.LIVE_KEYS).read() == KEY + "\n"


def test_access_apply_rejects_a_tampered_sealed_hash(priv, machine):
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))     # compromised owner session
    priv.cmd_access_apply()
    assert priv.calls == []


def test_access_apply_without_a_drive_does_nothing(priv, monkeypatch):
    monkeypatch.setattr(priv.ownertrust, "current_drive", lambda mountpoint=None: None)
    priv.cmd_access_apply()
    assert priv.calls == []


def test_mount_time_key_restore_also_needs_the_seal(priv, machine):
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    assert priv.restore_ssh_keys() == 0 and not os.path.exists(priv.LIVE_KEYS)
    ownertrust.seal(machine.drive, "ssh_authorized_keys", KEY + "\n", UID, GID)
    assert priv.restore_ssh_keys() == 1 and os.path.exists(priv.LIVE_KEYS)


def test_saved_ssh_keys_are_sealed_so_they_survive_a_reboot(priv, machine):
    assert priv.save_ssh_keys([KEY]) is True
    assert ownertrust.inspect(machine.drive, ownertrust.load_key())["ssh_authorized_keys"] == ("trusted", KEY + "\n")


def test_setting_the_password_seals_the_hash_on_the_drive(priv, machine, monkeypatch):
    priv.persist_owner_hash(OWNER_HASH)
    state, text = ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"]
    assert state == "trusted" and ownertrust.hash_text_of(text) == OWNER_HASH


def test_revoke_and_reset_remove_the_saved_credentials(priv, machine, monkeypatch):
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    ownertrust.seal(machine.drive, "ssh_authorized_keys", KEY + "\n", UID, GID)
    monkeypatch.setattr(priv.os.path, "exists", lambda p: False)
    priv.cmd_revoke()
    assert not (machine.secrets / "ssh_authorized_keys").exists()


def test_priv_no_longer_writes_owner_credentials_with_a_plain_json_write():
    src = open(os.path.join(DEB_LIB, "victor-priv")).read()
    assert "write_json_atomic(p, {\"hash\": h}" not in src
    assert "def saved_keys_path" not in src and "ROOT_LINK" not in src.split("def cmd_access_apply")[1].split("def cmd_smart")[0]


# ------------------------------------------------------------------ the console approval (victor-owner-setup)
@pytest.fixture
def setup_mod(monkeypatch, machine):
    loader = importlib.machinery.SourceFileLoader("victor_owner_setup", os.path.join(DEB_LIB, "victor-owner-setup"))
    spec = importlib.util.spec_from_loader("victor_owner_setup", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=UID, pw_gid=GID))
    monkeypatch.setattr(m, "switch_to_console", lambda: None)
    monkeypatch.setattr(m, "describe", lambda d: f"UUID {d['uuid']}")
    return m


class FakeConsole:
    def __init__(self, answers):
        self.answers, self.said, self.asked = list(answers), [], 0

    def say(self, t=""):
        self.said.append(t)

    def ask(self, prompt, secret=False):
        self.asked += 1
        return self.answers.pop(0) if self.answers else None


def _locked(monkeypatch):
    monkeypatch.setattr(ownertrust, "owner_hash", lambda: "!")


def test_console_approval_with_the_right_password_seals_and_applies(setup_mod, machine, monkeypatch):
    _locked(monkeypatch)
    (machine.secrets / "owner.shadow").write_text(shadow_text(OWNER_HASH))
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    applied = []
    con = FakeConsole(["correct horse 42"])
    rc = setup_mod.approve(con, run_priv=lambda: applied.append(1) or types.SimpleNamespace(stdout="restored"))
    assert rc == 0 and applied == [1]
    got = ownertrust.inspect(machine.drive, ownertrust.load_key())
    assert got["owner.shadow"][0] == "trusted" and got["ssh_authorized_keys"][0] == "trusted"


def test_console_approval_with_the_wrong_password_changes_nothing(setup_mod, machine, monkeypatch):
    _locked(monkeypatch)
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    applied = []
    con = FakeConsole(["guess1", "guess2", "guess3"])
    rc = setup_mod.approve(con, run_priv=lambda: applied.append(1))
    assert rc == 1 and applied == [] and con.asked == 3
    assert ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"][0] == "unapproved"


def test_console_skip_and_timeout_are_never_consent(setup_mod, machine, monkeypatch):
    _locked(monkeypatch)
    (machine.secrets / "owner.shadow").write_text(shadow_text(OWNER_HASH))
    for answers in ([""], []):                       # Enter, or no answer at all (timeout / end of input)
        applied = []
        assert setup_mod.approve(FakeConsole(answers), run_priv=lambda: applied.append(1)) == 1
        assert applied == [] and ownertrust.inspect(machine.drive, ownertrust.load_key())["owner.shadow"][0] == "unapproved"


def test_nothing_to_approve_means_no_prompt(setup_mod, machine, monkeypatch):
    _locked(monkeypatch)
    con = FakeConsole(["x"])
    assert setup_mod.approve(con) == 0 and con.asked == 0
    ownertrust.seal(machine.drive, "owner.shadow", shadow_text(OWNER_HASH), UID, GID)
    assert setup_mod.approve(con) == 0 and con.asked == 0       # already sealed


def test_unapproved_keys_alone_cannot_unlock_a_locked_account(setup_mod, machine, monkeypatch):
    """No password hash anywhere to prove against: the drive is simply ignored (and never prompts for a new password)."""
    _locked(monkeypatch)
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    con = FakeConsole(["whatever"])
    assert setup_mod.approve(con) == 1 and con.asked == 0
    assert ownertrust.inspect(machine.drive, ownertrust.load_key())["ssh_authorized_keys"][0] == "unapproved"


def test_current_password_approves_keys_but_not_a_different_saved_hash(setup_mod, machine, monkeypatch):
    monkeypatch.setattr(ownertrust, "owner_hash", lambda: OWNER_HASH)
    (machine.secrets / "owner.shadow").write_text(shadow_text(ATTACKER_HASH))
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    rc = setup_mod.approve(FakeConsole(["correct horse 42"]), run_priv=lambda: types.SimpleNamespace(stdout=""))
    got = ownertrust.inspect(machine.drive, ownertrust.load_key())
    assert rc == 0 and got["ssh_authorized_keys"][0] == "trusted" and got["owner.shadow"][0] == "unapproved"


def test_owner_setup_refuses_non_root_and_sudo(setup_mod, monkeypatch, capsys):
    monkeypatch.setattr(setup_mod.os, "geteuid", lambda: 1000)
    assert setup_mod.main(["x", "approve"]) == 2
    monkeypatch.setattr(setup_mod.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "victor")
    assert setup_mod.main(["x", "approve"]) == 2


def test_console_input_timeout_counts_as_skip(setup_mod, monkeypatch):
    class TTY(io.StringIO):
        def isatty(self):
            return True

        def fileno(self):
            return 0
    monkeypatch.setattr(setup_mod.select, "select", lambda *a: ([], [], []))
    out = io.StringIO()
    con = setup_mod.Console(timeout=1, stdin=TTY("secret\n"), stdout=out)
    assert con.ask("pw: ") is None and "timed out" in out.getvalue()


# ------------------------------------------------------------------ Pi helper
@pytest.fixture
def pipriv(monkeypatch, machine):
    loader = importlib.machinery.SourceFileLoader("victor_pi_priv", os.path.join(PI_DIR, "victor-pi-priv"))
    spec = importlib.util.spec_from_loader("victor_pi_priv", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_pi_restore_keys_only_from_a_sealed_copy(pipriv, machine, monkeypatch, capsys):
    installed = []
    monkeypatch.setattr(pipriv, "install_keys", lambda keys, persist=True: installed.append((keys, persist)))
    monkeypatch.setattr(pipriv, "valid_keys", lambda text: [l for l in text.splitlines() if l])
    monkeypatch.setattr(pipriv.os.path, "exists", lambda p: False)
    (machine.secrets / "ssh_authorized_keys").write_text(KEY + "\n")
    pipriv.cmd_restore_keys()
    assert installed == [] and "not approved" in capsys.readouterr().out
    ownertrust.seal(machine.drive, "ssh_authorized_keys", KEY + "\n", UID, GID)
    pipriv.cmd_restore_keys()
    assert installed == [([KEY], False)]


def test_pi_helper_no_longer_trusts_the_victor_controlled_root_link():
    src = open(os.path.join(PI_DIR, "victor-pi-priv")).read()
    assert "DATA_SECRETS" not in src and "ownertrust.seal(drive, ownertrust.OWNER_SHADOW" in src


# ------------------------------------------------------------------ packaging
def test_owner_setup_is_root_only_and_not_in_any_sudoers():
    for rel in ("debian-edition/config/includes.chroot_after_packages/etc/sudoers.d/victor-priv",
                "pi-edition/overlay/etc/sudoers.d/victor-pi-priv", "airootfs/etc/sudoers.d/10-victor"):
        assert "owner-setup" not in open(os.path.join(ROOT, rel)).read()
    hook = open(os.path.join(ROOT, "debian-edition/config/hooks/live/0200-victor-config.hook.chroot")).read()
    pihook = open(os.path.join(ROOT, "pi-edition/hooks/10-pi-config.sh")).read()
    assert "chmod 0700 /usr/local/lib/victorlinux/victor-owner-setup" in hook and "chmod 0700 /usr/local/lib/victorlinux/victor-owner-setup" in pihook
    assert "victor-owner-console.service" in hook and "victor-owner-console.service" in pihook


def test_console_service_runs_on_the_physical_tty_before_the_login():
    unit = open(os.path.join(DEB_LIB, "..", "..", "..", "..", "etc", "systemd", "system", "victor-owner-console.service")).read()
    assert "TTYPath=/dev/tty1" in unit and "StandardInput=tty" in unit
    assert "Before=getty@tty1.service display-manager.service" in unit and "After=victor-storage.service victor-access.service" in unit
