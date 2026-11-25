"""Wi-Fi hotspot (Arch): an isolated access point so a phone can reach this PC's API without any router.

Never starts by itself. Requires the remote API to be on (API key still required). The admin UI stays on 127.0.0.1.
Root work (hostapd, DHCP-only dnsmasq, interface address) goes through `victor-priv hotspot-start|hotspot-stop`.
"""
import json
import os
import re
import subprocess

import hotspot
import qrsvg
import victorcommon as tc

RUN = subprocess.run     # tests inject a fake


def cfg_path():
    return tc.data_path("config", "hotspot.json")


def load():
    c = dict(hotspot.DEFAULTS)
    p = cfg_path()
    try:
        with open(p) as f:
            c.update({k: v for k, v in json.load(f).items() if k in hotspot.DEFAULTS})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return c


def _cmd(args):
    try:
        r = RUN(args, capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def radios():
    ifaces = hotspot.parse_iw_dev(_cmd(["iw", "dev"]))
    return {"ifaces": ifaces, "ap_capable": hotspot.ap_supported(_cmd(["iw", "list"])), "iw": bool(ifaces) or bool(_cmd(["iw", "--version"]))}


def running():
    return _cmd(["systemctl", "is-active", "victor-hotspot-ap"]).strip() == "active"


def clients(iface):
    return len(re.findall(r"^Station ", _cmd(["iw", "dev", iface, "station", "dump"]), re.M)) if iface else 0


def api_url(s):
    r = s["remote"]
    return f"{'https' if r.get('tls') else 'http'}://{hotspot.ADDR}:{r.get('port')}"


def state(app, q):
    c, s = load(), tc.load_settings()
    ok, why = hotspot.api_reachable(s["remote"])
    rad, on = radios(), running()
    iface = c["iface"] or (rad["ifaces"][0] if rad["ifaces"] else "")
    return 200, {"config": hotspot.public(c), "configured": bool(c["ssid"] and c["psk"]), "radios": rad, "running": on,
                 "clients": clients(iface) if on else 0, "api_ready": ok, "api_why": why, "api_url": api_url(s), "address": hotspot.ADDR,
                 "storage": bool(cfg_path())}


def configure(app, b):
    if not cfg_path():
        raise hotspot.HotspotError("Set up storage first (Storage tab) so the hotspot settings can be saved.")
    c = load()
    for k in hotspot.DEFAULTS:
        if k in b and not (k == "psk" and (b[k] == "" or set(str(b[k])) == {"•"})):   # masked/empty = keep
            c[k] = b[k]
    if b.get("new_password") or not c["psk"]:
        c["psk"] = hotspot.new_psk()
    if not c["ssid"]:
        c["ssid"] = hotspot.default_ssid(app.machine_id() if hasattr(app, "machine_id") else "")
    c = hotspot.validate(c)
    os.makedirs(os.path.dirname(cfg_path()), exist_ok=True)
    tc.atomic_write(cfg_path(), json.dumps(c), 0o600)
    return 200, {"ok": True, "config": hotspot.public(c), "message": "Hotspot settings saved."}


def reveal(app, b):
    c = load()
    if not c["psk"]:
        raise hotspot.HotspotError("Save the hotspot settings first.")
    qr = hotspot.wifi_qr(c)
    return 200, {"ssid": c["ssid"], "psk": c["psk"], "qr_svg": qrsvg.svg(qr)}


def start(app, b):
    c = load()
    if not (c["ssid"] and c["psk"]):
        raise hotspot.HotspotError("Save the hotspot settings first.")
    ok, why = hotspot.api_reachable(tc.load_settings()["remote"])
    if not ok:
        raise hotspot.HotspotError(why)
    c = hotspot.validate(c)
    rc, out = app.priv(["hotspot-start"], stdin=json.dumps(c), timeout=60)
    if rc != 0:
        raise hotspot.HotspotError("Hotspot could not start: " + out)
    return 200, {"ok": True, "message": f"Hotspot {c['ssid']} is on. Phones that join reach the API at {api_url(tc.load_settings())}."}


def stop(app, b):
    rc, out = app.priv(["hotspot-stop"], timeout=30)
    if rc != 0:
        raise hotspot.HotspotError("Hotspot could not stop: " + out)
    return 200, {"ok": True, "message": "Hotspot is off."}


def _wrap(fn):
    def w(app, data):
        try:
            return fn(app, data)
        except hotspot.HotspotError as e:
            return 400, {"error": str(e)}
    return w


ROUTES = {("GET", "/api/hotspot/state"): state, ("POST", "/api/hotspot/config"): _wrap(configure),
          ("POST", "/api/hotspot/reveal"): _wrap(reveal), ("POST", "/api/hotspot/start"): _wrap(start),
          ("POST", "/api/hotspot/stop"): _wrap(stop)}
