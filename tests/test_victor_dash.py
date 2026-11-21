"""Multi-phone dashboard, microphone, voice and PC-detect logic (no phone, display, scrcpy or audio server needed)."""
from __future__ import annotations

import json

import pytest

import victordash as vd
import victormic as vm
import victorpcdetect as pcd
import victorvoice as vv


def phone(serial, model="Pixel 8", sid="", state="device", nick=""):
    return vd.Phone(serial, state, model, sid, nick)


# ---- identity -------------------------------------------------------------

def fake_run(table):
    def run(argv):
        key = (argv[2], argv[-1])
        if key not in table:
            raise OSError("offline")
        return table[key]
    return run


def test_usb_and_wifi_merge_only_when_identity_is_proven():
    run = fake_run({("R5CT1", "ro.serialno"): "R5CT1\n", ("R5CT1", "ro.product.device"): "shiba\n",
                    ("192.168.1.9:5555", "ro.serialno"): "R5CT1\n", ("192.168.1.9:5555", "ro.product.device"): "shiba\n"})
    out = vd.reconcile([phone("192.168.1.9:5555"), phone("R5CT1")], run)
    assert len(out) == 1 and out[0].adb_serial == "R5CT1" and out[0].stable_id == "shiba:R5CT1"


def test_unreadable_or_placeholder_identity_is_never_merged():
    run = fake_run({("A", "ro.serialno"): "unknown", ("A", "ro.product.device"): "x",
                    ("B", "ro.serialno"): "unknown", ("B", "ro.product.device"): "x"})
    assert len(vd.reconcile([phone("A"), phone("B")], run)) == 2
    assert len(vd.reconcile([phone("C"), phone("D")], fake_run({}))) == 2     # probes fail: two separate phones


def test_two_phones_same_model_stay_distinct():
    run = fake_run({("A", "ro.serialno"): "S1", ("A", "ro.product.device"): "shiba",
                    ("B", "ro.serialno"): "S2", ("B", "ro.product.device"): "shiba"})
    assert len(vd.reconcile([phone("A"), phone("B")], run)) == 2


def test_transport_detection():
    assert vd.transport_of("192.168.1.9:5555") == "wifi"
    assert vd.transport_of("adb-R5CT1-x._adb-tls-connect._tcp") == "wifi"
    assert vd.transport_of("R5CT1") == "usb"


# ---- tiles, focus, fourth phone ---------------------------------------------

def three():
    d = vd.Dashboard()
    for i, s in enumerate(("A", "B", "C")):
        d.add(phone(s, sid=f"id{i}", model=f"M{i}"))
        d.mark_live(i)
    return d


def test_fourth_phone_requires_explicit_replacement_choice():
    d = three()
    with pytest.raises(vd.PickReplacement) as ei:
        d.add(phone("D", sid="id3"))
    assert {t.slot for t in ei.value.candidates} == {0, 1, 2}
    assert d.tile_for("D") is None                      # nothing was replaced silently
    t = d.add(phone("D", sid="id3"), replace_slot=1)
    assert t.slot == 1 and d.tiles[1].phone.adb_serial == "D" and d.tiles[0].phone.adb_serial == "A"


def test_adding_same_phone_twice_returns_same_tile():
    d = three()
    assert d.add(phone("A2", sid="id0")) is d.tiles[0]


def test_dropped_phone_only_affects_its_own_tile_and_clears_focus():
    d = three()
    d.focus(1)
    d.grant_control(1)
    d.grant_control(2)
    d.phone_gone("B")
    assert d.tiles[1].state == "dropped" and d.tiles[1].mode == "view"
    assert d.focus_slot is None
    assert d.tiles[0].state == d.tiles[2].state == "live" and d.tiles[2].mode == "control"


def test_reconnect_needs_fresh_session_and_fresh_control_grant():
    d = three()
    t = d.tiles[1]
    old_session, old_epoch = t.session, t.epoch
    d.grant_control(1)
    grant_epoch = d.tiles[1].epoch
    d.phone_gone("B")
    back = d.phone_back(phone("B", sid="id1"))
    assert back.session != old_session and back.mode == "view" and back.state == "starting"
    d.mark_live(1)
    with pytest.raises(vd.InputRejected):
        d.authorize_input(old_session, grant_epoch)       # stale session
    with pytest.raises(vd.InputRejected):
        d.authorize_input(back.session, back.epoch)       # view-only until granted again
    d.grant_control(1)
    assert d.authorize_input(back.session, d.tiles[1].epoch) is d.tiles[1]


