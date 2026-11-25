"""Victorious Mesh support for VictorLinux (Arch, Debian and Pi editions). Stdlib only.

Joins this PC to the owner's own Headscale server with the standard tailscale/tailscaled, so the Victorious Mesh Android
app (a tailscale-android fork) can reach it. Everything is OFF by default and nothing is hardcoded: the owner types the
Headscale URL and a pre-auth key. The key is handed to `tailscale up --auth-key=file:...` once through a 0600 file in
/run that is deleted right after; it is never written to settings, logs or the command line.

Extras (each off by default): exit node, Wake-on-LAN, Taildrop receive, a read-only file share compatible with the app
(HTTP on the mesh address only, port 8090, Basic auth with any user name plus a generated password, files at /f/<name>,
2 GB cap, no upload) and SSH reachable over the mesh only (key auth, firewalled to tailscale0).
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

SHARE_PORT = 8090                       # same default as the app (MeshShareModels.DEFAULT_SHARE_PORT)
MAX_SHARE_BYTES = 2 * 1024 * 1024 * 1024
MIN_SHARE_PASSWORD = 16
TAG_PC, TAG_PI = "tag:victorlinux", "tag:victorlinux-pi"
OWNER = os.environ.get("VICTOR_OWNER", "victor")
RUN = os.environ.get("VICTOR_RUN", "/run/victor")
SSH_DROPIN = "/etc/ssh/sshd_config.d/15-victor-mesh.conf"
TS_DROPIN_DIR = "/run/systemd/system/tailscaled.service.d"
NFT_SET = ("inet", "victor", "mesh_tcp")
MESH_V4 = ipaddress.ip_network("100.64.0.0/10")            # Headscale/Tailscale defaults
MESH_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
DEFAULTS = {"control_url": "", "hostname": "", "tag": "", "exit_node": False, "wol": False, "taildrop": False,
            "share": False, "ssh": False}
TOGGLES = ("exit_node", "wol", "taildrop", "share", "ssh")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,256}$")
MAC_RE = re.compile(r"^([0-9a-f]{2})([:-]?)([0-9a-f]{2})(\2[0-9a-f]{2}){4}$")


class MeshError(Exception):
    """Error with a message that is safe to show (never contains the pre-auth key)."""


# ---------------------------------------------------------------- validation

def normalize_url(url):
    """Headscale URL: https://host[:port][/path]. http:// only for a private/loopback address or a .local/.lan name."""
    url = (url or "").strip()
    if not url or len(url) > 255 or any(c.isspace() or ord(c) < 0x20 for c in url):
        raise MeshError("Enter your Headscale URL, for example https://headscale.example.com")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise MeshError("The Headscale URL must look like https://headscale.example.com (no user name, query or #).")
    try:
        p.port
    except ValueError:
        raise MeshError("The port in the Headscale URL is not valid.")
    if p.scheme == "http":
        host = p.hostname
        ok = host == "localhost" or host.endswith((".local", ".lan", ".home.arpa"))
        try:
            ip = ipaddress.ip_address(host)
            ok = ip.is_private or ip.is_loopback
        except ValueError:
            pass
        if not ok:
            raise MeshError("Use https:// for a Headscale server on the internet (http:// is only accepted on your own LAN).")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def valid_authkey(key):
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise MeshError("That pre-auth key does not look right (letters, digits, - and _ only). Create one with "
                        "`headscale preauthkeys create --user <you> --tags tag:victorlinux`.")
    return key


def valid_hostname(name):
    name = (name or "").strip().lower()
    if not name:
        return ""
    if not HOST_RE.match(name):
        raise MeshError("Device name: lowercase letters, digits and -, up to 63 characters.")
    return name


def default_hostname():
    h = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()).strip("-")[:63]
    return h if HOST_RE.match(h or "") else "victorlinux"


def edition(os_release="/etc/os-release"):
    forced = os.environ.get("VICTOR_MESH_EDITION")
    if forced in ("pc", "pi"):
        return forced
    try:
        text = open(os_release).read()
    except OSError:
        return "pc"
    return "pi" if re.search(r'^VARIANT_ID="?pi"?$', text, re.M) else "pc"


def tag_for(ed):
    return TAG_PI if ed == "pi" else TAG_PC


def is_mesh_ip(ip):
    try:
        a = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return a in MESH_V4 if a.version == 4 else a in MESH_V6


def valid_mac(mac):
    m = (mac or "").strip().lower()
    if not MAC_RE.match(m):
        raise MeshError("MAC address must look like aa:bb:cc:dd:ee:ff")
    return bytes.fromhex(re.sub(r"[:-]", "", m))


