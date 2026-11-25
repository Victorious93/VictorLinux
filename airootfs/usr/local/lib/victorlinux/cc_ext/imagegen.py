"""Image generation (Arch): a user-supplied stable-diffusion.cpp program draws a picture from a prompt, on this PC.

OFF by default, no automatic downloads, no cloud fallback. One picture at a time, run in the background; the page polls
/api/imagegen/job. Pictures are saved to images/out on the data partition. Admin API stays on 127.0.0.1.
"""
import base64
import os
import subprocess
import threading

import imagegen
import victorcommon as tc

POPEN = subprocess.Popen       # tests inject a fake
_LOCK = threading.Lock()
JOB = {"state": "idle", "step": 0, "steps": 0, "error": "", "file": "", "proc": None}


def cfg_path():
    return tc.data_path("config", "imagegen.json")


def models_dir():
    return tc.data_path("images", "models")


def out_dir():
    return tc.data_path("images", "out")


def _cfg():
    p = cfg_path()
    return imagegen.load(p) if p else dict(imagegen.DEFAULTS)


def _public_job():
    j = {k: JOB[k] for k in ("state", "step", "steps", "error")}
    j["file"] = os.path.basename(JOB["file"]) if JOB["file"] else ""
    return j


def state(app, q):
    files = imagegen.model_files(models_dir())
    return 200, {"config": _cfg(), "status": imagegen.status(_cfg(), files), "folder": models_dir() or "",
                 "storage": bool(cfg_path()), "job": _public_job()}


def configure(app, b):
    if not cfg_path():
        raise imagegen.ImageGenError("Set up storage first (Storage tab) so the settings and pictures can be saved.")
    n = imagegen.update(_cfg(), b, imagegen.model_files(models_dir()))
    imagegen.save(cfg_path(), n)
    return 200, {"ok": True, "config": n, "message": "Image settings saved."}


def _run(c, job):
    def prog(step, steps):
        JOB.update(step=step, steps=steps)

    def popen(argv, **kw):
        p = POPEN(argv, **kw)
        JOB["proc"] = p
        return p
    try:
        path = imagegen.generate(c, job, out_dir(), popen=popen, on_progress=prog)
        JOB.update(state="done", file=path)
    except imagegen.ImageGenError as e:
        JOB.update(state="cancelled" if JOB["state"] == "cancelling" else "error", error=str(e))
    except Exception as e:  # noqa: BLE001 - surface any failure to the page instead of a stuck "running"
        JOB.update(state="error", error=f"{type(e).__name__}: {e}")
    finally:
        JOB["proc"] = None


def generate(app, b):
    c = _cfg()
    if not out_dir():
        raise imagegen.ImageGenError("Set up storage first (Storage tab) so pictures can be saved.")
    st = imagegen.status(c, imagegen.model_files(models_dir()))
    if not c["enabled"]:
        raise imagegen.ImageGenError("Image generation is off. Turn it on in Setup first.")
    if not st["ready"]:
        raise imagegen.ImageGenError("Not ready: " + "; ".join(st["missing"]))
    job = imagegen.validate_job(b)
    with _LOCK:
        if JOB["state"] in ("running", "cancelling"):
            raise imagegen.ImageGenError("A picture is already being drawn. Wait or cancel it.")
        JOB.update(state="running", step=0, steps=job["steps"], error="", file="")
    t = threading.Thread(target=_run, args=(c, job), daemon=True)
    t.start()
    return 200, {"ok": True, "job": _public_job()}


def job(app, q):
    j = _public_job()
    if JOB["state"] == "done" and JOB["file"] and os.path.isfile(JOB["file"]) and os.path.getsize(JOB["file"]) <= 16 * 2 ** 20:
        with open(JOB["file"], "rb") as f:
            j["png"] = base64.b64encode(f.read()).decode()
    return 200, j


def cancel(app, b):
    p = JOB.get("proc")
    if JOB["state"] != "running" or p is None:
        return 200, {"ok": True, "message": "Nothing is running."}
    JOB["state"] = "cancelling"
    try:
        p.terminate()
    except OSError:
        pass
    return 200, {"ok": True, "message": "Cancelling."}


def shutdown(app):
    p = JOB.get("proc")
    if p is not None:
        try:
            p.kill()
        except OSError:
            pass


def _wrap(fn):
    def w(app, data):
        try:
            return fn(app, data)
        except imagegen.ImageGenError as e:
            return 400, {"error": str(e)}
    return w


ROUTES = {("GET", "/api/imagegen/state"): state, ("GET", "/api/imagegen/job"): job,
          ("POST", "/api/imagegen/config"): _wrap(configure), ("POST", "/api/imagegen/generate"): _wrap(generate),
          ("POST", "/api/imagegen/cancel"): _wrap(cancel)}