def test_input_for_one_tile_never_authorizes_another():
    d = three()
    a, b = d.grant_control(0), d.tiles[1]
    with pytest.raises(vd.InputRejected):
        d.authorize_input(b.session, b.epoch)             # B is view-only
    assert d.authorize_input(a.session, a.epoch).phone.adb_serial == "A"


def test_revoke_makes_old_epoch_stale_immediately():
    d = three()
    t = d.grant_control(0)
    epoch = t.epoch
    d.revoke_control(0)
    with pytest.raises(vd.InputRejected):
        d.authorize_input(t.session, epoch)


def test_release_input_clears_focus_and_all_grants():
    d = three()
    d.focus(2); d.grant_control(2); d.grant_control(0)
    d.release_input()
    assert d.focus_slot is None and all(t.mode == "view" for t in d.tiles.values())


def test_not_ready_phone_is_refused():
    with pytest.raises(ValueError):
        vd.Dashboard().add(phone("X", state="unauthorized"))


# ---- geometry --------------------------------------------------------------

def test_grid_splits_width_with_gaps():
    g = vd.grid(3, 1200, 800, gap=10)
    assert len(g) == 3 and g[0] == (0, 0, 393, 800) and g[1][0] == 403
    assert vd.grid(1, 500, 400) == [(0, 0, 500, 400)] and vd.grid(0, 5, 5) == []


def test_letterbox_mapping_and_bars():
    # 1080x2400 phone in a 600x600 tile -> 270x600 image centred at x=165
    assert vd.fit_rect(1080, 2400, 600, 600) == (165, 0, 270, 600)
    assert vd.widget_to_device(10, 300, 600, 600, 1080, 2400) is None          # on the bar
    assert vd.widget_to_device(165, 0, 600, 600, 1080, 2400) == (0, 0)
    x, y = vd.widget_to_device(434, 599, 600, 600, 1080, 2400)
    assert x == 1079 - 0 or 1070 <= x <= 1079
    assert y == 2395 or 2390 <= y <= 2399


def test_rotation_swaps_axes_and_mapping_follows():
    assert vd.rotated_size(1080, 2400, 0) == (1080, 2400)
    assert vd.rotated_size(1080, 2400, 1) == (2400, 1080)
    assert vd.rotated_size(1080, 2400, 3) == (2400, 1080)
    w, h = vd.rotated_size(1080, 2400, 1)
    # landscape device in a 600x600 tile: centre click lands in the middle of the device
    assert vd.widget_to_device(300, 300, 600, 600, w, h) == (1200, 540)


def test_parsers():
    assert vd.parse_wm_size("Physical size: 1080x2400\n") == (1080, 2400)
    assert vd.parse_wm_size("Physical size: 1080x2400\nOverride size: 720x1600") == (720, 1600)
    assert vd.parse_orientation("  SurfaceOrientation: 1\n") == 1
    assert vd.parse_orientation("nothing") is None


def test_scroll_swipe_direction_and_bounds():
    x1, y1, x2, y2, ms = vd.swipe_for_scroll("down", 1000, 2000)
    assert y2 < y1 and x1 == x2 and 0 <= y2 and y1 <= 2000
    with pytest.raises(ValueError):
        vd.swipe_for_scroll("sideways", 1, 1)


# ---- command builders ------------------------------------------------------

def test_scrcpy_argv_pins_serial_and_view_only():
    d = three()
    t = d.tiles[0]
    argv = vd.scrcpy_tile_argv(t, (0, 0, 393, 800), origin=(100, 50))
    assert argv[0] == "scrcpy" and "--serial=A" in argv and "--no-control" in argv
    assert "--window-x=100" in argv and "--window-y=50" in argv and "--window-borderless" in argv and "--no-audio" in argv
    d.grant_control(0)
    assert "--no-control" not in vd.scrcpy_tile_argv(d.tiles[0], None)
    assert sum(1 for a in vd.scrcpy_tile_argv(d.tiles[0], None) if a.startswith("--serial=")) == 1


