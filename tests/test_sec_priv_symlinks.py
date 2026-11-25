"""Root helpers must never follow a symlink planted by the unprivileged user or by an untrusted data drive."""
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
ARCH_LIB = os.path.join(ROOT, "airootfs", "usr", "local", "lib", "victorlinux")
DEB_LIB = os.path.join(ROOT, "debian-edition", "config", "includes.chroot_after_packages", "usr", "local", "lib", "victorlinux")
PI_PRIV = os.path.join(ROOT, "pi-edition", "overlay", "usr", "local", "lib", "victorlinux", "pi", "victor-pi-priv")

sys.path.insert(0, ARCH_LIB)
import safefs  # noqa: E402

UID, GID = os.getuid(), os.getgid()
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAM phone"


def test_debian_copy_of_safefs_is_identical():
    assert open(os.path.join(ARCH_LIB, "safefs.py")).read() == open(os.path.join(DEB_LIB, "victorlib", "safefs.py")).read()


def test_write_file_replaces_a_symlink_instead_of_following_it(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    d = tmp_path / "ssh"
    d.mkdir()
    os.symlink(victim, d / "authorized_keys")
    fd = safefs.open_dir(str(d), uid=UID)
    safefs.write_file(fd, "authorized_keys", "new\n", UID, GID)
    os.close(fd)
    assert victim.read_text() == "precious"
    assert not os.path.islink(d / "authorized_keys") and (d / "authorized_keys").read_text() == "new\n"
    assert oct(os.stat(d / "authorized_keys").st_mode & 0o777) == "0o600"


def test_write_file_does_not_follow_a_planted_temp_file_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    d = tmp_path / "ssh"
    d.mkdir()
    os.symlink(victim, d / f".authorized_keys.{os.getpid()}.tmp")
    fd = safefs.open_dir(str(d), uid=UID)
    safefs.write_file(fd, "authorized_keys", "new\n", UID, GID)
    os.close(fd)
    assert victim.read_text() == "precious" and (d / "authorized_keys").read_text() == "new\n"


def test_open_dir_and_read_file_refuse_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "link")
    with pytest.raises(safefs.UnsafePath):
        safefs.open_dir(str(tmp_path / "link"))
    (real / "f").write_text("x")
    os.symlink(real / "f", real / "g")
    fd = safefs.open_dir(str(real))
    assert safefs.read_file(fd, "f") == "x" and safefs.read_file(fd, "missing") is None
    with pytest.raises(safefs.UnsafePath):
        safefs.read_file(fd, "g")
    os.close(fd)


def test_write_file_rejects_path_components(tmp_path):
    fd = safefs.open_dir(str(tmp_path))
    for bad in ("../x", "a/b", "", ".", ".."):
        with pytest.raises(safefs.UnsafePath):
            safefs.write_file(fd, bad, "x", UID, GID)
    os.close(fd)


@pytest.fixture
def priv(monkeypatch, tmp_path):
    loader = importlib.machinery.SourceFileLoader("victor_priv_sec", os.path.join(ARCH_LIB, "victor-priv"))
    spec = importlib.util.spec_from_loader("victor_priv_sec", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=UID, pw_gid=GID))
    monkeypatch.setattr(m, "RUN", str(tmp_path / "run"))
    monkeypatch.setattr(m.victorssh, "keygen_ok", lambda line, keygen=None: "256 SHA256:test phone (ED25519)")
    monkeypatch.setattr(m, "persistent_keys_path", lambda: None)
    monkeypatch.setattr(m, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "256 SHA256:test phone (ED25519)\n", ""))
    return m


