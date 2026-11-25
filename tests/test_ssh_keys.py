"""SSH bug fixes: option-prefixed authorized_keys lines (Arch), keys persisted on storage (both), Tk SSH controls (Debian)."""
import os
import subprocess
from pathlib import Path

import pytest

import victorssh

ROOT = Path(__file__).resolve().parents[1]
DEB = ROOT / "debian-edition/config/includes.chroot_after_packages"


@pytest.fixture(scope="module")
def pubkey(tmp_path_factory):
    d = tmp_path_factory.mktemp("k")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "owner@test", "-f", str(d / "id")], check=True)
    return (d / "id.pub").read_text().strip()


def test_tokens_respect_quotes():
    assert victorssh.tokens('command="echo a b",no-pty ssh-ed25519 AAAA c') == ['command="echo a b",no-pty', "ssh-ed25519", "AAAA", "c"]
    with pytest.raises(victorssh.SSHKeyError):
        victorssh.tokens('from="10.0.0.1 ssh-ed25519 AAAA')


def test_split_plain_and_option_lines(pubkey):
    t, b = pubkey.split()[:2]
    assert victorssh.split_line(pubkey)[:3] == ("", t, b)
    opts = 'from="10.0.0.0/8",no-port-forwarding'
    assert victorssh.split_line(f"{opts} {pubkey}")[:3] == (opts, t, b)
    for bad in ("", "garbage AAAA", "ssh-dss AAAA", "a\nb", "x" * 5000):
        with pytest.raises(victorssh.SSHKeyError):
            victorssh.split_line(bad)


def test_validate_uses_ssh_keygen(pubkey):
    line, fp = victorssh.validate_line(f'from="192.168.1.0/24",no-pty {pubkey}')
    assert "SHA256:" in fp and line.startswith("from=")
    with pytest.raises(victorssh.SSHKeyError):
        victorssh.validate_line("ssh-ed25519 AAAAnotakey owner")


def test_persist_and_restore_round_trip(tmp_path, pubkey):
    live, saved = tmp_path / "home/.ssh/authorized_keys", tmp_path / "data/secrets/ssh_authorized_keys"
    live.parent.mkdir(parents=True)
    live.write_text("# comment\n" + pubkey + "\n")
    assert victorssh.persist_keys(str(live), str(saved))
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    live.unlink()                                   # reboot: the live (RAM) home is empty again
    n = victorssh.restore_keys(str(saved), str(live), os.getuid(), os.getgid())
    assert n == 1 and live.read_text().strip() == pubkey
    assert oct(live.stat().st_mode & 0o777) == "0o600"
    assert victorssh.restore_keys(str(saved), str(live), os.getuid(), os.getgid()) == 0   # never overwrites live keys


def test_copies_match_between_editions():
    a = ROOT / "airootfs/usr/local/lib/victorlinux/victorssh.py"
    assert a.read_text() == (DEB / "usr/local/lib/victorlinux/victorssh.py").read_text()


def test_arch_helper_accepts_options_and_persists():
    s = (ROOT / "airootfs/usr/local/lib/victorlinux/victor-priv").read_text()
    assert "key options present" not in s and "victorssh.validate_line" in s
    assert "restore_ssh_keys()" in s and "save_ssh_keys()" in s
    assert '"security", "ssh_authorized_keys"' in s


def test_debian_helper_persists_restores_and_has_verbs():
    s = (DEB / "usr/local/lib/victorlinux/victor-priv").read_text()
    assert "ownertrust.SSH_KEYS" in s                        # same file the Pi edition uses (sealed by ownertrust, F-10)
    assert (DEB / "usr/local/lib/victorlinux/victorlib/ownertrust.py").read_text().count('SSH_KEYS = "ssh_authorized_keys"') == 1
    assert s.count("restore_ssh_keys(") >= 3                # definition + access-apply at boot + after a storage mount
    for verb in ('"ssh-status"', '"ssh-stop"', '"ssh-enable"', '"revoke"'):
        assert verb in s
    assert "AuthenticationMethods publickey" in s


def test_debian_tk_and_ctl_have_ssh_controls():
    tk = (DEB / "usr/local/bin/victor-control").read_text()
    for t in ('text="SSH"', "Add keys and enable", "Enable with saved keys", "Stop SSH", "Remove all keys and disable"):
        assert t in tk
    ctl = (DEB / "usr/local/bin/victor-ctl").read_text()
    assert 'g("ssh", "status", "enable", "stop", "revoke")' in ctl and "--keys-stdin" in ctl
    assert "def tab_ssh" in tk and "self.tab_ssh()" in tk