def test_low_load_profile_is_lighter():
    t = three().tiles[0]
    low, bal = vd.scrcpy_tile_argv(t, None, profile="low-load"), vd.scrcpy_tile_argv(t, None)
    assert "--max-fps=15" in low and "--max-size=720" in low and "--max-fps=30" in bal


def test_no_positioning_when_scrcpy_cannot():
    t = three().tiles[0]
    assert not any(a.startswith("--window-x") for a in vd.scrcpy_tile_argv(t, (0, 0, 1, 1), supports={"window-position": False}))


def test_adb_actions_are_whitelisted_and_target_one_serial():
    t = three().tiles[2]
    assert vd.adb_action_argv(t, "back") == ["adb", "-s", "C", "shell", "input", "keyevent", "4"]
    assert vd.adb_action_argv(t, "recents")[-1] == "187"
    assert vd.adb_action_argv(t, "text", "hello world")[-1] == "hello%sworld"
    for bad in ("hi; reboot", "$(id)", "a`b`", "x|y"):
        with pytest.raises(ValueError):
            vd.adb_action_argv(t, "text", bad)
    with pytest.raises(ValueError):
        vd.adb_action_argv(t, "rm")


def test_wireless_pair_and_connect_validate_endpoint():
    assert vd.adb_pair_argv("192.168.1.9:37099") == ["adb", "pair", "192.168.1.9:37099"]
    for bad in ("192.168.1.9", "evil.com:22", "1.2.3.4:5555; id"):
        with pytest.raises(ValueError):
            vd.adb_pair_argv(bad)
        with pytest.raises(ValueError):
            vd.adb_connect_argv(bad)


def test_probe_features_gives_specific_recovery_steps():
    r = vd.probe_features("", "", adb_present=False)
    assert not r["ready"] and any("android-tools" in p for p in r["problems"]) and any("trixie-backports" in p for p in r["problems"])
    full = "--serial --no-control --window-x --window-width --window-borderless --no-audio --max-fps"
    r = vd.probe_features(full, "scrcpy 2.4 <https://github.com/Genymobile/scrcpy>", True)
    assert r["ready"] and r["version"] == (2, 4, 0) and r["problems"] == []
    old = vd.probe_features("--serial --no-control", "scrcpy 1.25", True)
    assert old["ready"] and any("position" in p for p in old["problems"]) and any("--no-audio" in p for p in old["problems"])


def test_session_type():
    assert vd.session_type({"XDG_SESSION_TYPE": "Wayland"}) == "wayland"
    assert vd.session_type({"DISPLAY": ":0"}) == "x11" and vd.session_type({}) == "none"


# ---- microphone -------------------------------------------------------------

def src(name, desc="", props=None, mute=False, state="SUSPENDED", port=None, monitor_of=None):
    return {"name": name, "description": desc or name, "mute": mute, "state": state, "properties": props or {},
            "active_port": port, "monitor_of_sink": monitor_of}


BUILTIN = src("alsa_input.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Analog Stereo", {"device.form_factor": "internal"}, port="analog-input-internal-mic")
JACK = src("alsa_input.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Headset Mic", {"device.form_factor": "internal"}, port="analog-input-headset-mic")
USB = src("alsa_input.usb-Blue_Microphones_Yeti-00.analog-stereo", "Yeti", {"device.bus": "usb", "device.vendor.id": "0b05", "device.product.id": "1234", "device.serial": "YT1"})
USB_REPLUG = dict(USB, name="alsa_input.usb-Blue_Microphones_Yeti-00.mono-fallback")
BT = src("bluez_input.AA_BB_CC_DD_EE_FF.0", "Headset", {"api.bluez5.address": "aa:bb:cc:dd:ee:ff"})
MONITOR = src("alsa_output.pci-0000_00_1f.3.analog-stereo.monitor", "Monitor of Built-in", monitor_of="alsa_output.pci")


def listing(*items, default=""):
    return lambda: (json.dumps(list(items)), default or (items[0]["name"] if items else ""))


def test_monitors_are_excluded_and_kinds_detected():
    ss = vm.parse_pactl_sources(json.dumps([MONITOR, BUILTIN, USB, BT]), BUILTIN["name"])
    assert [s.kind for s in ss] == ["builtin", "usb", "bluetooth"]
    assert all(".monitor" not in s.name for s in ss)


