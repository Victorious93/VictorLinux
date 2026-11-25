"""Who may restore the owner's credentials from a data drive (security audit F-10).

The owner's password hash (``secrets/owner.shadow``) and saved SSH keys (``secrets/ssh_authorized_keys``) live on the
VICTORDATA/TORIDATA drive so they survive a reboot. A drive is untrusted storage: anyone can label a partition VICTORDATA,
and the owner's own session (uid victor) owns the ``secrets`` directory on a mounted drive. Restoring from it blindly
would hand out sudo.

So a restore is allowed only when the file carries a seal made by THIS machine:

    seal = HMAC-SHA256(machine key, drive filesystem UUID | file name | file bytes)

The machine key lives in ``/var/lib/victor-trust`` (root only, mode 0700/0600), never on the drive. A forged drive, an
edited file, a file copied to another drive, or a drive from another machine has no valid seal and is left alone: it
still mounts as plain storage, but never changes accounts, sudo or SSH keys. The owner approves such a drive at the
physical console by typing the password the drive claims to hold (``victor-owner-setup``); only then is it sealed.

On the Debian live USB nothing persists outside the data drive, so the machine key is new each boot and the owner
approves the drive at the console once per boot. On the Raspberry Pi the key persists on the SD card.
"""
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat

from . import safefs
from .common import VictorError, run
from . import common

TRUST_DIR = os.environ.get("VICTOR_TRUST_DIR", "/var/lib/victor-trust")
SHADOW = os.environ.get("VICTOR_SHADOW", "/etc/shadow")
OWNER = "victor"
KEY_NAME = "drive.key"
SEAL_NAME = ".victor-seal.json"
OWNER_SHADOW = "owner.shadow"
SSH_KEYS = "ssh_authorized_keys"
SEALED_NAMES = (OWNER_SHADOW, SSH_KEYS)
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
HASH6_RE = re.compile(r"^\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$")
MAX_FILE = 65536


class TrustError(VictorError):
    pass


# ---- machine key (root only, never on the drive) ---------------------------------------------------------------
def _trust_dir_fd(create):
    fd = safefs.open_path(TRUST_DIR, create=create, mode=0o700)
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        os.close(fd)
        raise TrustError(f"{TRUST_DIR} must be private to the helper (owner {os.geteuid()}, mode 0700)")
    return fd


