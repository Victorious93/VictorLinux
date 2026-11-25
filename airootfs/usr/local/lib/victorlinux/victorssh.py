"""SSH authorized_keys helpers shared by both editions (stdlib only; ssh-keygen is used for the real validation).

An authorized_keys line may start with options (from="10.0.0.0/8",no-pty ...). Both editions must accept such lines,
because that is what people paste from their own files. The options are kept verbatim; only the key itself and the
line shape are checked here.
"""
import os
import shutil
import subprocess

try:
    import safefs
except ImportError:           # Debian/Pi keep it in the victorlib package
    from victorlib import safefs

KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
MAX_LINE = 4096
MAX_FILE = 65536


class SSHKeyError(ValueError):
    pass


def tokens(line):
    """Split on whitespace outside double quotes (the authorized_keys option syntax)."""
    out, cur, quoted, esc = [], [], False, False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\" and quoted:
            cur.append(ch)
            esc = True
        elif ch == '"':
            cur.append(ch)
            quoted = not quoted
        elif ch in " \t" and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if quoted:
        raise SSHKeyError("unbalanced quote in the key options")
    if cur:
        out.append("".join(cur))
    return out


def split_line(line):
    """Return (options, keytype, blob, comment). options is '' when the line has none."""
    if any(c in line for c in "\n\r\0"):
        raise SSHKeyError("one key per line, no control characters")
    line = line.strip()
    if not line or len(line) > MAX_LINE:
        raise SSHKeyError("empty or too long")
    t = tokens(line)
    for i in (0, 1):
        if len(t) > i + 1 and t[i] in KEY_TYPES:
            return (t[0] if i == 1 else ""), t[i], t[i + 1], " ".join(t[i + 2:])
    raise SSHKeyError("Unsupported key type. Paste a public key line such as: ssh-ed25519 AAAA... name")


def keygen_ok(line, keygen=None):
    exe = keygen or shutil.which("ssh-keygen") or "ssh-keygen"
    try:
        r = subprocess.run([exe, "-l", "-f", "/dev/stdin"], input=line + "\n", capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def validate_line(line):
    """Return (clean_line, fingerprint_text). Raises SSHKeyError for anything that is not a real public key."""
    split_line(line)
    fp = keygen_ok(line.strip())
    if not fp:
        raise SSHKeyError("That is not a valid SSH public key.")
    return line.strip(), fp


def valid_keys(text):
    """All real key lines in a block of text (comments and blanks skipped). Raises SSHKeyError on the first bad line."""
    if len(text) > MAX_FILE:
        raise SSHKeyError("too much input")
    keys = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        keys.append(validate_line(s)[0])
    return keys


def read_text(path):
    """Contents of a keys file, or '' when it is missing, unreadable or reached through a symlink.
    Root helpers call this on files inside folders an unprivileged user (or an untrusted drive) controls."""
    try:
        dfd = safefs.open_dir(os.path.dirname(path))
    except OSError:
        return ""
    try:
        return safefs.read_file(dfd, os.path.basename(path), MAX_FILE + 1) or ""
    except OSError:
        return ""
    finally:
        os.close(dfd)


def count_keys(path):
    return len([l for l in read_text(path).splitlines() if l.strip() and not l.lstrip().startswith("#")])


def write_keys(path, keys, uid=None, gid=None, dir_mode=None):
    """Atomically replace `path`. Never follows a symlink in the folder or at the file, never reuses a planted temp file."""
    dfd = safefs.open_dir(os.path.dirname(path), create=True)
    try:
        safefs.chown_dir(dfd, uid, gid, dir_mode)
        safefs.write_file(dfd, os.path.basename(path), "\n".join(keys) + "\n", uid, gid, 0o600)
    finally:
        os.close(dfd)


def persist_keys(src, dst, uid=None, gid=None):
    """Copy the live authorized_keys to its persistent copy (0600). Returns True if written."""
    try:
        keys = valid_keys(read_text(src))
    except (OSError, SSHKeyError):
        return False
    if not keys:
        return False
    write_keys(dst, keys, uid, gid)
    return True


def restore_keys(saved, live, uid, gid):
    """If the live file is missing/empty and a saved copy exists, re-create it. Returns the number of keys restored."""
    if count_keys(live) or not read_text(saved):
        return 0
    try:
        keys = valid_keys(read_text(saved))
    except (OSError, SSHKeyError):
        return 0
    if not keys:
        return 0
    write_keys(live, keys, uid, gid, dir_mode=0o700)
    return len(keys)
