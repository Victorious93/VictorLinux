"""victor-ctl coral ...: Coral Edge TPU for vision and wake-word models you provide (never chat models).

Off by default, nothing downloaded, no CPU/cloud fallback. Models go in coral/ on the data partition. Works on the
Raspberry Pi edition too (it ships these tools), where a USB Accelerator is the usual setup.
"""
import json, os, subprocess
from ..common import VictorError, data_path
from .. import coral

NAME = "coral"
POPEN = subprocess.Popen


def register(sp):
    p = sp.add_parser(NAME, help="Coral Edge TPU: vision / wake-word models you provide (cannot run chat models); off by default")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    st = s.add_parser("set"); st.add_argument("assign", nargs="+", metavar="KEY=VALUE",
                                              help="enabled, vision_model, wake_model, wake_label, wake_threshold")
    s.add_parser("classify").add_argument("image")
    w = s.add_parser("wake", help="listen on the default microphone and print a line each time the wake word is heard")
    w.add_argument("--seconds", type=int, default=60)


def _path():
    return data_path("config", "coral.json")


def _files():
    return coral.model_files(data_path("coral"))


def _ready(c, key):
    if not c["enabled"]:
        raise VictorError("Coral is off. Turn it on first: victor-ctl coral set enabled=true")
    if not c[key]:
        raise VictorError(f"Pick a model first: victor-ctl coral set {key}=FILE")
    if not coral.detect():
        raise VictorError("No Coral Edge TPU detected.")


def handle(a):
    try:
        c, files = coral.load(_path()), _files()
        if a.cmd == "status":
            return dict(coral.status(c, files, coral.detect()), folder=data_path("coral"))
        if a.cmd == "set":
            b = {}
            for x in a.assign:
                k, sep, v = x.partition("=")
                if not sep or k not in coral.DEFAULTS:
                    raise VictorError(f"Unknown setting {x} (use {', '.join(coral.DEFAULTS)})")
                if k == "enabled":
                    b[k] = v.lower() in ("1", "true", "yes", "on")
                elif k in ("vision_model", "wake_model") and v and os.sep not in v:
                    b[k] = next((f for f in files if os.path.basename(f) == v), v)
                else:
                    b[k] = v
            n = coral.update(c, b, files)
            coral.save(_path(), n)
            return n
        if a.cmd == "classify":
            _ready(c, "vision_model")
            it = coral.interpreter(c["vision_model"])
            return {"top": coral.classify_image(it, a.image, coral.labels_for(c["vision_model"]))}
        if a.cmd == "wake":
            _ready(c, "wake_model")
            if not c["wake_label"]:
                raise VictorError("Set the wake label first: victor-ctl coral set wake_label=WORD")
            ww = coral.WakeWord(coral.interpreter(c["wake_model"]), coral.labels_for(c["wake_model"]), c["wake_label"], c["wake_threshold"])
            p = POPEN(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-d", str(max(1, min(a.seconds, 3600)))],
                      stdout=subprocess.PIPE)
            hits = 0
            try:
                for chunk in iter(lambda: p.stdout.read(3200), b""):     # 100 ms frames, kept in memory only
                    if ww.feed(chunk):
                        hits += 1
                        print(json.dumps({"wake": c["wake_label"]}), flush=True)
            finally:
                p.kill()
            return {"ok": True, "detections": hits}
    except coral.CoralError as e:
        raise VictorError(str(e))