def test_headset_jack_distinct_from_internal_mic():
    a, b = vm.parse_pactl_sources(json.dumps([BUILTIN]))[0], vm.parse_pactl_sources(json.dumps([JACK]))[0]
    assert a.stable_id != b.stable_id and b.kind == "headset-jack"


def test_usb_identity_survives_replug_and_profile_change():
    a = vm.parse_pactl_sources(json.dumps([USB]))[0]
    b = vm.parse_pactl_sources(json.dumps([USB_REPLUG]))[0]
    assert a.stable_id == b.stable_id == "usb:0b05:1234:YT1"


def test_bad_pactl_output_raises():
    with pytest.raises(ValueError):
        vm.parse_pactl_sources("not json")


def test_hotplug_event_filter():
    assert vm.parse_subscribe_line("Event 'new' on source #52") == ("new", "source")
    assert vm.parse_subscribe_line("Event 'remove' on card #3") == ("remove", "card")
    assert vm.parse_subscribe_line("Event 'change' on sink #1") is None
    assert vm.parse_subscribe_line("garbage") is None


def test_policy_follow_default_preferred_and_missing():
    srcs = vm.parse_pactl_sources(json.dumps([BUILTIN, USB]), BUILTIN["name"])
    assert vm.choose_active(srcs, vm.Policy())[0].kind == "builtin"
    pref = vm.Policy(vm.PREFERRED, "usb:0b05:1234:YT1")
    assert vm.choose_active(srcs, pref)[0].kind == "usb"
    only_builtin = srcs[:1]
    act, why = vm.choose_active(only_builtin, pref)
    assert act is None and "preferred" in why
    # follow-default with "use preferred when connected": falls back to default when preferred absent
    soft = vm.Policy(vm.FOLLOW_DEFAULT, "usb:0b05:1234:YT1", True)
    assert vm.choose_active(only_builtin, soft)[0].kind == "builtin"
    assert vm.choose_active(srcs, soft)[0].kind == "usb"
    assert vm.choose_active([], soft)[0] is None


def test_hotplug_sequence_start_empty_plug_select_unplug_replug_and_cancel_utterance():
    state = {"items": []}
    cancels, changes = [], []
    mgr = vm.MicManager(lambda: (json.dumps(state["items"]), state["items"][0]["name"] if state["items"] else ""),
                        vm.Policy(vm.PREFERRED, "usb:0b05:1234:YT1"), on_change=changes.append, on_cancel=cancels.append)
    st = mgr.refresh()
    assert st.active is None and not st.ready_for_ptt and "No microphone" in st.message or "preferred" in st.message
    with pytest.raises(RuntimeError):
        mgr.begin_utterance()
    state["items"] = [USB]                                    # plug in while dashboard is open
    assert mgr.refresh().ready_for_ptt
    mgr.begin_utterance()
    state["items"] = []                                       # unplug mid-utterance
    st = mgr.refresh()
    assert st.active is None and cancels and "cancelled" in cancels[0]
    state["items"] = [USB_REPLUG]                             # preferred mic returns (different profile name)
    st = mgr.refresh()
    assert st.active.stable_id == "usb:0b05:1234:YT1" and st.ready_for_ptt
    assert len(changes) >= 3


def test_default_device_change_moves_follow_default():
    state = {"default": BUILTIN["name"]}
    mgr = vm.MicManager(lambda: (json.dumps([BUILTIN, USB]), state["default"]), vm.Policy())
    assert mgr.refresh().active.kind == "builtin"
    state["default"] = USB["name"]
    assert mgr.refresh().active.kind == "usb"


def test_mute_blocks_push_to_talk():
    mgr = vm.MicManager(listing(dict(BUILTIN, mute=True)), vm.Policy())
    st = mgr.refresh()
    assert st.muted and not st.ready_for_ptt and "MUTED" in st.message
    with pytest.raises(RuntimeError, match="muted"):
        mgr.begin_utterance()


def test_audio_server_down_is_reported_not_raised():
    def boom():
        raise OSError("no pactl")
    st = vm.MicManager(boom, vm.Policy()).refresh()
    assert st.active is None and "not reachable" in st.message


