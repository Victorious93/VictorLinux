"""victor-ctl hotspot ...: isolated Wi-Fi access point via NetworkManager so a phone can reach this PC's API.

Never starts by itself. The firewall lets joined devices get an address (DHCP/DNS) and blocks all forwarding, so no
internet is shared and they reach only the opened API port (API key still required).
"""
import json, os, re, subprocess
from ..common import VictorError, data_path, write_json_atomic
from .. import hotspot, llama

NAME = "hotspot"
RUN = subprocess.run
PRIV = ["sudo", "-n", "/usr/local/lib/victorlinux/victor-priv"]
CON = "victor-hotspot"


def register(sp):
    p = sp.add_parser(NAME, help="isolated Wi-Fi hotspot so a phone can reach the API (never starts by itself)")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    st = s.add_parser("set"); st.add_argument("assign", nargs="+", metavar="KEY=VALUE", help="ssid, band (2.4|5), channel, country, iface")
    s.add_parser("new-password")
    s.add_parser("show-password")
    s.add_parser("start")
    s.add_parser("stop")


def _path():
    return data_path("config", "hotspot.json")


def _load():
    c = dict(hotspot.DEFAULTS)
    try:
        with open(_path()) as f:
            c.update({k: v for k, v in json.load(f).items() if k in hotspot.DEFAULTS})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return c


def _save(c):
    write_json_atomic(_path(), hotspot.validate(c), 0o600)


def _run(args, timeout=30):
    try:
        return RUN(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def wifi_ifaces():
    r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    return [ln.split(":")[0] for ln in r.stdout.splitlines() if ln.endswith(":wifi")] if r.returncode == 0 else []


def _ensure(c):
    if not c["ssid"]:
        c["ssid"] = hotspot.default_ssid()
    if not c["psk"]:
        c["psk"] = hotspot.new_psk()
    return c


def _api(c):
    lc = llama.load_config()
    ok, why = hotspot.api_reachable({"enabled": lc["remote"]["enabled"], "bind": lc["bind"]})
    scheme = "https" if lc["remote"]["enabled"] and lc["remote"]["tls"] else "http"
    return ok, why, f"{scheme}://{hotspot.ADDR}:{lc['port']}"


def handle(a):
    try:
        c = _load()
        if a.cmd == "status":
            ok, why, url = _api(c)
            active = _run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]).stdout.split()
            return {"config": hotspot.public(c), "running": CON in active, "wifi": wifi_ifaces(), "api_ready": ok, "api_why": why, "api_url": url}
        if a.cmd == "set":
            for x in a.assign:
                k, sep, v = x.partition("=")
                if not sep or k not in ("ssid", "band", "channel", "country", "iface"):
                    raise VictorError(f"Unknown setting {x} (use ssid, band, channel, country, iface; the password is never set on the command line)")
                c[k] = int(v) if k == "channel" and v.isdigit() else v
            if "band" in [x.partition("=")[0] for x in a.assign] and "channel" not in [x.partition("=")[0] for x in a.assign]:
                c["channel"] = 6 if c["band"] == "2.4" else 36
            _save(_ensure(c))
            return hotspot.public(_load())
        if a.cmd == "new-password":
            c["psk"] = hotspot.new_psk(); _save(_ensure(c))
            return {"ok": True, "message": "New password saved. Use show-password to see it."}
        if a.cmd == "show-password":
            c = _ensure(c); _save(c)
            return {"ssid": c["ssid"], "password": c["psk"], "qr_text": hotspot.wifi_qr(c)}
        if a.cmd == "start":
            c = _ensure(c); _save(c)
            ok, why, url = _api(c)
            if not ok:
                raise VictorError(why)
            ifaces = wifi_ifaces()
            iface = c["iface"] or (ifaces[0] if ifaces else "")
            if iface not in ifaces:
                raise VictorError("No Wi-Fi adapter found." if not ifaces else f"{iface} is not a Wi-Fi adapter.")
            r = _run([*PRIV, "hotspot-fw", iface, "on"])
            if r.returncode:
                raise VictorError("Firewall update failed: " + (r.stderr or "").strip())
            cmds = hotspot.nmcli_commands(iface, c, CON)
            _run(cmds[0])                                   # remove a previous profile (may not exist)
            for cmd in cmds[1:]:
                r = _run(cmd, timeout=60)
                if r.returncode:
                    _run([*PRIV, "hotspot-fw", iface, "off"])
                    raise VictorError("NetworkManager refused: " + ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1])
            return {"ok": True, "ssid": c["ssid"], "iface": iface, "api_url": url, "message": "Hotspot is on (isolated; no internet sharing)."}
        if a.cmd == "stop":
            _run(["nmcli", "connection", "down", CON])
            iface = c["iface"] or next(iter(wifi_ifaces()), "")
            if iface:
                _run([*PRIV, "hotspot-fw", iface, "off"])
            return {"ok": True, "message": "Hotspot is off."}
    except hotspot.HotspotError as e:
        raise VictorError(str(e))
