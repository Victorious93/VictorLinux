"""Symlink-safe file operations for root helpers that touch directories an unprivileged user controls.

victor-priv and its siblings run as root but are asked to write inside /home/victor/.ssh and onto a data drive whose
contents are not trusted. Plain open()/chown()/chmod() follow symlinks, so a planted link would turn a root write into an
overwrite or chown of an arbitrary system file. Everything here works relative to a directory file descriptor opened with
O_NOFOLLOW, creates files with O_EXCL|O_NOFOLLOW, and replaces files with rename(), never by writing through a path.
"""
import errno
import os
import stat


class UnsafePath(OSError):
    """A symlink or a wrongly owned/typed object was found where a plain directory or file was required."""


def open_dir(path, uid=None, create=False, mode=0o700):
    """Open `path` as a directory without following a symlink at the last component. Returns a directory fd.
    When `uid` is given the directory must belong to that uid (or be created here)."""
    if create:
        os.makedirs(path, mode, exist_ok=True)       # parents are trusted; the final component is re-opened with O_NOFOLLOW
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(e.errno, f"{path} is a symbolic link or not a directory") from e
        raise
    if uid is not None and os.fstat(fd).st_uid not in (uid, 0 if create else uid):
        os.close(fd)
        raise UnsafePath(errno.EPERM, f"{path} is not owned by the expected account")
    return fd


def read_file(dir_fd, name, limit=1 << 20):
    """Contents of `name` inside dir_fd, or None when absent. Refuses symlinks and non-regular files."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise UnsafePath(e.errno, f"{name} is a symbolic link") from e
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePath(errno.EINVAL, f"{name} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(limit).decode("utf-8", "replace")
    finally:
        os.close(fd)


def write_file(dir_fd, name, data, uid=None, gid=None, mode=0o600):
    """Atomically replace `name` in dir_fd with `data`, owned by uid:gid. A symlink already at `name` is replaced,
    never followed; a stale temp file is removed first."""
    if "/" in name or name in ("", ".", ".."):
        raise UnsafePath(errno.EINVAL, "bad file name")
    tmp = f".{name}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(fd)
        if uid is not None:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.close(fd)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def chown_dir(dir_fd, uid, gid, mode=None):
    if uid is not None:
        os.fchown(dir_fd, uid, gid)
    if mode is not None:
        os.fchmod(dir_fd, mode)