def test_policy_roundtrip(tmp_path):
    p = tmp_path / "x" / "phone-dash.json"
    vm.save_policy(str(p), vm.Policy(vm.PREFERRED, "usb:1:2:3", False))
    assert vm.load_policy(str(p)) == vm.Policy(vm.PREFERRED, "usb:1:2:3", False)
    assert vm.load_policy(str(tmp_path / "missing.json")) == vm.Policy()


def test_pcm_level_and_capture_argv():
    assert vm.pcm_level(b"\x00\x00" * 100) == 0.0
    assert vm.pcm_level((32767).to_bytes(2, "little", signed=True) * 100) == 1.0
    assert vm.capture_argv("alsa_input.x")[:2] == ["parec", "--device=alsa_input.x"]


# ---- voice ------------------------------------------------------------------

@pytest.mark.parametrize("text,kind,target,param", [
    ("Show all three phones", "show_all", None, None),
    ("show two phones", "show_n", None, 2),
    ("Focus my Nord", "focus", "my nord", None),
    ("Go back on Phone Two", "back", "phone two", None),
    ("scroll down", "scroll", None, "down"),
    ("Scroll up on phone three", "scroll", "phone three", "up"),
    ("Read the selected PC text", "read_selection", None, None),
    ("home", "home", None, None),
    ("recent apps on the pixel", "recents", "pixel", None),
    ("full screen", "full_screen_toggle", None, None),
    ("stop reading", "stop_reading", None, None),
])
def test_grammar(text, kind, target, param):
    a = vv.parse_command(text)
    assert isinstance(a, vv.Action) and (a.kind, a.target, a.param) == (kind, target, param)


@pytest.mark.parametrize("text", [
    "", "run rm -rf slash", "open a terminal", "execute shell command ls", "sudo reboot the phone", "send a text to mom",
    "type adb shell on phone one", "install this apk", "back door", "format the phone",
])
def test_nothing_unknown_or_shell_like_is_an_action(text):
    assert isinstance(vv.parse_command(text), vv.Unrecognized)


def test_action_kinds_have_no_shell_kind():
    assert not any("shell" in k or "exec" in k or "terminal" in k for k in vv.KINDS)


PHONES = [vv.Named("k-nord", vv.ordinal_names(0, "Nord 3", "Nord")), vv.Named("k-pixel", vv.ordinal_names(1, "Pixel 8")),
          vv.Named("k-pixel2", vv.ordinal_names(2, "Pixel 8"))]


def test_named_target_resolves_by_stable_identity():
    r = vv.resolve_target("my nord", PHONES, None, None)
    assert r == vv.Resolved("k-nord")
    assert vv.resolve_target("phone two", PHONES, None, None) == vv.Resolved("k-pixel")


def test_duplicate_names_and_low_confidence_ask():
    r = vv.resolve_target("pixel 8", PHONES, None, None)
    assert isinstance(r, vv.NeedTarget) and set(r.candidates) == {"phone two", "phone three"}
    assert isinstance(vv.resolve_target("galaxy fold", PHONES, None, None), vv.NeedTarget)


def test_unnamed_command_binds_to_focus_at_ptt_start_and_asks_if_focus_moved():
    assert vv.resolve_target(None, PHONES, "k-pixel", "k-pixel") == vv.Resolved("k-pixel")
    moved = vv.resolve_target(None, PHONES, "k-pixel", "k-nord")
    assert isinstance(moved, vv.NeedTarget) and "Focus changed" in moved.reason
    assert isinstance(vv.resolve_target(None, PHONES, None, None), vv.NeedTarget)
    assert isinstance(vv.resolve_target("nord", [], None, None), vv.NeedTarget)


class FakeCap:
    def __init__(self):
        self.closed, self.n = False, 0

    def read(self, n):
        self.n += 1
        return b"\x01\x00" * 10

    def close(self):
        self.closed = True


class FakeRec:
    def __init__(self, text):
        self.text, self.fed = text, 0

    def feed(self, b):
        self.fed += 1

    def finish(self):
        return self.text


def test_push_to_talk_opens_and_closes_capture_and_parses():
    cap, rec = FakeCap(), FakeRec("go back on phone two")
    focus = {"v": "k-nord"}
    ptt = vv.PushToTalk(lambda name: cap, lambda: rec, lambda: "mic", lambda: focus["v"])
    utt = ptt.press()
    ptt.pump(); ptt.pump()
    focus["v"] = "k-pixel"                                    # focus change during recognition
    u, act = ptt.release()
    assert cap.closed and rec.fed == 2 and u.focus_at_start == "k-nord" and act.kind == "back"


