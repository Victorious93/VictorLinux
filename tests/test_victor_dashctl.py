"""Dashboard controller against fake `adb` and `scrcpy` executables: real processes, no phones."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

import victordash as vd
import victordashctl as dc
import victorvoice as vv

FAKE_ADB = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\n")
if a[:1] == ["devices"]:
    print("List of devices attached")
    for s in st["devices"]:
        print(f"{s}\tdevice product:x model:{st['props'][s]['model']} device:d")
    sys.exit(0)
if a[:1] == ["-s"]:
    s, rest = a[1], a[2:]
    if s not in st["devices"]:
        print("error: device not found", file=sys.stderr); sys.exit(1)
    if rest[:2] == ["shell", "getprop"]:
        print(st["props"][s].get(rest[2], ""))
    elif rest[:3] == ["shell", "wm", "size"]:
        print("Physical size: 1080x2400")
    sys.exit(0)
'''

FAKE_SCRCPY = r'''#!/usr/bin/env python3
import json, os, sys, time
if "--help" in sys.argv:
    print("--serial --no-control --window-x --window-width --window-borderless --no-audio --max-fps"); sys.exit(0)
if "--version" in sys.argv:
    print("scrcpy 2.4"); sys.exit(0)
open(os.path.join(os.environ["FAKE_PROCS"], str(os.getpid())), "w").write(json.dumps(sys.argv[1:]))
time.sleep(60)
'''


@pytest.fixture
def rig(tmp_path, monkeypatch):
    bindir, procs = tmp_path / "bin", tmp_path / "procs"
    bindir.mkdir(); procs.mkdir()
    for name, src in (("adb", FAKE_ADB), ("scrcpy", FAKE_SCRCPY)):
        f = bindir / name
        f.write_text(src.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state.json"
    log = tmp_path / "adb.log"
    props = {s: {"ro.serialno": f"SN-{s}", "ro.product.device": "dev", "model": f"M{s}"} for s in ("A", "B", "C", "D")}
    set_devices = lambda *ds: state.write_text(json.dumps({"devices": list(ds), "props": props}))
    set_devices("A", "B", "C")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(state)); monkeypatch.setenv("FAKE_LOG", str(log)); monkeypatch.setenv("FAKE_PROCS", str(procs))
    c = dc.DashController()
    c.probe()
    yield c, set_devices, procs, log
    c.shutdown()


def spawned(procs: Path, n: int, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        files = list(procs.iterdir())
        if len(files) >= n:
            return [json.loads(f.read_text()) for f in files]
        time.sleep(0.05)
    raise AssertionError(f"expected {n} scrcpy processes, saw {len(list(procs.iterdir()))}")


def test_three_phones_three_processes_each_pinned_to_its_serial(rig):
    c, _, procs, _ = rig
    phones = c.scan()
    assert len(phones) == 3
    for p in phones:
        c.add_phone(p)
    argvs = spawned(procs, 3)
    serials = sorted(next(a for a in v if a.startswith("--serial=")) for v in argvs)
    assert serials == ["--serial=A", "--serial=B", "--serial=C"]
    assert all("--no-control" in v for v in argvs)           # everything starts view-only
    assert c.features["ready"]


def test_tap_is_refused_without_control_and_reaches_only_target_with_it(rig):
    c, _, procs, log = rig
    for p in c.scan():
        c.add_phone(p)
    tgt = c.dash.tiles[1].phone.stable_id
    r = c.execute(vv.Action("back", scope="device"), target_key=tgt)
    assert not r.ok and "view-only" in r.message
    assert '"keyevent"' not in log.read_text()               # nothing was sent
    assert c.grant_control(1).ok
    assert c.execute(vv.Action("back", scope="device"), target_key=tgt).ok
    sent = [json.loads(l) for l in log.read_text().splitlines() if '"input"' in l]
    assert len(sent) == 1 and sent[0][:2] == ["-s", "B"] and sent[0][-2:] == ["keyevent", "4"]


def test_grant_respawns_only_that_phone_with_input_and_revoke_returns_view_only(rig):
    c, _, procs, _ = rig
    for p in c.scan():
        c.add_phone(p)
    spawned(procs, 3)
    old_pid = c.procs[1].pid
    c.grant_control(1)
    assert c.procs[1].pid != old_pid
    controlled = []
    for _ in range(100):                                       # wait for the respawned process to record its argv
        vals = [json.loads(f.read_text()) for f in procs.iterdir() if f.read_text()]
        controlled = [v for v in vals if "--serial=B" in v and "--no-control" not in v]
        if controlled:
            break
        time.sleep(0.05)
    assert controlled
    pid_controlled = c.procs[1].pid
    c.revoke_control(1)
    assert c.procs[1].pid != pid_controlled and c.dash.tiles[1].mode == "view"
    with pytest.raises(vd.InputRejected):
        t = c.dash.tiles[1]
        c.dash.authorize_input(t.session, t.epoch)


def test_unplugging_one_phone_pauses_only_its_tile_and_reconnect_is_view_only(rig):
    c, set_devices, procs, _ = rig
    for p in c.scan():
        c.add_phone(p)
    c.grant_control(0); c.grant_control(1)
    set_devices("A", "C")                                      # B unplugged
    notes = c.poll()
    assert any("MB" in n or "B" in n for n in notes)
    assert c.dash.tiles[1].state == "dropped"
    assert c.dash.tiles[0].state == "live" and c.dash.tiles[0].mode == "control" and c.dash.tiles[2].state == "live"
    set_devices("A", "B", "C")
    notes = c.poll()
    assert c.dash.tiles[1].state == "live" and c.dash.tiles[1].mode == "view" and any("back" in n for n in notes)


def test_fourth_phone_offers_choice_and_replace_stops_old_stream(rig):
    c, set_devices, procs, _ = rig
    set_devices("A", "B", "C", "D")
    phones = c.scan()
    assert len(phones) == 4
    for p in phones[:3]:
        c.add_phone(p)
    with pytest.raises(vd.PickReplacement):
        c.add_phone(phones[3])
    assert len(c.dash.tiles) == 3
    victim_pid = c.procs[2].pid
    c.add_phone(phones[3], replace_slot=2)
    assert c.procs[2].pid != victim_pid and c.dash.tiles[2].phone.adb_serial == phones[3].adb_serial
    time.sleep(0.2)
    assert not _alive(victim_pid)                              # the replaced phone's stream was stopped


def _alive(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_usb_and_wifi_same_phone_become_one_tile(rig, tmp_path):
    c, set_devices, procs, _ = rig
    state = json.loads(Path(os.environ["FAKE_STATE"]).read_text())
    state["props"]["10.0.0.5:5555"] = dict(state["props"]["A"])       # same serial/device as A
    Path(os.environ["FAKE_STATE"]).write_text(json.dumps({"devices": ["A", "10.0.0.5:5555", "B"], "props": state["props"]}))
    phones = c.scan()
    assert sorted(p.adb_serial for p in phones) == ["A", "B"]


def test_voice_scroll_goes_to_resolved_phone_only(rig):
    c, _, procs, log = rig
    for p in c.scan():
        c.add_phone(p)
    c.grant_control(2)
    r = vv.resolve_target("phone three", c.keys_for(), None, None)
    assert isinstance(r, vv.Resolved)
    res = c.execute(vv.parse_command("scroll down"), target_key=r.key)
    assert res.ok
    swipes = [json.loads(l) for l in log.read_text().splitlines() if '"swipe"' in l]
    assert len(swipes) == 1 and swipes[0][1] == "C"


def test_release_input_kills_input_capable_processes(rig):
    c, *_ = rig
    for p in c.scan():
        c.add_phone(p)
    c.grant_control(0)
    assert c.execute(vv.parse_command("release input")).ok
    assert all(t.mode == "view" for t in c.dash.tiles.values()) and c.dash.focus_slot is None