# ---------------------------------------------------------------- where state lives

def state_dir():
    """Mesh state on persistent storage when there is one, otherwise RAM (lost at power-off)."""
    forced = os.environ.get("VICTOR_MESH_DIR")
    if forced:
        return forced, True
    link = os.path.join(RUN, "root")                       # Debian / Pi edition
    data = os.path.join(RUN, "data")
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(data) and os.path.ismount(data):
        return os.path.join(data, "secrets", "mesh"), True
    try:                                                   # Arch edition
        st = json.load(open(os.path.join(RUN, "persist.json")))
        if st.get("mode") == "persistent" and st.get("data_root"):
            return os.path.join(st["data_root"], "security", "mesh"), True
    except (OSError, ValueError):
        pass
    return os.path.join(RUN, "mesh"), False


def paths():
    d, persistent = state_dir()
    return {"dir": d, "persistent": persistent, "settings": os.path.join(d, "settings.json"),
            "state": os.path.join(d, "tailscaled.state"), "share_auth": os.path.join(d, "share-auth.json"),
            "share_dir": os.path.join(d, "share"), "received": os.path.join(d, "received")}


def load_settings(p=None):
    p = p or paths()
    try:
        raw = json.load(open(p["settings"]))
    except (OSError, ValueError):
        raw = {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS and type(v) is type(DEFAULTS[k]):
            out[k] = v
    return out


def _write(path, text, mode=0o600, uid=None, gid=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    if uid is not None:
        os.chown(tmp, uid, gid)
    os.replace(tmp, path)


def save_settings(s, p=None, uid=None, gid=None):
    p = p or paths()
    clean = {k: s.get(k, v) for k, v in DEFAULTS.items()}
    _write(p["settings"], json.dumps(clean, indent=2, sort_keys=True) + "\n", 0o644, uid, gid)
    return clean


# ---------------------------------------------------------------- tailscale command lines (pure, tested)

def up_args(settings, authkey_file=None, operator=OWNER):
    """Arguments for `tailscale up`. --reset so that turning a flag off really turns it off."""
    if not settings.get("control_url"):
        raise MeshError("Join a mesh first (Headscale URL and pre-auth key).")
    a = ["up", "--reset", f"--login-server={settings['control_url']}", f"--advertise-tags={settings['tag'] or TAG_PC}",
         f"--operator={operator}", "--accept-dns=false", "--accept-routes=false", "--ssh=false", "--timeout=90s"]
    if settings.get("hostname"):
        a.append(f"--hostname={settings['hostname']}")
    if settings.get("exit_node"):
        a.append("--advertise-exit-node")
    if authkey_file:
        a.append(f"--auth-key=file:{authkey_file}")
    return a


def tailscaled_dropin(state_file, exe):
    """systemd drop-in (in /run, so it never outlives this boot) that keeps the node identity on persistent storage."""
    return ("[Service]\nExecStart=\n"
            f"ExecStart={exe} --state={state_file} --socket=/run/tailscale/tailscaled.sock --port=${{PORT}} $FLAGS\n")


def sshd_dropin(mesh_ip, owner=OWNER):
    return (f"# Victorious Mesh SSH: key-only, mesh address only (written by victor-mesh-priv)\nListenAddress {mesh_ip}\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"
            f"AllowUsers {owner}\nAuthenticationMethods publickey\nX11Forwarding no\n")


def magic_packet(mac_bytes):
    return b"\xff" * 6 + mac_bytes * 16


def send_wol(mac, broadcast="255.255.255.255", port=9):
    pkt = magic_packet(valid_mac(mac))
    ipaddress.ip_address(broadcast)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, (broadcast, port))
    return len(pkt)


# ---------------------------------------------------------------- share password (only a salted hash is stored)

def new_password():
    p = secrets.token_urlsafe(18)       # 24 characters, like the app's 18 random bytes
    return p


def hash_password(pw, salt=None, n=2 ** 14):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=8, p=1, dklen=32)
    return {"alg": "scrypt", "n": n, "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(dk).decode()}


def check_password(pw, rec):
    try:
        salt = base64.b64decode(rec["salt"])
        want = base64.b64decode(rec["hash"])
        got = hashlib.scrypt(pw.encode(), salt=salt, n=int(rec["n"]), r=8, p=1, dklen=32)
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def valid_password(p):
    return len(p) >= MIN_SHARE_PASSWORD and not any(c.isspace() or c == ":" for c in p)


# ---------------------------------------------------------------- share folder (same rules as the app)

def safe_name(raw):
    base = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(c for c in base if ord(c) >= 0x20 and c not in ':*?"<>|')
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("."):
        return None
    return cleaned[:120]


def unique_name(name, taken):
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, "." + ext) if dot and stem else (name, "")
    i = 2
    while f"{stem} ({i}){ext}" in taken:
        i += 1
    return f"{stem} ({i}){ext}"


