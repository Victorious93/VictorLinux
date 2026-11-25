"""The mesh root helper writes inside a folder the unprivileged user owns: it must never follow a planted symlink."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "airootfs/usr/local/lib/victorlinux"))

import safefs  # noqa: E402
import victormesh as vm  # noqa: E402
from test_victor_mesh import load_priv  # noqa: E402


def test_open_path_refuses_symlink_in_the_middle(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    fd = safefs.open_path(str(tmp_path / "real"))
    os.close(fd)
    with pytest.raises(safefs.UnsafePath):
        safefs.open_path(str(tmp_path / "link" / "x"), create=True)
    assert not (tmp_path / "real" / "x").exists()


def test_open_path_creates_missing_components_with_mode(tmp_path):
    fd = safefs.open_path(str(tmp_path / "a" / "b"), create=True)
    os.close(fd)
    assert (tmp_path / "a" / "b").is_dir() and oct((tmp_path / "a" / "b").stat().st_mode & 0o777) == "0o700"
    with pytest.raises(safefs.UnsafePath):
        safefs.open_path("relative/path")


def test_mesh_write_does_not_follow_a_planted_file_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    d = tmp_path / "state"
    d.mkdir()
    (d / "settings.json").symlink_to(victim)
    vm._write(str(d / "settings.json"), '{"x": 1}')
    assert victim.read_text() == "keep"
    assert (d / "settings.json").read_text() == '{"x": 1}' and not (d / "settings.json").is_symlink()


def test_mesh_write_refuses_a_symlinked_state_folder(tmp_path):
    target = tmp_path / "etc"
    target.mkdir()
    (tmp_path / "state").symlink_to(target)
    with pytest.raises(OSError):
        vm._write(str(tmp_path / "state" / "settings.json"), "{}")
    assert list(target.iterdir()) == []


def test_ensure_dirs_refuses_symlinked_folders_and_does_not_chmod_the_target(tmp_path, monkeypatch):
    mod = load_priv()
    monkeypatch.setattr(mod, "owner_ids", lambda: (os.getuid(), os.getgid(), str(tmp_path)))
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    state = tmp_path / "state"
    state.symlink_to(target)
    p = {"dir": str(state), "share_dir": str(state / "share"), "received": str(state / "received")}
    with pytest.raises(SystemExit):
        mod.ensure_dirs(p)
    assert oct(target.stat().st_mode & 0o777) == "0o755" and list(target.iterdir()) == []


def test_ensure_dirs_refuses_a_symlinked_share_folder(tmp_path, monkeypatch):
    mod = load_priv()
    monkeypatch.setattr(mod, "owner_ids", lambda: (os.getuid(), os.getgid(), str(tmp_path)))
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "share").symlink_to(target)
    p = {"dir": str(state), "share_dir": str(state / "share"), "received": str(state / "received")}
    with pytest.raises(SystemExit):
        mod.ensure_dirs(p)
    assert oct(target.stat().st_mode & 0o777) == "0o755"


def test_ensure_dirs_happy_path(tmp_path, monkeypatch):
    mod = load_priv()
    monkeypatch.setattr(mod, "owner_ids", lambda: (os.getuid(), os.getgid(), str(tmp_path)))
    p = {"dir": str(tmp_path / "s"), "share_dir": str(tmp_path / "s" / "share"), "received": str(tmp_path / "s" / "received")}
    mod.ensure_dirs(p)
    for d in (p["dir"], p["share_dir"], p["received"]):
        assert os.path.isdir(d) and oct(os.stat(d).st_mode & 0o777) == "0o700"


def test_mesh_files_are_identical_across_editions():
    deb = ROOT / "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux"
    arch = ROOT / "airootfs/usr/local/lib/victorlinux"
    for name, other in (("victormesh.py", "victormesh.py"), ("victor-mesh-priv", "victor-mesh-priv"),
                        ("safefs.py", "victorlib/safefs.py")):
        assert (arch / name).read_bytes() == (deb / other).read_bytes(), name
