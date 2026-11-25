"""Phone dashboard logic: push-to-talk source/binding/dispatch and the per-tile scrcpy runner. Work by Victorious."""
import io
import subprocess
import threading

import pytest

import victormic as vm
import victorphonedash as pd
import victorscreens as vs
import victorvoice as vv


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class FakeRec:
    def __init__(self, text="go back", conf=0.9): self.text, self.conf, self.got = text, conf, b""
    def reset(self): self.got = b""
    def accept(self, pcm): self.got += pcm
    def finish(self): return self.text, self.conf


class FakeCapture:
    """Stands in for parec: a finite PCM stream that then blocks until terminated."""
    def __init__(self, pcm=b"\x01\x00" * 3200):
        self._buf = io.BytesIO(pcm)
        self._done = threading.Event()
        self.terminated = False
        outer = self

        class Out:
            def read(self, n):
                b = outer._buf.read(n)
                if b:
                    return b
                outer._done.wait(2)
                return b""
        self.stdout = Out()

    def terminate(self): self.terminated = True; self._done.set()
    def wait(self, timeout=None): return 0
    def kill(self): self.terminate()
    def poll(self): return 0 if self.terminated else None


def _mgr(control=True):
    m = vs.TileManager()
    for d, n in (("a", "Nord"), ("b", "Pixel")):
        t = m.add(d, n); t.state = "live"; t.permissions = "control" if control else "view"
    return m


def _src(name, sid, default=False):
    return vm.PcSource(sid, name, name.title(), "usb", default)


# ------------------------------------------------------------------ capture plan

def test_capture_plan_uses_selected_pc_mic_via_pc_capture_argv():
    reg = vm.SessionRegistry("pc", Clock())
    s = vm.normalize({"mode": "pc", "pc_stable_id": "usb1"})
    plan = pd.capture_plan(s, [_src("alsa_in", "usb1"), _src("other", "x", True)], "other", reg, False)
    assert plan.kind == "pc" and plan.argv == ["parec", "--device=alsa_in", "--format=s16le", "--rate=16000",
                                               "--channels=1", "--latency-msec=40"]


def test_capture_plan_never_substitutes_when_chosen_pc_mic_missing():
    reg = vm.SessionRegistry("pc", Clock())
    plan = pd.capture_plan(vm.normalize({"mode": "pc", "pc_stable_id": "gone"}), [_src("other", "x", True)], "other", reg, False)
    assert plan.argv is None and plan.kind == "none" and plan.help


def test_capture_plan_phone_reads_receiver_virtual_source_only_when_live():
    c = Clock(); reg = vm.SessionRegistry("pc", c)
    ses = reg.offer("nord1", "Nord", "keyboard")
    s = vm.normalize({"mode": "phone", "phone": {"device_id": "nord1", "provider": "keyboard"}})
    assert pd.capture_plan(s, [], "", reg, True).argv is None            # consent but not streaming yet
    ses.live, ses.sharing, ses.last_frame_at = True, "sharing", c.t
    off = pd.capture_plan(s, [], "", reg, False)
    assert off.argv is None and any("--virtual-mic" in h for h in off.help)
    on = pd.capture_plan(s, [], "", reg, True)
    assert on.kind == "phone" and "--device=victor_phone_mic" in on.argv


def test_virtual_source_detection():
    run = lambda argv: subprocess.CompletedProcess(argv, 0, "1\talsa_input.x\tmod\n7\tvictor_phone_mic\tmod\n", "")
    assert pd.virtual_source_present(run)
    run2 = lambda argv: subprocess.CompletedProcess(argv, 0, "1\talsa_input.x\tmod\n", "")
    assert not pd.virtual_source_present(run2)


def test_missing_model_shows_install_steps(tmp_path):
    ok, msg = pd.recognizer_status(str(tmp_path / "none"))
    assert not ok and "alphacephei.com/vosk/models" in msg and "pip install" in msg and "keep working" in msg


def test_model_status_finds_unzipped_subfolder(tmp_path):
    (tmp_path / "vosk-model-small-en-us-0.15" / "am").mkdir(parents=True)
    st = vv.model_status(str(tmp_path))
    assert st["installed"] and st["path"].endswith("vosk-model-small-en-us-0.15")


# ------------------------------------------------------------------ PTT controller

def _ptt(mgr, rec, sent, cap=None):
    plan = pd.CapturePlan(["parec", "--device=x"], "pc", "USB mic", [])
    cap = cap or FakeCapture()
    run = lambda argv: (sent.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1]
    return pd.PttController(mgr, rec, lambda: plan, popen=lambda argv: cap, run=run), cap


def test_ptt_dispatches_through_victordispatch_to_focus_at_press():
    m = _mgr(); m.set_focus("b"); m.grant_input("b"); sent = []
    c, cap = _ptt(m, FakeRec("go home"), sent)
    assert "Pixel" in c.press()
    out = c.release()
    assert out.ok and sent == [["adb", "-s", "b", "shell", "input", "keyevent", "3"]]
    assert cap.terminated


def test_ptt_refused_like_a_click_when_input_not_held():
    m = _mgr(); m.set_focus("a"); sent = []
    c, _ = _ptt(m, FakeRec("go back"), sent)
    c.press(); out = c.release()
    assert not out.ok and sent == []


def test_ptt_focus_moved_mid_utterance_asks():
    m = _mgr(); m.set_focus("a"); m.grant_input("a"); sent = []
    c, _ = _ptt(m, FakeRec("go back"), sent)
    c.press(); m.set_focus("b"); out = c.release()
    assert not out.ok and out.ask is not None and out.ask.reason == "focus-changed" and sent == []