def fits_cap(current, adding, cap=MAX_SHARE_BYTES):
    return adding >= 0 and current >= 0 and current + adding <= cap


def list_share(share_dir, cap=MAX_SHARE_BYTES):
    """Visible files: regular, not hidden, not symlinks, safe names; stop once the cap would be exceeded."""
    out, total = [], 0
    try:
        entries = sorted(os.scandir(share_dir), key=lambda e: e.name.lower())
    except OSError:
        return []
    for e in entries:
        if safe_name(e.name) != e.name or not e.is_file(follow_symlinks=False):
            continue
        size = e.stat(follow_symlinks=False).st_size
        if not fits_cap(total, size, cap):
            continue
        total += size
        out.append({"name": e.name, "size": size, "mtime": int(e.stat(follow_symlinks=False).st_mtime)})
    return out


def share_add(share_dir, src, cap=MAX_SHARE_BYTES):
    if not os.path.isfile(src):
        raise MeshError(f"Not a file: {src}")
    name = safe_name(os.path.basename(src))
    if not name:
        raise MeshError("That file name cannot be shared (hidden or empty).")
    os.makedirs(share_dir, mode=0o700, exist_ok=True)
    cur = list_share(share_dir, cap)
    size = os.path.getsize(src)
    if not fits_cap(sum(f["size"] for f in cur), size, cap):
        raise MeshError("The share is capped at 2 GB. Stop sharing something first.")
    if shutil.disk_usage(share_dir).free < size + 64 * 1024 * 1024:
        raise MeshError("Not enough free space to copy that file into the share.")
    name = unique_name(name, {e for e in os.listdir(share_dir)})
    dst = os.path.join(share_dir, name)
    tmp = os.path.join(share_dir, f".incoming-{secrets.token_hex(4)}")
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return name


def share_remove(share_dir, name):
    n = safe_name(name)
    if n != name:
        raise MeshError("No such shared file.")
    p = os.path.join(share_dir, n)
    if not os.path.isfile(p) or os.path.islink(p):
        raise MeshError("No such shared file.")
    os.unlink(p)


# ---------------------------------------------------------------- the read-only share server

PAGE_CSS = ("body{background:#0B0F1A;color:#e8eef9;font:15px system-ui,sans-serif;margin:0;padding:24px}"
            "h1{color:#00E5FF;font-size:20px;margin:0 0 4px}p{color:#9ba8c3}a{color:#00E5FF}"
            "table{border-collapse:collapse;width:100%;max-width:900px}td{padding:8px;border-bottom:1px solid #33406a}"
            "td.s{color:#FF2BD6;text-align:right;white-space:nowrap}")


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def render_index(files, host):
    rows = "".join(f'<tr><td><a href="/f/{urllib.parse.quote(f["name"])}" download>{html.escape(f["name"])}</a></td>'
                   f'<td class="s">{human(f["size"])}</td></tr>' for f in files) or '<tr><td>Nothing shared yet.</td></tr>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>VictorLinux mesh share</title><style>{PAGE_CSS}</style></head><body>'
            f'<h1>VictorLinux mesh share</h1><p>{html.escape(host)} · read-only · only devices on your mesh can see this page</p>'
            f'<table>{rows}</table></body></html>')


class Limiter:
    """Small per-address lockout for wrong passwords: 10 failures -> 60 s pause."""
    def __init__(self, limit=10, window=60):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, ip, now=None):
        now = now or time.monotonic()
        with self.lock:
            n, t = self.fails.get(ip, (0, now))
            if now - t > self.window:
                self.fails.pop(ip, None)
                return False
            return n >= self.limit

    def fail(self, ip, now=None):
        now = now or time.monotonic()
        with self.lock:
            n, t = self.fails.get(ip, (0, now))
            self.fails[ip] = (n + 1, t if now - t <= self.window else now)

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


def parse_basic(header):
    if not header or not header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    return raw.split(":", 1)[1]           # any user name works, like the app


