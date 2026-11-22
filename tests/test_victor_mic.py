"""Tests: mic settings/resolution, VMIC1 stream, virtual mic, voice grammar, tiles, PC read policy."""
import sys, os; os.environ.setdefault("VICTORFLEET_LIB", os.path.join(os.path.dirname(__file__), "..", "fleet"))
import json, socket, ssl, struct, subprocess, threading, os
import pytest
import victormic as vm, victorvoice as vv, victorscreens as vs, pcread as pr


class Clock:
    def __init__(s): s.t = 1000.0
    def __call__(s): return s.t


def pcs(*names):
    return [vm.PcSource(n, n, n.upper(), "usb") for n in names]


def live(reg, dev="a", prov="keyboard", nick="Nord"):
    s = reg.offer(dev, nick, prov); s.live = True; s.sharing = "sharing"; s.last_frame_at = reg.clock(); return s


def test_default_follows_system_default_and_names_it():
    reg = vm.SessionRegistry("pc", Clock())
    r = vm.resolve(vm.normalize(None), pcs("a", "b"), "b", reg)
    assert r.delivering == "B" and r.state == "ok" and "B" in r.selected


def test_manual_pc_choice_survives_headset_hotplug():
    reg = vm.SessionRegistry("pc", Clock())
    s = vm.normalize({"mode": "pc", "pc_stable_id": "usb1"})
    r = vm.resolve(s, pcs("usb1", "headset"), "headset", reg)
    assert r.delivering_ref == "usb1"
    r = vm.resolve(s, pcs("headset"), "headset", reg)          # chosen mic unplugged: do NOT jump to the headset
    assert r.state == "missing" and r.delivering == "" and r.recovery


def test_two_phone_providers_are_separate_and_independent():
    c = Clock(); reg = vm.SessionRegistry("pc", c)
    live(reg, "a", "keyboard")
    sd = vm.normalize({"mode": "phone", "phone": {"device_id": "a", "provider": "droidcommand"}})
    sk = vm.normalize({"mode": "phone", "phone": {"device_id": "a", "provider": "keyboard"}})
    assert vm.resolve(sd, [], "", reg).state == "needs-consent"
    r = vm.resolve(sk, [], "", reg)
    assert r.state == "ok" and r.delivering == "Nord · Victorious Keyboard"


def test_no_phone_authorised_gives_connect_guidance():
    r = vm.resolve(vm.normalize(None), [], "", vm.SessionRegistry("pc", Clock()))
    assert r.state == "no-source" and any("Connect a phone microphone" in x for x in r.recovery)


def test_preferred_phone_fallback_only_when_enabled_and_consented():
    c = Clock(); reg = vm.SessionRegistry("pc", c)
    s = vm.normalize({"fallback_to_preferred_phone": True, "preferred_phone": {"device_id": "a", "provider": "keyboard"}})
    assert vm.resolve(s, [], "", reg).state == "no-source"
    ses = live(reg)
    r = vm.resolve(s, [], "", reg)
    assert r.fallback_used and r.delivering_kind == "phone"
    assert vm.resolve(s, pcs("x"), "x", reg).delivering_kind == "pc"      # PC mic present wins in default mode
    c.t += 4000                                                           # consent (3600 s) expires
    assert vm.resolve(s, [], "", reg).state == "no-source"


def test_remembered_preference_cannot_restart_capture_after_consent_expires_or_reboot():
    c = Clock(); reg = vm.SessionRegistry("pc", c)
    ses = live(reg)
    s = vm.normalize({"mode": "phone", "phone": {"device_id": "a", "provider": "keyboard"}})
    c.t += 7200
    assert vm.resolve(s, [], "", reg).state == "needs-consent"
    assert vm.resolve(s, [], "", vm.SessionRegistry("pc", c)).state == "needs-consent"   # "after reboot": empty registry


def test_settings_roundtrip_0600_and_garbage_tolerated(tmp_path):
    p = str(tmp_path / "d" / "mic.json")
    vm.save_settings({"mode": "phone", "phone": {"device_id": "a", "provider": "keyboard"}, "muted": True}, p)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert vm.load_settings(p)["phone"]["provider"] == "keyboard"
    open(p, "w").write("{nope")
    assert vm.load_settings(p)["mode"] == "default"
    assert vm.normalize({"mode": "phone", "phone": {"device_id": "a", "provider": "evil"}})["mode"] == "default"