def test_ptt_named_phone_overrides_focus():
    m = _mgr(); m.set_focus("a"); m.grant_input("b"); sent = []
    c, _ = _ptt(m, FakeRec("pixel go back"), sent)
    c.press(); assert c.release().ok and sent[0][2] == "b"


def test_ptt_without_model_or_source_never_captures():
    m = _mgr(); started = []
    c = pd.PttController(m, None, lambda: pd.CapturePlan(["x"], "pc", "", []), popen=lambda a: started.append(a))
    assert "speech model" in c.press().lower() and not started
    c2 = pd.PttController(m, FakeRec(), lambda: pd.CapturePlan(None, "none", "", ["Plug it in"]), popen=lambda a: started.append(a))
    assert "Plug it in" in c2.press() and not started and not c2.release().ok


def test_ptt_source_vanishing_cancels_utterance():
    m = _mgr(); m.grant_input("a"); sent = []

    class Dead(FakeCapture):
        def __init__(self):
            super().__init__(b"")
            self.stdout.read = lambda n: b""
    c, cap = _ptt(m, FakeRec("go back"), sent, Dead())
    c.press(); c._reader.join(2)
    assert not c.release().ok and sent == []


# ------------------------------------------------------------------ tile runner

class FakeProc:
    def __init__(self, argv): self.argv, self.code = argv, None
    def poll(self): return self.code
    def terminate(self): self.code = -15
    def wait(self, timeout=None): return self.code
    def kill(self): self.code = -9


def _runner(n=2):
    m = vs.TileManager(); c = Clock(); started = []
    for i in range(n):
        m.add(f"s{i}", f"P{i}")
    r = pd.TileRunner(m, popen=lambda argv: (started.append(FakeProc(argv)), started[-1])[1], clock=c)
    return m, r, c, started


def test_runner_starts_view_only_then_goes_live():
    m, r, c, started = _runner()
    assert r.sync() == ["s0", "s1"] and all("--no-control" in p.argv for p in started)
    assert all(f"--serial=s{i}" in started[i].argv for i in range(2))
    c.t += 2; r.sync()
    assert all(t.state == "live" for t in m.tiles)


def test_only_the_input_holder_runs_with_control():
    m, r, c, started = _runner()
    r.sync(); c.t += 2; r.sync()
    m.get("s1").permissions = "control"; m.grant_input("s1")
    e0 = m.get("s1").epoch
    assert r.sync() == ["s1"]
    assert "--no-control" not in started[-1].argv and r.procs["s0"] is started[0]      # s0 untouched
    assert m.get("s1").epoch == e0 + 1
    m.release_input(); r.sync()
    assert "--no-control" in started[-1].argv


def test_reconnect_keeps_other_streams_and_drops_stale_input():
    m, r, c, started = _runner(3)
    r.sync(); c.t += 2; r.sync()
    t = m.get("s1"); t.permissions = "control"; m.grant_input("s1"); r.sync()
    old_epoch = t.epoch; others = (r.procs["s0"], r.procs["s2"])
    r.procs["s1"].code = 1                       # stream dropped
    r.sync()
    assert t.state == "reconnecting" and m.input_held == "" and t.epoch > old_epoch
    assert m.route_input("s1", old_epoch, {"kind": "touch"}) is None
    assert (r.procs["s0"], r.procs["s2"]) == others and "s1" not in r.procs       # backoff before restart
    c.t += 1.1; r.sync()
    assert "--no-control" in r.procs["s1"].argv and (r.procs["s0"], r.procs["s2"]) == others


def test_disconnect_and_remove_stop_only_that_tile():
    m, r, c, started = _runner(2)
    r.sync()
    m.set_state("s0", "disconnected"); r.sync()
    assert "s0" not in r.procs and started[0].code is not None and r.procs["s1"] is started[1]
    m.remove("s1"); r.sync()
    assert r.procs == {}


def test_fourth_phone_must_choose_tile():
    m = vs.TileManager()
    for i in range(3):
        pd.add_phone(m, f"s{i}", f"P{i}")
    with pytest.raises(vs.FourthPhone) as e:
        pd.add_phone(m, "s3", "P3")
    assert e.value.tiles == ["s0", "s1", "s2"]
    pd.add_phone(m, "s3", "P3", replace="s1")
    assert [t.device_id for t in m.tiles] == ["s0", "s2", "s3"]


def test_low_profile_limits_stream():
    t = vs.Tile("s0", "P")
    a = pd.tile_argv(t, False, "low")
    assert "--max-size=720" in a and "--max-fps=15" in a and "--no-control" in a and "--no-audio" in a


def test_embedding_decision_is_honest():
    assert not pd.embedding_supported({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, lambda x: "/usr/bin/" + x)[0]
    assert not pd.embedding_supported({"DISPLAY": ":0"}, lambda x: None)[0]
    assert pd.embedding_supported({"DISPLAY": ":0"}, lambda x: "/usr/bin/" + x)[0]
    assert pd.embed_argvs("0x1", 42, 300, 600)[0] == ["xdotool", "windowreparent", "0x1", "42"]


def test_headless_screens_dry_run(capsys):
    import victorphone
    assert victorphone.main_headless(["screens", "--serials", "a,b", "--control", "b", "--profile", "low", "--dry-run"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and "--no-control" in lines[0] and "--no-control" not in lines[1] and "--max-fps=15" in lines[1]
    assert victorphone.main_headless(["screens", "--serials", "a,b,c,d", "--dry-run"]) == 2