def make_server(bind_ip, port, share_dir, auth_rec, cap=MAX_SHARE_BYTES, require_mesh=True):
    """Threaded HTTP server bound to ONE mesh address. GET/HEAD only. Raises MeshError for a non-mesh bind."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if require_mesh and not is_mesh_ip(bind_ip):
        raise MeshError(f"Refusing to serve on {bind_ip}: the share only listens on a mesh address (100.64.0.0/10 or fd7a:115c:a1e0::/48).")
    limiter = Limiter()

    class H(BaseHTTPRequestHandler):
        server_version = "VictorMeshShare"
        sys_version = ""

        def log_message(self, *a):          # no access log with names or addresses
            pass

        def _hdrs(self, code, ctype, length, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _text(self, code, msg, extra=None):
            body = (msg + "\n").encode()
            self._hdrs(code, "text/plain; charset=utf-8", len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authed(self):
            ip = self.client_address[0]
            if limiter.blocked(ip):
                self._text(429, "Too many wrong passwords. Try again in a minute.")
                return False
            pw = parse_basic(self.headers.get("Authorization"))
            if pw is not None and check_password(pw, auth_rec):
                limiter.ok(ip)
                return True
            if pw is not None:
                limiter.fail(ip)
            self._text(401, "Password required (any user name).", {"WWW-Authenticate": 'Basic realm="VictorLinux mesh share", charset="UTF-8"'})
            return False

        def do_GET(self):
            if not self._authed():
                return
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                body = render_index(list_share(share_dir, cap), f"{bind_ip}:{port}").encode()
                self._hdrs(200, "text/html; charset=utf-8", len(body))
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            if not path.startswith("/f/"):
                return self._text(404, "Not found")
            name = urllib.parse.unquote(path[3:])
            if "/" in name or safe_name(name) != name or name not in {f["name"] for f in list_share(share_dir, cap)}:
                return self._text(404, "Not found")
            fp = os.path.join(share_dir, name)
            try:
                fd = os.open(fp, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                return self._text(404, "Not found")
            with os.fdopen(fd, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                start, end, code = 0, size - 1, 200
                rng = self.headers.get("Range", "")
                m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
                if m and size and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1)); end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
                    else:
                        start = max(size - int(m.group(2)), 0)
                    if start > end:
                        return self._text(416, "Range not satisfiable", {"Content-Range": f"bytes */{size}"})
                    code = 206
                extra = {"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(name), "Accept-Ranges": "bytes"}
                if code == 206:
                    extra["Content-Range"] = f"bytes {start}-{end}/{size}"
                length = max(end - start + 1, 0)
                self._hdrs(code, "application/octet-stream", length, extra)
                if self.command == "HEAD":
                    return
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        do_HEAD = do_GET

        def _no(self):
            self._text(405, "Read-only share.", {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

    fam = socket.AF_INET6 if ":" in bind_ip else socket.AF_INET

    class S(ThreadingHTTPServer):
        address_family = fam
        daemon_threads = True

    return S((bind_ip, port), H)


# ---------------------------------------------------------------- reading status (unprivileged)

def run(cmd, timeout=30, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: timed out")


def tailscale_installed():
    return bool(shutil.which("tailscale") and (shutil.which("tailscaled") or os.path.exists("/usr/sbin/tailscaled")))


def ts_status():
    r = run(["tailscale", "status", "--json"], timeout=15)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def mesh_ips(st=None):
    st = st if st is not None else ts_status()
    if not st:
        return []
    return [ip for ip in (st.get("TailscaleIPs") or []) if is_mesh_ip(ip)]


def mesh_ipv4(st=None):
    return next((ip for ip in mesh_ips(st) if "." in ip), None)


def unit_active(unit):
    return run(["systemctl", "is-active", unit], timeout=10).stdout.strip() == "active"


def summarize(settings, st, p, services):
    """Status object for both Control Centers and `victor-mesh status` (no secrets)."""
    self_ = (st or {}).get("Self") or {}
    peers = []
    for peer in ((st or {}).get("Peer") or {}).values():
        peers.append({"name": peer.get("HostName") or peer.get("DNSName", ""), "ips": peer.get("TailscaleIPs") or [],
                      "online": bool(peer.get("Online")), "os": peer.get("OS", ""), "tags": peer.get("Tags") or []})
    ip4 = mesh_ipv4(st) if st else None
    return {"installed": tailscale_installed(), "joined": bool(settings.get("control_url")),
            "running": services.get("tailscaled", False), "backend": (st or {}).get("BackendState"),
            "control_url": settings.get("control_url"), "hostname": settings.get("hostname") or self_.get("HostName"),
            "tag": settings.get("tag"), "mesh_ips": mesh_ips(st) if st else [],
            "share_url": f"http://{ip4}:{SHARE_PORT}/" if ip4 and settings.get("share") else None,
            "toggles": {k: settings.get(k, False) for k in TOGGLES},
            "services": services, "state_persistent": p["persistent"],
            "peers": sorted(peers, key=lambda x: x["name"]),
            "share_files": list_share(p["share_dir"]) if os.access(p["share_dir"], os.R_OK) else []}