PACTL = json.dumps([
    {"name": "alsa_input.usb-X-00.mono", "description": "USB Mic", "properties": {"device.bus": "usb", "device.serial": "S1", "device.vendor.id": "1", "device.product.id": "2"}, "monitor_of_sink": None},
    {"name": "alsa_output.pci.monitor", "description": "Monitor", "properties": {}, "monitor_of_sink": 3},
    {"name": "bluez_input.AA", "description": "Buds", "properties": {"device.api": "bluez", "api.bluez5.address": "AA"}},
    {"name": "alsa_input.pci.analog-stereo", "description": "Built-in", "properties": {"device.form_factor": "internal"}},
    {"name": vm.VirtualMic.SOURCE_NAME, "description": "ours", "properties": {}}])


def test_parse_sources_filters_monitors_and_ours_and_classifies():
    src = vm.parse_pactl_sources(PACTL, "bluez_input.AA")
    assert [s.kind for s in src] == ["usb", "bluetooth", "builtin"]
    assert src[0].stable_id == "1:2:S1" and src[1].is_default


def test_list_sources_without_audio_server_is_empty():
    def run(argv, **k): raise FileNotFoundError
    assert vm.list_pc_sources(run) == ([], "")


def _pair():
    a, b = socket.socketpair(); a.settimeout(5); b.settimeout(5); return a, b


def test_stream_auth_audio_bound_buffer_and_cancel_on_disconnect():
    c = Clock(); reg = vm.SessionRegistry("pc1", c); ses = reg.offer("a", "Nord", "keyboard")
    got, cancels, events = [], [], []
    rx = vm.MicReceiver(reg, [lambda s, p: got.append(p)], lambda e, s: events.append(e), lambda s: cancels.append(1))
    srv, cli = _pair()
    t = threading.Thread(target=rx.serve, args=(srv,)); t.start()
    vm.client_hello(cli, ses)
    loud = struct.pack("<320h", *([16000] * 320))
    for i in range(3): cli.sendall(vm.audio_frame(i, loud))
    cli.sendall(vm.audio_frame(1, loud))               # replayed seq: dropped
    cli.close(); t.join(5)
    assert len(got) == 3 and cancels and "disconnected" in events and not ses.live


def test_stream_rejects_wrong_secret_wrong_binding_and_expired():
    c = Clock(); reg = vm.SessionRegistry("pc1", c); ses = reg.offer("a", "Nord", "keyboard")
    for mutate in (lambda s: setattr(s, "secret", b"x" * 32), lambda s: setattr(s, "provider", "droidcommand"),
                   lambda s: setattr(s, "pc_id", "other")):
        import copy; bad = copy.copy(ses); mutate(bad)
        srv, cli = _pair(); rx = vm.MicReceiver(reg)
        th = threading.Thread(target=lambda: rx.serve(srv)); th.start()
        with pytest.raises((PermissionError, EOFError, OSError)): vm.client_hello(cli, bad)
        th.join(5); cli.close()
    c.t += 99999
    srv, cli = _pair(); rx = vm.MicReceiver(reg); th = threading.Thread(target=lambda: rx.serve(srv)); th.start()
    with pytest.raises((PermissionError, EOFError, OSError)): vm.client_hello(cli, ses)
    th.join(5)


def test_revocation_mid_stream_stops_audio_and_mute_cancels_utterance():
    c = Clock(); reg = vm.SessionRegistry("pc1", c); ses = reg.offer("a", "N", "droidcommand")
    got, cancels = [], []
    rx = vm.MicReceiver(reg, [lambda s, p: got.append(p)], on_utterance_cancel=lambda s: cancels.append(1))
    srv, cli = _pair(); th = threading.Thread(target=rx.serve, args=(srv,)); th.start()
    vm.client_hello(cli, ses); pcm = b"\x01\x00" * 320
    cli.sendall(vm.audio_frame(0, pcm))
    cli.sendall(vm.pack_frame(vm.T_CTRL, b'{"state":"muted"}')); cli.sendall(vm.audio_frame(1, pcm))
    import time
    for _ in range(100):
        if cancels: break
        time.sleep(0.02)
    reg.revoke(ses.session_id); cli.sendall(vm.audio_frame(2, pcm)); th.join(5)
    assert len(got) == 1 and len(cancels) >= 2