def test_cancel_discards_recognition_and_closes_stream():
    cap = FakeCap()
    ptt = vv.PushToTalk(lambda name: cap, lambda: FakeRec("show all phones"), lambda: "mic", lambda: None)
    ptt.press()
    ptt.cancel("The microphone was disconnected, so the command was cancelled.")
    u, act = ptt.release()
    assert cap.closed and act is None and "disconnected" in u.cancelled


def test_ptt_refused_while_reading_aloud():
    ptt = vv.PushToTalk(lambda n: FakeCap(), lambda: FakeRec(""), lambda: "mic", lambda: None, tts_busy=lambda: True)
    with pytest.raises(RuntimeError, match="suspended"):
        ptt.press()


def test_speech_status_reports_how_to_install(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path)}
    st = vv.speech_status(env, have_module=lambda n: False)
    assert not st["ready"] and str(tmp_path) in st["help"] and "pip install" in st["help"]
    m = tmp_path / "victorlinux" / "stt" / "vosk-model-small-en-us-0.15" / "am"
    m.mkdir(parents=True)
    (m / "final.mdl").write_text("x")
    st = vv.speech_status(env, have_module=lambda n: True)
    assert st["ready"] and st["model"].endswith("vosk-model-small-en-us-0.15")
    assert not vv.speech_status(env, have_module=lambda n: False)["ready"]


# ---- PC detect / read aloud ----------------------------------------------------

def test_xrandr_parse():
    text = "Screen 0: minimum 8 x 8\neDP-1 connected primary 1920x1080+0+0 (normal) 344mm x 193mm\nHDMI-1 disconnected\nDP-2 connected 2560x1440+1920+0 (normal)\n"
    assert pcd.parse_xrandr(text) == [{"name": "eDP-1", "primary": True, "width": 1920, "height": 1080, "x": 0, "y": 0},
                                      {"name": "DP-2", "primary": False, "width": 2560, "height": 1440, "x": 1920, "y": 0}]


def test_sharing_capability_reports_view_only_accurately():
    assert pcd.sharing_capability({}, False, {})["view"] is False
    x = pcd.sharing_capability({"XDG_SESSION_TYPE": "x11"}, False, {})
    assert x["view"] and x["control"]
    w = pcd.sharing_capability({"XDG_SESSION_TYPE": "wayland"}, False, {"xdg-desktop-portal": "/usr/bin/x"})
    assert w["view"] and not w["control"] and "view-only" in w["why"]
    assert not pcd.sharing_capability({"XDG_SESSION_TYPE": "wayland"}, True, {})["view"]


def test_detect_pc_collects_everything_with_fakes():
    info = pcd.detect_pc({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "USER": "victor"},
                         run=lambda argv: "eDP-1 connected primary 1920x1080+0+0 (normal)\n",
                         listing=listing(BUILTIN, USB, MONITOR), hw={"ram": {"total": 8}})
    assert info["session"]["type"] == "x11" and info["displays"][0]["name"] == "eDP-1"
    assert [a["kind"] for a in info["audio_inputs"]] == ["builtin", "usb"] and info["audio_inputs"][0]["active"]
    assert info["hardware"]["ram"]["total"] == 8 and info["hostname"]


def window_tree():
    return {"role": "frame", "children": [
        {"role": "label", "text": "Invoice 4411 due Friday"},
        {"role": "password text", "text": "hunter2"},
        {"role": "panel", "sensitive": True, "children": [{"role": "label", "text": "ssn 123-45-6789"}]},
        {"role": "label", "text": "hidden ad", "visible": False},
        {"role": "paragraph", "text": "Thanks for your order."},
    ]}


def test_window_read_excludes_protected_hidden_and_nearby_private_content():
    r = pcd.extract_window_text(window_tree())
    assert r.ok and r.text == "Invoice 4411 due Friday Thanks for your order."
    for secret in ("hunter2", "123-45-6789", "hidden ad"):
        assert secret not in r.text
    assert "3 protected" in r.explanation


def test_window_with_selection_reads_only_the_selection():
    t = window_tree()
    t["children"][0]["selected_text"] = "due Friday"
    r = pcd.extract_window_text(t)
    assert r.text == "due Friday" and r.explanation == "selection only"