def _add_key(priv, monkeypatch, keys_path):
    monkeypatch.setattr(priv, "KEYS", str(keys_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    priv.cmd_ssh_add_key([])


def test_ssh_add_key_appends_and_sets_modes(priv, monkeypatch, tmp_path):
    keys = tmp_path / "home" / ".ssh" / "authorized_keys"
    (tmp_path / "home").mkdir()
    _add_key(priv, monkeypatch, keys)
    _add_key(priv, monkeypatch, keys)
    assert keys.read_text() == (KEY + "\n") * 2
    assert oct(os.stat(keys).st_mode & 0o777) == "0o600" and oct(os.stat(keys.parent).st_mode & 0o777) == "0o700"


def test_ssh_add_key_never_writes_through_a_symlinked_authorized_keys(priv, monkeypatch, tmp_path):
    victim = tmp_path / "etc-passwd"
    victim.write_text("root:x:0:0\n")
    os.chmod(victim, 0o644)
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    os.symlink(victim, ssh / "authorized_keys")
    _add_key(priv, monkeypatch, ssh / "authorized_keys")
    assert victim.read_text() == "root:x:0:0\n" and oct(os.stat(victim).st_mode & 0o777) == "0o644"
    assert not os.path.islink(ssh / "authorized_keys") and (ssh / "authorized_keys").read_text() == KEY + "\n"


def test_ssh_add_key_refuses_a_symlinked_ssh_directory(priv, monkeypatch, tmp_path):
    target = tmp_path / "etc"
    target.mkdir()
    os.chmod(target, 0o755)
    home = tmp_path / "home"
    home.mkdir()
    os.symlink(target, home / ".ssh")
    with pytest.raises(SystemExit):
        _add_key(priv, monkeypatch, home / ".ssh" / "authorized_keys")
    assert os.listdir(target) == [] and oct(os.stat(target).st_mode & 0o777) == "0o755"


def test_ssh_status_does_not_read_through_a_symlink(priv, monkeypatch, tmp_path, capsys):
    secret = tmp_path / "shadow"
    secret.write_text("a\nb\nc\n")
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    os.symlink(secret, ssh / "authorized_keys")
    monkeypatch.setattr(priv, "KEYS", str(ssh / "authorized_keys"))
    monkeypatch.setattr(priv, "SSHD_DROPIN", str(tmp_path / "none.conf"))
    priv.cmd_ssh_status([])
    assert '"authorized_keys": 0' in capsys.readouterr().out


def test_ensure_tree_refuses_symlinked_subfolders_and_root(priv, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    root = tmp_path / "drive" / "victorlinux"
    root.mkdir(parents=True)
    os.symlink(outside, root / "models")
    with pytest.raises(ValueError):
        priv.ensure_tree(str(root))
    assert oct(os.stat(outside).st_mode & 0o777) == "0o755" and os.listdir(outside) == []
    os.symlink(outside, tmp_path / "drive" / "linkroot")
    with pytest.raises(ValueError):
        priv.ensure_tree(str(tmp_path / "drive" / "linkroot"))


def test_ensure_tree_creates_the_folders_on_a_clean_drive(priv, tmp_path):
    root = tmp_path / "victorlinux"
    root.mkdir()
    priv.ensure_tree(str(root))
    assert sorted(os.listdir(root)) == sorted(priv.SUBDIRS)
    assert oct(os.stat(root / "config").st_mode & 0o777) == "0o700"


def test_victorssh_write_keys_never_follows_links(tmp_path):
    import victorssh
    victim = tmp_path / "victim"
    victim.write_text("precious")
    d = tmp_path / "ssh"
    d.mkdir()
    os.symlink(victim, d / "authorized_keys")
    os.symlink(victim, d / f".authorized_keys.{os.getpid()}.tmp")
    victorssh.write_keys(str(d / "authorized_keys"), [KEY], UID, GID, dir_mode=0o700)
    assert victim.read_text() == "precious" and (d / "authorized_keys").read_text() == KEY + "\n"
    # a symlinked folder is refused outright
    os.symlink(d, tmp_path / "linked")
    with pytest.raises(OSError):
        victorssh.write_keys(str(tmp_path / "linked" / "authorized_keys"), [KEY], UID, GID)


def test_victorssh_readers_do_not_read_through_links(tmp_path):
    import victorssh
    secret = tmp_path / "shadow"
    secret.write_text("a\nb\nc\n")
    d = tmp_path / "ssh"
    d.mkdir()
    os.symlink(secret, d / "authorized_keys")
    assert victorssh.read_text(str(d / "authorized_keys")) == "" and victorssh.count_keys(str(d / "authorized_keys")) == 0


def test_debian_victorssh_matches_arch_copy():
    assert open(os.path.join(ARCH_LIB, "victorssh.py")).read() == open(os.path.join(DEB_LIB, "victorssh.py")).read()


def test_debian_and_pi_helpers_write_keys_only_through_safe_writers():
    deb = open(os.path.join(DEB_LIB, "victor-priv")).read()
    assert "victorssh.write_keys(LIVE_KEYS, lines, uid, gid, dir_mode=0o700)" in deb and "open(d + \"/authorized_keys\"" not in deb
    pi = open(PI_PRIV).read()
    assert "safefs.write_file(dfd, \"authorized_keys\"" in pi and "chown\", \"-R\"" not in pi