def test_live_buffer_is_bounded_and_drops_oldest():
    b = vm.LiveBuffer(100)
    for i in range(50): b.push(bytes([i]))
    assert len(b) == 5 and b.pop() == bytes([45]) and b.dropped == 45


def test_tls_loopback_with_real_cert(tmp_path):
    sys.path.insert(0, os.environ["VICTORFLEET_LIB"]); from victorfleet import tlsutil
    crt, key = tlsutil.ensure_cert(str(tmp_path), ["127.0.0.1"])
    c = Clock(); reg = vm.SessionRegistry("pc1", c); ses = reg.offer("a", "N", "keyboard"); got = []
    rx = vm.MicReceiver(reg, [lambda s, p: got.append(p)])
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1); ls.settimeout(10)
    ctx = tlsutil.server_context(crt, key)
    def server():
        conn, _ = ls.accept(); rx.serve(ctx.wrap_socket(conn, server_side=True))
    th = threading.Thread(target=server); th.start()
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); cctx.load_verify_locations(crt)
    raw = socket.create_connection(ls.getsockname(), timeout=5)
    s = cctx.wrap_socket(raw, server_hostname="127.0.0.1")
    vm.client_hello(s, ses); s.sendall(vm.audio_frame(0, b"\x05\x00" * 320)); s.close(); th.join(10)
    assert len(got) == 1


class FakeRun:
    def __init__(s): s.calls = []
    def __call__(s, argv, **k):
        s.calls.append(argv); n = len([c for c in s.calls if "load-module" in c])
        return subprocess.CompletedProcess(argv, 0, stdout=f"{40+n}\n" if "load-module" in argv else "orig_src\n", stderr="")


class FakeProc:
    def __init__(s): s.stdin = type("S", (), {"write": lambda self, b: None, "flush": lambda self: None, "close": lambda self: None})()
    def terminate(s): pass


def test_virtual_mic_not_default_unless_asked_and_unloaded_on_stop():
    r = FakeRun(); v = vm.VirtualMic(r, lambda a: FakeProc())
    v.start("Nord · Victorious Keyboard")
    assert not any("set-default-source" in c for c in r.calls) and v.active and "not default" in v.mode
    v.stop()
    assert ["pactl", "unload-module", "41"] in r.calls and ["pactl", "unload-module", "42"] in r.calls and not v.active
    r2 = FakeRun(); v2 = vm.VirtualMic(r2, lambda a: FakeProc()); v2.start("x", set_default=True); v2.stop()
    assert ["pactl", "set-default-source", "orig_src"] in r2.calls            # default restored


def test_virtual_mic_unsupported_cleans_up():
    def run(argv, **k): return subprocess.CompletedProcess(argv, 1, "", "no such module")
    with pytest.raises(RuntimeError): vm.VirtualMic(run, lambda a: FakeProc()).start("x")


PH = [vv.Phone("a", "Nord", 1), vv.Phone("b", "Pixel", 2), vv.Phone("c", "Moto", 3)]


def test_voice_commands():
    P = lambda t, f="a", **k: vv.parse(t, PH, f, **k)
    assert P("Show all three phones") == vv.Command("show_all")
    assert P("Focus my Nord") == vv.Command("focus", "a")
    assert P("Go back on Phone Two") == vv.Command("back", "b")
    assert P("read the selected PC text") == vv.Command("read_selected")
    assert P("go home", "c") == vv.Command("home", "c", True)             # bound to focus at PTT start
    assert isinstance(P("go home", "c", focus_now="a"), vv.Ask)           # focus moved mid-command
    assert P("go back", confidence=0.3).reason == "low-confidence"
    assert P("go back on nord and pixel").reason == "ambiguous"
    assert P("focus").reason == "ambiguous"
    assert P("format the disk; rm -rf /").reason == "unknown"