def test_window_without_text_explains():
    r = pcd.extract_window_text({"role": "frame", "children": [{"role": "canvas"}, {"role": "password text", "text": "x"}]})
    assert not r.ok and "no readable text" in r.explanation and "protected" in r.explanation


def test_selection_reader_reports_missing_tool_and_empty(monkeypatch):
    monkeypatch.setattr(pcd.shutil, "which", lambda n: None)
    assert "xclip" in pcd.read_selection({"DISPLAY": ":0"}).explanation
    monkeypatch.setattr(pcd.shutil, "which", lambda n: "/usr/bin/" + n)
    assert pcd.read_selection({"DISPLAY": ":0"}, run=lambda a: "  hello \n world ").text == "hello world"
    assert "Nothing is selected" in pcd.read_selection({"DISPLAY": ":0"}, run=lambda a: "  ").explanation


def test_speaker_suspends_and_never_puts_text_on_argv(monkeypatch):
    monkeypatch.setattr(pcd.shutil, "which", lambda n: "/usr/bin/espeak-ng" if n == "espeak-ng" else None)
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen["argv"] = argv
            self.stdin = type("S", (), {"write": lambda s, t: seen.__setitem__("text", t), "close": lambda s: None})()

        def poll(self):
            return None

        def terminate(self):
            seen["terminated"] = True

    sp = pcd.Speaker(popen=P)
    sp.say("secret words")
    assert sp.busy and "secret words" not in " ".join(seen["argv"]) and seen["text"] == "secret words"
    sp.stop()
    assert seen["terminated"] and not sp.busy


# ---- packaging / launch -------------------------------------------------------

def test_new_dashboard_files_identical_in_both_editions_and_executable():
    import stat
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    arch, deb = root / "airootfs/usr/local", root / "debian-edition/config/includes.chroot_after_packages/usr/local"
    for name in ("victordash", "victordashctl", "victordashgui", "victormic", "victorvoice", "victorpcdetect", "victorphone"):
        assert (arch / "lib/victorlinux" / f"{name}.py").read_bytes() == (deb / "lib/victorlinux" / f"{name}.py").read_bytes(), name
    for d in (arch, deb):
        launcher = d / "bin/victor-phone-dash"
        assert launcher.stat().st_mode & stat.S_IXUSR and "main_dash" in launcher.read_text()
    assert (root / "airootfs/usr/share/applications/victor-phone-dash.desktop").read_bytes() == \
        (root / "debian-edition/config/includes.chroot_after_packages/usr/share/applications/victor-phone-dash.desktop").read_bytes()
    assert "victor-phone-dash" in (root / "airootfs/home/victor/.config/openbox/menu.xml").read_text()
    assert "victor-phone-dash" in (deb / "bin/victor-control").read_text()
    assert "/api/phone/dashboard" in (arch / "lib/victorlinux/control_center.py").read_text()


def test_launch_dashboard_refuses_root_and_no_display_and_missing_launcher():
    import victordashctl as dc
    calls = []
    popen = lambda *a, **k: calls.append((a, k))
    assert not dc.launch_dashboard({"DISPLAY": ":0"}, popen=popen, which=lambda n: "/x", euid=lambda: 0).ok
    assert "DISPLAY" in dc.launch_dashboard({}, popen=popen, which=lambda n: "/x", euid=lambda: 1000).message
    assert "not installed" in dc.launch_dashboard({"DISPLAY": ":0"}, popen=popen, which=lambda n: None, euid=lambda: 1000).message
    assert calls == []
    ok = dc.launch_dashboard({"DISPLAY": ":0"}, popen=popen, which=lambda n: "/usr/local/bin/victor-phone-dash", euid=lambda: 1000)
    assert ok.ok and calls[0][0][0] == ["/usr/local/bin/victor-phone-dash"] and calls[0][1]["start_new_session"]


def test_headless_dash_dry_run_json(monkeypatch, capsys):
    import victorphone as vp
    monkeypatch.setattr(vp, "list_devices", lambda: ([vp.Device("A", "device", "model:Pixel_8"), vp.Device("B", "unauthorized")], ""))
    assert vp.main_headless(["dash", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan) == 1 and "--serial=A" in plan[0]["argv"] and "--no-control" in plan[0]["argv"]
