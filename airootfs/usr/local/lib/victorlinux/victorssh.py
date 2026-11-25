"""SSH authorized_keys helpers shared by both editions (stdlib only; ssh-keygen is used for the real validation).

An authorized_keys line may start with options (from="10.0.0.0/8",no-pty ...). Both editions must accept such lines,
because that is what people paste from their own files. The options are kept verbatim; only the key itself and the
line shape are checked here.
"""
import os
import shutil
import subprocess

KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
MAX_LINE = 4096
MAX_FILE = 65536


class KeyError_(ValueError):
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
        raise KeyError_("unbalanced quote in the key options")
    if cur:
        out.append("".join(cur))
    return out


def split_line(line):
    """Return (options, keytype, blob, comment). options is '' when the line has none."""
    if any(c in line for c in "\n\r\0"):
        raise KeyError_("one key per line, no control characters")
    line = line.strip()
    if not line or len(line) > MAX_LINE:
        raise KeyError_("empty or too long")
    t = tokens(line)
    for i in (0, 1):
        if len(t) > i + 1 and t[i] in KEY_TYPES:
            return (t[0] if i == 1 else ""), t[i], t[i + 1], " ".join(t[i + 2:])
    raise KeyError_("Unsupported key type. Paste a public key line such as: ssh-ed25519 AAAA... name")


def keygen_ok(line, keygen=None):
    exe = keygen or shutil.which("ssh-keygen") or "ssh-keygen"
    try:
        r = subprocess.run([exe, "-l", "-f", "/dev/stdin"], input=line + "\n", capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def validate_line(line):
    """Return (clean_line, fingerprint_text). Raises KeyError_ for anything that is not a real public key."""
    split_line(line)
    fp = keygen_ok(line.strip())
    if not fp:
        raise KeyError_("That is not a valid SSH public key.")
    return line.strip(), fp


def valid_keys(text):
    """All real key lines in a block of text (comments and blanks skipped). Raises KeyError_ on the first bad line."""
    if len(text) > MAX_FILE:
        raise KeyError_("too much input")
    keys = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        keys.append(validate_line(s)[0])
    return keys


def count_keys(path):
    try:
        with open(path) as f:
            return len([l for l in f if l.strip() and not l.lstrip().startswith("#")])
    except OSError:
        return 0


def write_keys(path, keys, uid=None, gid=None):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keys) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if uid is not None:
        os.chown(tmp, uid, gid)
    os.replace(tmp, path)


def persist_keys(src, dst, uid=None, gid=None):
    """Copy the live authorized_keys to its persistent copy (0600). Returns True if written."""
    try:
        keys = valid_keys(open(src).read())
    except (OSError, KeyError_):
        return False
    if not keys:
        return False
    write_keys(dst, keys, uid, gid)
    return True


def restore_keys(saved, live, uid, gid):
    """If the live file is missing/empty and a saved copy exists, re-create it. Returns the number of keys restored."""
    if count_keys(live) or not os.path.isfile(saved):
        return 0
    try:
        keys = valid_keys(open(saved).read())
    except (OSError, KeyError_):
        return 0
    if not keys:
        return 0
    d = os.path.dirname(live)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chown(d, uid, gid)
    os.chmod(d, 0o700)
    write_keys(live, keys, uid, gid)
    return len(keys)