def test_echo_guard():
    c = Clock(); g = vv.EchoGuard(1.0, c)
    assert g.accept_input(); g.start_output(); assert not g.accept_input()
    g.end_output(); assert not g.accept_input(); c.t += 2; assert g.accept_input()


def test_missing_speech_model_help(tmp_path):
    st = vv.model_status(str(tmp_path / "none")); assert not st["installed"] and "import" in st["help"].lower()


def test_tiles_layout_fourth_phone_and_reconnect_isolated():
    m = vs.TileManager()
    for d in "abc": m.add(d, d.upper()); m.get(d).permissions = "control"; m.set_state(d, "live")
    with pytest.raises(vs.FourthPhone): m.add("d", "D")
    m.add("d", "D", replace="b"); assert [t.device_id for t in m.tiles] == ["a", "c", "d"]
    lay = m.layout(1000, 600); assert len(lay) == 3 and lay["a"][2] > lay["c"][2]
    m.set_state("d", "live"); m.get("d").permissions = "control"
    m.grant_input("c"); e = m.get("c").epoch
    assert m.route_input("c", e, {"kind": "touch"}) == ("c", {"kind": "touch"})
    assert m.route_input("a", m.get("a").epoch, {"kind": "touch"}) is None     # input not held for a
    ea = m.get("a").epoch; m.set_state("a", "reconnecting"); m.set_state("a", "live")
    assert m.get("c").epoch == e and m.get("a").epoch > ea                      # only A's epoch advanced
    assert m.route_input("c", e, {"kind": "touch"}) is not None
    m.set_state("c", "reconnecting"); assert m.route_input("c", e, {"kind": "touch"}) is None
    m.set_state("c", "live"); assert m.route_input("c", e, {"kind": "touch"}) is None   # stale epoch


def test_view_only_denies_input_and_rotation_mapping():
    m = vs.TileManager(); m.add("a", "A"); m.set_state("a", "live")
    with pytest.raises(PermissionError): m.grant_input("a")
    assert vs.map_point(100, 300, (0, 0, 1000, 600), 1080, 2400) is None          # letterbox bar
    x, y = vs.map_point(500, 300, (0, 0, 1000, 600), 2400, 1080)                   # after rotation to landscape
    assert abs(x - 1200) <= 2 and abs(y - 540) <= 2
    m.rotate("a", 90, 2400, 1080); assert m.get("a").epoch >= 2


class FakeBackend:
    def __init__(s, win, runs): s.win, s.r = win, runs
    def active_window(s): return s.win
    def runs(s, w): return s.r


def test_read_policy_privacy():
    w = pr.Window("w1", "app", "Notes")
    runs = [pr.TextRun("secret", "password text", "w1", True),
            pr.TextRun("old selection", "text", "w1", True, stamp=1.0),
            pr.TextRun("other window", "text", "w2", True, stamp=99.0),
            pr.TextRun("known selection", "text", "w1", True, stamp=99.0)]
    r = pr.read_selection(FakeBackend(w, runs), "Desk", True, now=100.0)
    assert r.ok and r.text == "known selection" and r.skipped_protected == 1 and "Desk" in pr.announce(r) and "Notes" in pr.announce(r)
    assert pr.read_selection(FakeBackend(w, runs), "Desk", False).reason == "not-enabled"
    assert pr.read_selection(FakeBackend(None, []), "Desk", True).reason == "no-window"
    assert pr.read_selection(FakeBackend(w, runs), "Desk", True, {"zzz"}).reason == "denied"
    only_pw = [pr.TextRun("pw", "password text", "w1")]
    assert pr.read_selection(FakeBackend(w, only_pw), "Desk", True).reason == "nothing-readable"


def test_detect_and_freshness():
    rep = pr.detect({"cpu_name": "X", "gpu_names": ["G"], "ram_total": 8 * 2**30}, "Desk", pcs("m"), "M", {"XDG_SESSION_TYPE": "wayland"}, lambda: 100.0)
    assert rep["memory_gib"] == 8.0 and rep["display_session"] == "wayland" and pr.freshness(rep, 110) == "fresh"
    assert pr.freshness(rep, 500).startswith("stale") and pr.freshness(None, 0) == "offline"


