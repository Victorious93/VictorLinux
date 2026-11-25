"""Push-to-talk acceptance: real Vosk model + real PulseAudio capture through victorptt.pc_capture_argv +
prerecorded synthetic speech (espeak-ng) played into a virtual sink. No physical microphone is involved, and the
adb runner is a recorder, so no phone receives keys. Work by Victorious.

  VICTOR_STT_MODEL=<dir with an official Vosk model> \\
    dbus-run-session -- tools/acceptance/with_pulse.sh python3 tools/acceptance/ptt_vosk_acceptance.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "airootfs", "usr", "local", "lib", "victorlinux"))
import victormic as vm          # noqa: E402
import victorphonedash as pd    # noqa: E402
import victorptt as vp          # noqa: E402
import victorscreens as vs      # noqa: E402

CASES = [  # phrase, focus at press, input holder, expected adb target or None (refused/asked)
    ("pixel go back", "emulator-5556", "emulator-5554", "emulator-5554"),
    ("show recent apps", "emulator-5558", "emulator-5558", "emulator-5558"),
    ("nord go home", "emulator-5554", "emulator-5556", "emulator-5556"),
    ("tablet go back", "emulator-5554", "", None),            # input released: refused like a click
    ("show all phones", "emulator-5554", "", "none"),          # global action, no adb
]


def main() -> int:
    pc, default = vm.list_pc_sources()
    src = next(p for p in pc if p.name == "vtest_mic")
    settings = vm.normalize({"mode": "pc", "pc_stable_id": src.stable_id})
    reg = vm.SessionRegistry("pc")
    plan_fn = lambda: pd.capture_plan(settings, pc, default, reg, False)
    ok, msg = pd.recognizer_status()
    print("capture:", " ".join(plan_fn().argv))
    print("recognizer:", msg)
    rec = vp.make_recognizer("vosk")
    mgr = vs.TileManager()
    for d, n in (("emulator-5554", "Pixel"), ("emulator-5556", "Nord"), ("emulator-5558", "Tablet")):
        t = mgr.add(d, n); t.state = "live"; t.permissions = "control"
    sent: list = []
    run = lambda argv: (sent.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1]
    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for phrase, focus, holder, want in CASES:
            mgr.set_focus(focus); mgr.release_input()
            if holder:
                mgr.grant_input(holder)
            wav = os.path.join(tmp, "p.wav")
            subprocess.run(["espeak-ng", "-s", "140", "-w", wav, phrase], check=True)
            ctl = pd.PttController(mgr, rec, plan_fn, run=run)
            ctl.press(); time.sleep(0.4)
            subprocess.run(["paplay", "--device=vtest_sink", wav], check=True)
            time.sleep(0.6)
            n = len(sent)
            out = ctl.release()
            target = sent[n][2] if len(sent) > n else ("none" if out.ok else None)
            good = target == want
            passed += good
            print(json.dumps({"phrase": phrase, "focus": focus, "holder": holder or None, "result": out.message,
                              "adb_target": target, "expected": want, "pass": good}))
    print(f"{passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
