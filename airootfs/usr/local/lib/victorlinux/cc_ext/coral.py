"""Coral Edge TPU (Arch): status, model choice and a vision test, with Edge TPU models the owner supplies.

Vision and wake-word only: an Edge TPU cannot run chat models. OFF by default, nothing downloaded, no CPU or cloud
fallback (no TPU = clear error). Admin API stays on 127.0.0.1.
"""
import base64
import os
import tempfile
import threading

import coral
import victorcommon as tc

_LOCK = threading.Lock()


def cfg_path():
    return tc.data_path("config", "coral.json")


def folder():
    return tc.data_path("coral")


def _cfg():
    p = cfg_path()
    return coral.load(p) if p else dict(coral.DEFAULTS)


def state(app, q):
    c = _cfg()
    return 200, {"config": c, "status": coral.status(c, coral.model_files(folder()), coral.detect()), "folder": folder() or "",
                 "storage": bool(cfg_path())}


def configure(app, b):
    if not cfg_path():
        raise coral.CoralError("Set up storage first (Storage tab) so the Coral settings can be saved.")
    n = coral.update(_cfg(), b, coral.model_files(folder()))
    coral.save(cfg_path(), n)
    return 200, {"ok": True, "config": n, "message": "Coral settings saved."}


def classify(app, b):
    c = _cfg()
    if not c["enabled"]:
        raise coral.CoralError("Coral is off. Turn it on in Setup first.")
    if not c["vision_model"]:
        raise coral.CoralError("Pick an Edge TPU vision model first.")
    if not coral.detect():
        raise coral.CoralError("No Coral Edge TPU detected.")
    try:
        data = base64.b64decode(str(b.get("image") or ""), validate=True)
    except ValueError:
        raise coral.CoralError("The picture could not be read.")
    if not data or len(data) > 8 * 2 ** 20:
        raise coral.CoralError("Send a picture smaller than 8 MB.")
    with _LOCK, tempfile.TemporaryDirectory(prefix="victor-coral-") as d:
        p = os.path.join(d, "in.img")
        with open(p, "wb") as f:
            f.write(data)
        it = coral.interpreter(c["vision_model"])
        top = coral.classify_image(it, p, coral.labels_for(c["vision_model"]))
    return 200, {"ok": True, "top": top}


def _wrap(fn):
    def w(app, data):
        try:
            return fn(app, data)
        except coral.CoralError as e:
            return 400, {"error": str(e)}
    return w


ROUTES = {("GET", "/api/coral/state"): state, ("POST", "/api/coral/config"): _wrap(configure),
          ("POST", "/api/coral/classify"): _wrap(classify)}