def test_serve_end_to_end_records_what_the_phone_sent(tmp_path):
    """Real process: `victor-mic serve` + a TLS client that pins the printed SPKI, as the Android apps do."""
    import hashlib, subprocess, sys, time, wave, math
    wavp = tmp_path / "rx.wav"
    port = 47900 + os.getpid() % 90
    root = os.path.join(os.path.dirname(__file__), "..")
    env = dict(os.environ, VICTORFLEET_LIB=os.path.join(root, "fleet"), XDG_CONFIG_HOME=str(tmp_path))
    p = subprocess.Popen([sys.executable, os.path.join(root, "airootfs/usr/local/bin/victor-mic"), "serve", "--port", str(port),
                          "--pc-id", "desk", "--offer", "Nord:dev1:keyboard", "--record", str(wavp), "--seconds", "8"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    offer = p.stdout.readline().strip().split(";")
    assert offer[0] == "vmic1" and offer[5] == "desk"
    time.sleep(0.8)
    ses = vm.PhoneSession(offer[3], offer[6], "Nord", offer[7], offer[5], bytes.fromhex(offer[4]))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5))
    # pin check like the apps do: SPKI of the presented cert must equal the offer's pin
    der = s.getpeercert(True)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    assert hashlib.sha256(x509.load_der_x509_certificate(der).public_key().public_bytes(*spki)).hexdigest() == offer[8]
    vm.client_hello(s, ses)
    tone = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * (i / 16000)))) for i in range(16000))
    for i in range(0, len(tone), 640):
        s.sendall(vm.audio_frame(i // 640, tone[i:i + 640]))
    s.sendall(vm.pack_frame(vm.T_BYE, b"")); time.sleep(0.5); s.close()
    p.terminate(); p.wait(10)
    w = wave.open(str(wavp)); got = w.readframes(w.getnframes())
    assert got == tone


def test_handover_between_apps_on_one_handset_releases_previous_owner():
    reg = vm.SessionRegistry("pc", Clock())
    a = reg.offer("h", "Nord", "droidcommand"); a.live = True
    b = reg.offer("h", "Nord", "keyboard")
    assert not a.consent_valid(reg.clock()) and b.consent_valid(reg.clock())
    assert reg.find("h", "droidcommand") is None and reg.find("h", "keyboard") is b
    other = reg.offer("h2", "Pixel", "keyboard")
    assert b.consent_valid(reg.clock()) and other.consent_valid(reg.clock())     # different handsets unaffected


def test_scrcpy_tiles_are_placed_per_serial_and_view_only_by_default():
    m = vs.TileManager()
    for d in "abc": m.add(d, d.upper())
    m.get("b").permissions = "control"
    argv = vs.scrcpy_tile_argvs(m, 1800, 900)
    assert set(argv) == {"a", "b", "c"}
    for d, a in argv.items():
        assert f"--serial={d}" in a and any(x.startswith("--window-x=") for x in a)
    assert "--no-control" in argv["a"] and "--no-control" in argv["b"]       # view_only default
    assert "--no-control" not in vs.scrcpy_tile_argvs(m, 1800, 900, view_only=False)["b"]
    assert "--no-control" in vs.scrcpy_tile_argvs(m, 1800, 900, view_only=False)["a"]   # not granted


def test_victor_pc_cli_detect_and_read_gating(tmp_path):
    import subprocess, sys
    root = os.path.join(os.path.dirname(__file__), "..")
    exe = os.path.join(root, "airootfs/usr/local/bin/victor-pc")
    d = subprocess.run([sys.executable, exe, "detect"], capture_output=True, text=True, timeout=30)
    assert d.returncode == 0 and "display_session" in json.loads(d.stdout)
    r = subprocess.run([sys.executable, exe, "read"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "turned off" in r.stdout


def test_status_snapshot_roundtrip_has_no_secret_and_goes_stale(tmp_path):
    c = Clock(); reg = vm.SessionRegistry("pc", c); s = live(reg, "a", "keyboard")
    p = str(tmp_path / "st.json"); vm.write_status(reg, p)
    assert s.secret.hex() not in open(p).read() and oct(os.stat(p).st_mode & 0o777) == "0o600"
    r2 = vm.read_status_registry("pc", p, clock=c)
    sel = vm.normalize({"mode": "phone", "phone": {"device_id": "a", "provider": "keyboard"}})
    assert vm.resolve(sel, [], "", r2).state == "ok"
    c.t += 30                                                   # serve stopped writing: phone must not look live
    assert vm.resolve(sel, [], "", vm.read_status_registry("pc", p, clock=c)).state == "needs-consent"


def test_voice_dispatch_targets_only_named_phone_and_respects_grants():
    import victordispatch as vd
    m = vs.TileManager()
    for d in "abc": m.add(d, {"a": "Nord", "b": "Pixel", "c": "Moto"}[d]); m.get(d).permissions = "control"; m.set_state(d, "live")
    sent = []
    run = lambda argv: (sent.append(argv), subprocess.CompletedProcess(argv, 0))[1]
    ph = [vv.Phone("a", "Nord", 1), vv.Phone("b", "Pixel", 2), vv.Phone("c", "Moto", 3)]
    cmd = vv.parse("go back on phone two", ph, "a")
    assert not vd.dispatch(cmd, m, run=run).ok and not sent                     # input not held: refused like a click
    m.grant_input("b")
    assert vd.dispatch(cmd, m, run=run).ok
    assert sent == [["adb", "-s", "b", "shell", "input", "keyevent", "4"]]       # only Pixel; fixed argv, no shell text
    assert vd.dispatch(vv.parse("focus my nord", ph, "b"), m, run=run).ok and m.focus == "a"
    assert vd.dispatch(vv.parse("go home", ph, "a", focus_now="c"), m, run=run).ask is not None
    assert len(sent) == 1


class FakeRec:
    def __init__(s, text="go back", conf=0.9): s.text, s.conf, s.got = text, conf, b""
    def reset(s): s.got = b""
    def accept(s, pcm): s.got += pcm
    def finish(s): return s.text, s.conf


def test_ptt_binds_focus_at_press_ignores_echo_and_cancels():
    import victorptt as vp
    c = Clock(); focus = ["a"]
    g = vv.EchoGuard(1.0, c); rec = FakeRec("go home")
    s = vp.PttSession(rec, PH, lambda: focus[0], g)
    s.press(); s.feed(b"\x01\x00" * 10); assert rec.got
    assert s.release() == vv.Command("home", "a", True)
    s.press(); g.start_output(); s.feed(b"\x01\x00" * 10); assert rec.got == b""         # TTS playing: ignored
    g.end_output(); c.t += 5
    s.cancel(); assert s.release().reason == "unknown"                                   # cancelled utterance cannot complete
    s.press(); focus[0] = "c"; r = s.release(); assert isinstance(r, vv.Ask) and r.reason == "focus-changed"
    s.press(); assert vp.VoskRecognizer.__name__ and True


def test_missing_model_gives_install_help(tmp_path):
    import victorptt as vp
    with pytest.raises(vp.RecognizerMissing) as e: vp.VoskRecognizer(str(tmp_path / "none"))
    assert "import" in str(e.value).lower() or "model" in str(e.value).lower()
    assert vp.pc_capture_argv("src")[0] == "parec"


def test_read_request_needs_its_own_optin_and_works_without_audio():
    def run(allow):
        reg = vm.SessionRegistry("pc1", Clock()); ses = reg.offer("a", "N", "droidcommand", allow_read=allow)
        rx = vm.MicReceiver(reg, read_handler=lambda: {"ok": True, "text": "hello", "pc": "pc1", "window": "W", "reason": ""})
        srv, cli = _pair(); th = threading.Thread(target=rx.serve, args=(srv,)); th.start()
        vm.client_hello(cli, ses); cli.sendall(vm.pack_frame(vm.T_READ_REQ, b""))
        t, body = vm.read_frame(cli); cli.sendall(vm.pack_frame(vm.T_BYE, b"")); th.join(5); cli.close()
        return t, json.loads(body)
    t, r = run(False); assert t == vm.T_READ_RESP and r == {"ok": False, "reason": "not-enabled"}
    t, r = run(True); assert r["ok"] and r["text"] == "hello" and r["window"] == "W"
