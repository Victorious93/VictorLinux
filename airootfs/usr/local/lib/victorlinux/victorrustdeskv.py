"""RustDeskV host helpers shared by `victor-rustdeskv` (user CLI) and `victor-rustdeskv-priv` (root half).

Pure logic only (validation, paths, policy). No secret is ever stored here: the owner's permanent password goes to
RustDeskV's own config through its IPC, and is never written to this repository, to /etc or to a log.
"""
import base64
import binascii
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

OWNER = "victor"
RUN = os.environ.get("VICTOR_RUN", "/run/victor")
STATE_DIR = os.environ.get("VICTOR_RUSTDESKV_STATE", "/var/lib/victorlinux/rustdeskv")
MARKER = os.path.join(STATE_DIR, "enabled")            # present = the owner finished first-boot setup this boot
RELAY_CONF = os.environ.get("VICTOR_RUSTDESKV_RELAY", "/etc/victorlinux/rustdeskv-relay.conf")
BIN = "/usr/bin/rustdesk"
CONFIG_DIR = "/root/.config/rustdesk"                   # config of the root-run service (id, keys, password hash)
UNIT = "victor-rustdeskv.service"

MIN_PASSWORD = 10
MAX_PASSWORD = 128
WEAK = {"password", "passw0rd", "1234567890", "0123456789", "qwertyuiop", "rustdesk123", "victorlinux", "letmein123"}

# Always applied at service start. Temporary one-time passwords and click-to-accept are off, so an unattended
# host can only be entered with the permanent password the owner chose.
POLICY = (("verification-method", "use-permanent-password"), ("approve-mode", "password"))
RELAY_KEYS = {"rendezvous_server": "custom-rendezvous-server", "relay_server": "relay-server",
              "api_server": "api-server", "key": "key"}

HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
KEY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{20,100}$")      # RustDesk server public key (base64); it is a public key


class SetupError(ValueError):
    pass


def validate_password(pw):
    """The owner's own permanent password. Never a default: there is none."""
    if not isinstance(pw, str) or "\n" in pw or "\r" in pw:
        raise SetupError("password must be one line")
    if len(pw) < MIN_PASSWORD:
        raise SetupError(f"password must be at least {MIN_PASSWORD} characters")
    if len(pw) > MAX_PASSWORD:
        raise SetupError(f"password must be at most {MAX_PASSWORD} characters")
    if not all(32 < ord(c) < 127 or ord(c) == 32 for c in pw) or pw != pw.strip():
        raise SetupError("password must be printable ASCII without leading or trailing spaces")
    if pw.startswith("-"):
        raise SetupError("password must not start with '-'")
    if len(set(pw)) < 4 or pw.lower() in WEAK:
        raise SetupError("password is too easy to guess")
    return pw


def validate_host(value, what="server"):
    """host[:port] with a hostname or an IP address. Empty is allowed (= not set)."""
    v = (value or "").strip()
    if not v:
        return ""
    host, port = v, None
    if v.startswith("["):                                # [ipv6]:port
        m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?", v)
        if not m:
            raise SetupError(f"{what}: bad IPv6 address")
        host, port = m.group(1), m.group(2)
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise SetupError(f"{what}: bad IPv6 address") from None
        host = f"[{host}]"
    else:
        if v.count(":") == 1:
            host, port = v.split(":")
        elif v.count(":") > 1:
            raise SetupError(f"{what}: write IPv6 addresses as [addr]:port")
        if re.fullmatch(r"[0-9.]+", host):
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                raise SetupError(f"{what}: bad IPv4 address") from None
        elif not HOST_RE.match(host):
            raise SetupError(f"{what}: bad host name")
    if port is not None and not (port.isdigit() and 1 <= int(port) <= 65535):
        raise SetupError(f"{what}: bad port")
    return v


