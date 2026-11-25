"""Wi-Fi hotspot helpers shared by both editions (pure: no side effects, easy to test).

The hotspot is an ISOLATED network: phones and laptops that join it can reach this PC's API (API key still required),
nothing else. No internet sharing, no forwarding. The admin Control Center stays on 127.0.0.1. It never starts by
itself: only an explicit Start (and it is gone after a reboot).
"""
import re
import secrets
import string

ADDR = "10.77.0.1"
PREFIX = 24
DHCP_RANGE = ("10.77.0.10", "10.77.0.60")
DEFAULTS = {"ssid": "", "psk": "", "band": "2.4", "channel": 6, "country": "", "iface": ""}
CH_24 = tuple(range(1, 14))
CH_5 = (36, 40, 44, 48)                       # non-DFS UNII-1 only: usable without radar detection in most countries
IFACE_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,15}$")


class HotspotError(Exception):
    pass


def new_psk(n=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def default_ssid(machine_hint=""):
    tail = re.sub(r"[^A-Za-z0-9]", "", machine_hint)[-4:].upper() or secrets.token_hex(2).upper()
    return f"VictorLinux-{tail}"


def validate(c):
    """Return a cleaned copy or raise HotspotError with a user-facing message."""
    n = dict(DEFAULTS)
    n.update({k: c[k] for k in DEFAULTS if k in c})
    ssid = str(n["ssid"])
    if not 1 <= len(ssid.encode()) <= 32 or any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid):
        raise HotspotError("Network name must be 1-32 bytes without control characters.")
    psk = str(n["psk"])
    if not 8 <= len(psk) <= 63 or any(not 32 <= ord(ch) <= 126 for ch in psk):
        raise HotspotError("Password must be 8-63 printable ASCII characters.")
    if n["band"] not in ("2.4", "5"):
        raise HotspotError("Band must be 2.4 or 5 (GHz).")
    ch = n["channel"]
    if not isinstance(ch, int) or isinstance(ch, bool) or ch not in (CH_24 if n["band"] == "2.4" else CH_5):
        raise HotspotError("Channel must be 1-13 for 2.4 GHz or 36/40/44/48 for 5 GHz.")
    cc = str(n["country"]).upper()
    if cc and not re.fullmatch(r"[A-Z]{2}", cc):
        raise HotspotError("Country must be a two-letter code like US or DE.")
    if n["band"] == "5" and not cc:
        raise HotspotError("5 GHz needs your country code (regulatory rules).")
    n["country"] = cc
    if n["iface"] and not IFACE_RE.fullmatch(str(n["iface"])):
        raise HotspotError("Bad interface name.")
    return n


def public(c):
    """Settings for display: the password is only shown on explicit request."""
    p = dict(c)
    p["psk"] = "•" * len(c.get("psk") or "") if c.get("psk") else ""
    return p


def parse_iw_dev(text):
    """`iw dev` → list of wireless interface names."""
    return re.findall(r"^\s*Interface\s+(\S+)", text, re.M)


def ap_supported(iw_list_text):
    """True if any radio in `iw list` lists AP among its supported interface modes."""
    for block in re.split(r"\nWiphy ", "\n" + iw_list_text):
        m = re.search(r"Supported interface modes:\n((?:\s+\*.*\n?)+)", block)
        if m and re.search(r"\*\s+AP\s*$", m.group(1), re.M):
            return True
    return False


def hostapd_conf(iface, c):
    def esc(v):
        if "\n" in v:
            raise HotspotError("newline in value")
        return v
    lines = [f"interface={iface}", "driver=nl80211", f"ssid2={c['ssid'].encode().hex()}",
             f"hw_mode={'g' if c['band'] == '2.4' else 'a'}", f"channel={c['channel']}", "ieee80211n=1",
             "wmm_enabled=1", "auth_algs=1", "wpa=2", "wpa_key_mgmt=WPA-PSK", "rsn_pairwise=CCMP",
             f"wpa_passphrase={esc(c['psk'])}", "ap_isolate=1", "ignore_broadcast_ssid=0"]
    if c["band"] == "5":
        lines.append("ieee80211ac=1")
    if c["country"]:
        lines += [f"country_code={c['country']}", "ieee80211d=1"]
    return "\n".join(lines) + "\n"


def dnsmasq_conf(iface):
    """DHCP only (port=0 disables DNS). No router/DNS options: clients keep their own internet (mobile data)."""
    return "\n".join([f"interface={iface}", "bind-interfaces", "except-interface=lo", "port=0", "no-resolv", "no-hosts",
                      f"dhcp-range={DHCP_RANGE[0]},{DHCP_RANGE[1]},255.255.255.0,2h", "dhcp-option=3", "dhcp-option=6",
                      "dhcp-authoritative", "leasefile-ro"]) + "\n"


def nmcli_commands(iface, c, con="victor-hotspot"):
    """NetworkManager (Debian): an AP connection with NM's built-in DHCP. Forwarding is blocked by the firewall."""
    band = "bg" if c["band"] == "2.4" else "a"
    return [["nmcli", "connection", "delete", con],
            ["nmcli", "connection", "add", "type", "wifi", "ifname", iface, "con-name", con, "autoconnect", "no", "ssid", c["ssid"],
             "802-11-wireless.mode", "ap", "802-11-wireless.band", band, "802-11-wireless.channel", str(c["channel"]),
             "802-11-wireless-security.key-mgmt", "wpa-psk", "802-11-wireless-security.proto", "rsn",
             "802-11-wireless-security.pairwise", "ccmp", "802-11-wireless-security.psk", c["psk"],
             "ipv4.method", "shared", "ipv4.addresses", f"{ADDR}/{PREFIX}", "ipv6.method", "disabled"],
            ["nmcli", "connection", "up", con]]


def wifi_qr(c):
    """Standard Wi-Fi QR payload (phones join by scanning)."""
    def e(v):
        return re.sub(r'([\\;,:"])', r"\\\1", v)
    return f"WIFI:T:WPA;S:{e(c['ssid'])};P:{e(c['psk'])};;"


def api_reachable(remote):
    """The hotspot is only useful if the API listens on all interfaces (or the hotspot address) with keys."""
    if not remote.get("enabled"):
        return False, "Turn on Server & Access (remote API) first; the hotspot only gives phones a way to reach it."
    if remote.get("bind") not in ("0.0.0.0", "::", ADDR):
        return False, f"The API listens on {remote.get('bind')} only. Set it to 0.0.0.0 (all networks) in Server & Access."
    return True, ""