def load_key(create=False):
    """The machine key as bytes, or None when this machine has none yet (and create is False)."""
    try:
        dfd = _trust_dir_fd(create)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(KEY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        except FileNotFoundError:
            if not create:
                return None
            safefs.write_file(dfd, KEY_NAME, secrets.token_hex(32) + "\n", os.geteuid(), os.getegid(), 0o600)
            fd = os.open(KEY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise TrustError(f"{TRUST_DIR}/{KEY_NAME} has unsafe ownership or permissions")
            raw = os.read(fd, 256).decode("ascii", "replace").strip()
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    if not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise TrustError(f"{TRUST_DIR}/{KEY_NAME} is not a valid key")
    return bytes.fromhex(raw)


def mac(key, uuid, name, data):
    if isinstance(data, str):
        data = data.encode()
    msg = b"victorlinux-drive-seal-v1\0" + uuid.lower().encode() + b"\0" + name.encode() + b"\0" + data
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# ---- the data drive ----------------------------------------------------------------------------------------------
def current_drive(mountpoint=None):
    """{'mountpoint','uuid'} for the data partition mounted at the data mount point, else None. A plain directory that
    merely sits at that path (the owner can create one) is not a drive."""
    mp = mountpoint or common.DATA_MP
    if not os.path.ismount(mp):
        return None
    r = run(["findmnt", "-n", "-o", "UUID", "--mountpoint", mp])
    uuid = r.stdout.strip().splitlines()[0].strip() if r.returncode == 0 and r.stdout.strip() else ""
    if not UUID_RE.match(uuid):
        return None
    return {"mountpoint": mp, "uuid": uuid.lower()}


def _secrets_fd(drive, create=False):
    mp_fd = safefs.open_path(drive["mountpoint"])
    try:
        if create:
            try:
                os.mkdir("secrets", 0o700, dir_fd=mp_fd)
            except FileExistsError:
                pass
        try:
            return os.open("secrets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=mp_fd)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return None
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise safefs.UnsafePath(e.errno, "secrets on the data drive is a symbolic link or not a directory") from e
            raise
    finally:
        os.close(mp_fd)


def _seal_doc(dfd, uuid):
    try:
        doc = json.loads(safefs.read_file(dfd, SEAL_NAME, MAX_FILE) or "{}")
    except (ValueError, safefs.UnsafePath):
        return {}
    if not isinstance(doc, dict) or str(doc.get("uuid", "")).lower() != uuid.lower() or not isinstance(doc.get("macs"), dict):
        return {}
    return {k: v for k, v in doc["macs"].items() if isinstance(k, str) and isinstance(v, str)}


def inspect(drive, key=None):
    """What the drive offers and what this machine trusts: {name: (state, text|None)} for each credential file.
    state is 'absent', 'trusted' (valid seal) or 'unapproved' (present, no valid seal). Text is returned only when
    trusted; the bytes that were checked are the bytes that are returned (no re-read, no race with the owner)."""
    out = {n: ("absent", None) for n in SEALED_NAMES}
    if not drive:
        return out
    try:
        dfd = _secrets_fd(drive)
    except OSError:
        return {n: ("unapproved", None) for n in SEALED_NAMES}
    if dfd is None:
        return out
    try:
        macs = _seal_doc(dfd, drive["uuid"])
        for name in SEALED_NAMES:
            try:
                text = safefs.read_file(dfd, name, MAX_FILE)
            except OSError:
                out[name] = ("unapproved", None)
                continue
            if text is None:
                continue
            ok = key is not None and name in macs and hmac.compare_digest(macs[name], mac(key, drive["uuid"], name, text))
            out[name] = ("trusted", text) if ok else ("unapproved", None)
    finally:
        os.close(dfd)
    return out


def peek(drive, name):
    """Raw text of a credential file on the drive for DISPLAY/VERIFICATION only (never installed from here)."""
    dfd = _secrets_fd(drive)
    if dfd is None:
        return None
    try:
        return safefs.read_file(dfd, name, MAX_FILE)
    except OSError:
        return None
    finally:
        os.close(dfd)


def seal(drive, name, text, uid, gid, key=None):
    """Write `name` on the drive and record this machine's seal for it. Raises TrustError when it cannot be done
    (no drive, read-only drive); callers treat that as 'not persisted', never as a reason to fail the user's action."""
    if name not in SEALED_NAMES:
        raise TrustError("not a credential file")
    if not drive:
        raise TrustError("no data drive is mounted")
    key = key or load_key(create=True)
    try:
        dfd = _secrets_fd(drive, create=True)
        if dfd is None:
            raise TrustError("cannot create secrets on the data drive")
        try:
            macs = _seal_doc(dfd, drive["uuid"])
            safefs.write_file(dfd, name, text, uid, gid, 0o600)
            macs[name] = mac(key, drive["uuid"], name, text)
            safefs.write_file(dfd, SEAL_NAME, json.dumps({"v": 1, "uuid": drive["uuid"], "macs": macs}, sort_keys=True) + "\n",
                              uid, gid, 0o600)
        finally:
            os.close(dfd)
    except OSError as e:
        raise TrustError(f"could not save {name} on the data drive: {e}") from e


# ---- the owner account -------------------------------------------------------------------------------------------
def owner_hash():
    try:
        for line in open(SHADOW):
            f = line.split(":")
            if f[0] == OWNER:
                return f[1]
    except OSError:
        pass
    return "!"


def owner_locked():
    h = owner_hash()
    return h == "" or h.startswith(("!", "*"))


def valid_hash(h):
    return bool(h) and bool(HASH6_RE.fullmatch(h))


def hash_text_of(shadow_text):
    """The hash inside an owner.shadow file ({"hash": "$6$..."}), or None when malformed."""
    try:
        h = (json.loads(shadow_text) or {}).get("hash")
    except (ValueError, AttributeError):
        return None
    return h if isinstance(h, str) and valid_hash(h) else None


def verify_password(h, password):
    """True if `password` hashes to the SHA-512 crypt hash `h` (uses openssl; python3.13 has no crypt)."""
    if not valid_hash(h):
        return False
    salt = h.split("$")[2]
    chk = run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=password + "\n").stdout.strip()
    return hmac.compare_digest(chk.encode(), h.encode())


def remove(drive, name):
    """Delete a credential file (and its seal) from the drive. Absent drive/file is not an error."""
    if not drive or name not in SEALED_NAMES:
        return
    try:
        dfd = _secrets_fd(drive)
    except OSError:
        return
    if dfd is None:
        return
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass
        except OSError:
            return
        macs = _seal_doc(dfd, drive["uuid"])
        if name in macs:
            del macs[name]
            try:
                safefs.write_file(dfd, SEAL_NAME, json.dumps({"v": 1, "uuid": drive["uuid"], "macs": macs}, sort_keys=True) + "\n",
                                  None, None, 0o600)
            except OSError:
                pass
    finally:
        os.close(dfd)