def validate_api(value):
    """api_server is a URL in RustDesk (http/https); only https, or http to a private/loopback/.lan name."""
    v = (value or "").strip()
    if not v:
        return ""
    m = re.fullmatch(r"(https?)://([^/\s]+)", v)
    if not m:
        raise SetupError("api_server must look like https://host[:port]")
    host = validate_host(m.group(2), "api_server")
    if m.group(1) == "http":
        bare = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        private = bare.endswith((".lan", ".local", ".home.arpa")) or bare == "localhost"
        try:
            ip = ipaddress.ip_address(bare.strip("[]"))
            private = ip.is_private or ip.is_loopback
        except ValueError:
            pass
        if not private:
            raise SetupError("api_server over plain http is only accepted for a private address")
    return v


def parse_relay(text):
    """Parse the KEY=VALUE relay placeholder. Unknown keys and bad values are errors, never silently ignored."""
    out = {k: "" for k in RELAY_KEYS}
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SetupError(f"relay config line {n}: expected KEY=VALUE")
        k, v = (s.strip() for s in line.split("=", 1))
        if k not in RELAY_KEYS:
            raise SetupError(f"relay config line {n}: unknown key '{k}'")
        out[k] = v
    out["rendezvous_server"] = validate_host(out["rendezvous_server"], "rendezvous_server")
    out["relay_server"] = validate_host(out["relay_server"], "relay_server")
    out["api_server"] = validate_api(out["api_server"])
    if out["key"] and not KEY_RE.match(out["key"]):
        raise SetupError("key must be the server's public key (base64), not a password")
    if out["key"] and not out["rendezvous_server"]:
        raise SetupError("key is set but rendezvous_server is empty")
    return out


def load_relay(path=None):
    try:
        with open(path or RELAY_CONF) as f:
            return parse_relay(f.read())
    except FileNotFoundError:
        return parse_relay("")


def require_private_relay(cfg):
    """Require explicit self-hosted endpoints and a valid server public key before host startup."""
    for name in ("rendezvous_server", "relay_server"):
        if not cfg[name]:
            raise SetupError(f"{name} must be set before enabling the host")
    for name in ("rendezvous_server", "relay_server", "api_server"):
        value = cfg[name]
        if not value:
            continue
        host = urlsplit(value if name == "api_server" else "//" + value).hostname
        if host and (host.lower() == "rustdesk.com" or host.lower().endswith(".rustdesk.com")):
            raise SetupError(f"{name}: public RustDesk endpoints are not permitted for the host")
    try:
        if len(base64.b64decode(cfg["key"], validate=True)) != 32:
            raise ValueError("wrong key length")
    except (ValueError, binascii.Error):
        raise SetupError("set your server's 32-byte Ed25519 public key (id_ed25519.pub)") from None
    return cfg


def relay_options(cfg):
    """(rustdesk option, value) pairs for the validated configuration."""
    return [(RELAY_KEYS[k], cfg[k]) for k in RELAY_KEYS]


def persist_dir():
    """(path, persistent). Same detection as Victorious Mesh: Debian/Pi VICTORDATA, Arch persist.json, else RAM."""
    forced = os.environ.get("VICTOR_RUSTDESKV_PERSIST")
    if forced:
        return forced, True
    link, data = os.path.join(RUN, "root"), os.path.join(RUN, "data")
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(data) and os.path.ismount(data):
        return os.path.join(data, "secrets", "rustdeskv"), True
    try:
        with open(os.path.join(RUN, "persist.json")) as f:
            st = json.load(f)
        if st.get("mode") == "persistent" and st.get("data_root"):
            return os.path.join(st["data_root"], "security", "rustdeskv"), True
    except (OSError, ValueError):
        pass
    return None, False


def autostart_desktop(path_home):
    return os.path.join(path_home, ".config", "autostart", "victor-rustdeskv.desktop")


AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=RustDeskV
Comment=Start the RustDeskV client at login (written by victor-rustdeskv autostart on)
Exec=rustdesk
Icon=rustdesk
Terminal=false
X-GNOME-Autostart-enabled=true
"""
