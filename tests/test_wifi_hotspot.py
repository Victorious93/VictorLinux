"""Wi-Fi hotspot: validation, generated configs (isolated, no internet sharing), routes with a fake victor-priv, Debian parity."""
import argparse
import filecmp
import json
import os
import subprocess
import sys

import pytest

import cc_ext
import hotspot

ARCH = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux")
DEB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages")
DEB_LIB = os.path.join(DEB, "usr", "local", "lib", "victorlinux")
GOOD = {"ssid": "VictorLinux-AB12", "psk": "correcthorse9", "band": "2.4", "channel": 6, "country": "", "iface": ""}


def test_shared_module_is_identical_in_both_editions():
    assert filecmp.cmp(os.path.join(ARCH, "hotspot.py"), os.path.join(DEB_LIB, "victorlib", "hotspot.py"), shallow=False)


def test_validation_rejects_bad_values():
    assert hotspot.validate(GOOD)["ssid"] == "VictorLinux-AB12"
    bad = [{"ssid": ""}, {"ssid": "x" * 33}, {"ssid": "a\nb"}, {"psk": "short"}, {"psk": "x" * 64}, {"psk": "pässwört123"},
           {"band": "6"}, {"channel": 14}, {"channel": True}, {"band": "5", "channel": 36}, {"band": "5", "channel": 52, "country": "US"},
           {"country": "USA"}, {"iface": "wlan0; rm -rf /"}]
    for b in bad:
        with pytest.raises(hotspot.HotspotError):
            hotspot.validate(dict(GOOD, **b))
    assert hotspot.validate(dict(GOOD, band="5", channel=40, country="de"))["country"] == "DE"


def test_generated_configs_are_isolated_and_injection_safe():
    c = hotspot.validate(dict(GOOD, ssid="My Net=1"))
    h = hotspot.hostapd_conf("wlan0", c)
    assert "ap_isolate=1" in h and "wpa=2" in h and "rsn_pairwise=CCMP" in h
    assert "ssid2=" + "My Net=1".encode().hex() in h and "\nssid=" not in h      # hex SSID: no config injection
    d = hotspot.dnsmasq_conf("wlan0")
    assert "port=0" in d and "dhcp-option=3" in d and "dhcp-option=6" in d          # DHCP only: no DNS, no default route
    nm = hotspot.nmcli_commands("wlan0", c)
    add = nm[1]
    assert add[add.index("802-11-wireless.ap-isolation") + 1] == "yes"             # clients cannot reach each other (hostapd: ap_isolate=1)
    assert "autoconnect" in nm[1] and nm[1][nm[1].index("autoconnect") + 1] == "no"  # never comes back by itself
    with pytest.raises(hotspot.HotspotError):
        hotspot.hostapd_conf("wlan0", dict(c, psk="abcdefgh\ninterface=eth0"))
    assert hotspot.wifi_qr(dict(c, psk='a;b"c:d,e')) == 'WIFI:T:WPA;S:My Net=1;P:a\\;b\\"c\\:d\\,e;;'


def test_public_view_hides_password_and_parsers():
    assert "correcthorse9" not in json.dumps(hotspot.public(GOOD))
    assert hotspot.parse_iw_dev("phy#0\n\tInterface wlp2s0\n\t\taddr x\n") == ["wlp2s0"]
    iw = "Wiphy phy0\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n\t\t * monitor\n\tBand 1:\n"
    assert hotspot.ap_supported(iw) and not hotspot.ap_supported(iw.replace(" * AP\n", ""))
    assert hotspot.api_reachable({"enabled": False})[0] is False
    assert hotspot.api_reachable({"enabled": True, "bind": "127.0.0.1"})[0] is False
    assert hotspot.api_reachable({"enabled": True, "bind": "0.0.0.0"}) == (True, "")


def test_off_by_default_nothing_enables_it_on_boot():
    assert hotspot.DEFAULTS["ssid"] == "" and hotspot.DEFAULTS["psk"] == ""
    for root in (os.path.join(ARCH, "..", "..", "..", "..", "etc", "systemd"), os.path.join(DEB, "etc", "systemd")):
        for dp, _dn, fns in os.walk(root):
            assert not any("hotspot" in f or f.startswith(("hostapd", "dnsmasq")) for f in fns), dp


@pytest.fixture
def arch(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    import cc_ext.hotspot as ch
    remote = {"enabled": True, "bind": "0.0.0.0", "port": 8080, "tls": True}
    monkeypatch.setattr(ch.tc, "data_path", lambda *p: str(tmp_path.joinpath("data", *p)))
    monkeypatch.setattr(ch.tc, "load_settings", lambda: {"remote": remote})

    def fake_run(args, **kw):
        out = {"dev": "phy#0\n\tInterface wlan0\n", "list": "Wiphy phy0\n\tSupported interface modes:\n\t\t * AP\n"}.get(args[1], "")
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(ch, "RUN", fake_run)

    class App:
        calls = []

        def priv(self, args, stdin=None, timeout=60):
            self.calls.append((args, stdin))
            return 0, "{}"
    return ch, App(), remote


def test_arch_routes_save_start_stop_via_priv(arch):
    ch, app, remote = arch
    R = ch.ROUTES
    assert R[("POST", "/api/hotspot/start")](app, {})[0] == 400          # not configured yet
    st, body = R[("POST", "/api/hotspot/config")](app, {"ssid": "Bench", "band": "2.4", "channel": 11})
    assert st == 200 and body["config"]["psk"].startswith("•")            # password generated, never echoed
    saved = json.load(open(ch.cfg_path()))
    assert len(saved["psk"]) == 16 and oct(os.stat(ch.cfg_path()).st_mode & 0o777) == "0o600"
    st, body = R[("POST", "/api/hotspot/config")](app, {"psk": "••••"})  # masked value keeps the password
    assert json.load(open(ch.cfg_path()))["psk"] == saved["psk"]
    st, body = R[("POST", "/api/hotspot/start")](app, {})
    assert st == 200 and "https://10.77.0.1:8080" in body["message"]
    args, stdin = app.calls[-1]
    assert args == ["hotspot-start"] and json.loads(stdin)["ssid"] == "Bench"
    assert R[("POST", "/api/hotspot/stop")](app, {})[0] == 200 and app.calls[-1][0] == ["hotspot-stop"]
    rv = R[("POST", "/api/hotspot/reveal")](app, {})[1]
    assert rv["psk"] == saved["psk"] and rv["qr_svg"].startswith("<svg")
    remote["bind"] = "127.0.0.1"
    st, body = R[("POST", "/api/hotspot/start")](app, {})
    assert st == 400 and "0.0.0.0" in body["error"]
    st, s = R[("GET", "/api/hotspot/state")](app, {})
    assert s["radios"]["ifaces"] == ["wlan0"] and s["radios"]["ap_capable"] and saved["psk"] not in json.dumps(s)


def test_routes_registered():
    cc_ext.load()
    assert ("POST", "/api/hotspot/start") in cc_ext.ROUTES


def test_priv_verbs_present_in_both_editions():
    a = open(os.path.join(ARCH, "victor-priv")).read()
    assert '"hotspot-start": cmd_hotspot_start' in a and '"hotspot-stop": cmd_hotspot_stop' in a
    body = a[a.index("def cmd_hotspot_start"):a.index("def main")]
    assert "ip_forward" not in body and "masquerade" not in body.lower() and "iptables" not in body   # no internet sharing
    d = open(os.path.join(DEB_LIB, "victor-priv")).read()
    assert '"hotspot-fw": (cmd_hotspot_fw, 2)' in d
    nft = open(os.path.join(DEB, "etc", "nftables.conf")).read()
    assert "iifname @hotspot_if drop" in nft and "oifname @hotspot_if drop" in nft


@pytest.fixture
def deb(tmp_path, monkeypatch):
    sys.path.insert(0, DEB_LIB)
    try:
        from victorlib.ctl_ext import hotspot as dh
        from victorlib.common import VictorError
    finally:
        sys.path.remove(DEB_LIB)
    monkeypatch.setattr(dh, "_path", lambda: str(tmp_path / "hotspot.json"))
    monkeypatch.setattr(dh, "_api", lambda c: (True, "", "https://10.77.0.1:8080"))
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        out = "wlan0:wifi\neth0:ethernet\n" if args[:4] == ["nmcli", "-t", "-f", "DEVICE,TYPE"] else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(dh, "RUN", fake_run)
    return dh, VictorError, calls


def test_debian_cli(deb):
    dh, VictorError, calls = deb
    with pytest.raises(VictorError, match="password is never set"):
        dh.handle(argparse.Namespace(cmd="set", assign=["psk=hunter2hunter2"]))
    r = dh.handle(argparse.Namespace(cmd="set", assign=["ssid=Lab", "band=5", "country=us"]))
    assert r["ssid"] == "Lab" and r["channel"] == 36 and r["psk"].startswith("•")
    pw = dh.handle(argparse.Namespace(cmd="show-password"))
    assert pw["qr_text"].startswith("WIFI:T:WPA;S:Lab;")
    r = dh.handle(argparse.Namespace(cmd="start"))
    assert r["ok"] and ["sudo", "-n", "/usr/local/lib/victorlinux/victor-priv", "hotspot-fw", "wlan0", "on"] in calls
    add = next(c for c in calls if c[:3] == ["nmcli", "connection", "add"])
    assert "ap" in add and "no" == add[add.index("autoconnect") + 1]
    dh.handle(argparse.Namespace(cmd="stop"))
    assert calls[-1][-2:] == ["wlan0", "off"]
