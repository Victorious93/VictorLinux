"""Switch the running model without a reboot, while the API gateway stays up.

1. RUN_DIR/swap.json is written; the gateway now HOLDS new API requests (up to 3 minutes) instead of failing them.
2. Only llama-server is stopped; the gateway process and its API keys/TLS stay as they are.
3. The new model starts on the same private port and must produce one token (the normal verification).
4. On success the choice is saved. On any failure the previous model is started again (rollback).
5. swap.json is removed and held requests continue against whichever model is now loaded.
"""
import json
import os
import threading
import time

import victorcommon as tc

SWAP = {"state": "idle"}
VERIFY_WAIT_S = 300


def _marker(st=None):
    p = os.path.join(tc.RUN_DIR, "swap.json")
    if st is None:
        try:
            os.unlink(p)
        except OSError:
            pass
        return
    os.makedirs(tc.RUN_DIR, exist_ok=True)
    tc.atomic_write(p, json.dumps(st), 0o644)


def _wait_verified(app):
    end = time.time() + VERIFY_WAIT_S
    while time.time() < end:
        v = getattr(app, "llm_verify", None) or {}
        if v.get("state") in ("verified", "failed"):
            return v
        if not app.llm.running():
            return {"state": "failed", "error": "llama-server exited while loading."}
        time.sleep(0.5)
    return {"state": "failed", "error": "The model did not answer within 5 minutes."}


def _start(app, settings):
    app.llm_verify = {"state": "waiting"}
    r = app.start_llm(settings, keep_gateway=True)
    if not r.get("ok"):
        return {"state": "failed", "error": r.get("error") or "start failed"}
    return _wait_verified(app)


def _run(app, old, new):
    t0 = time.time()
    try:
        with app.lock:
            app.stop(app.llm)
            app.llm_info = {}
        v = _start(app, new)
        if v["state"] == "verified":
            s = tc.load_settings()
            s["model"] = new["model"]
            tc.save_settings(s)
            SWAP.update(state="done", seconds=round(time.time() - t0, 1), message=f"Now serving {os.path.basename(new['model'])}.")
            return
        err = v.get("error") or "the new model failed"
        with app.lock:
            app.stop(app.llm)
            app.llm_info = {}
        back = _start(app, old)
        SWAP.update(state="error", error=err, rolled_back=back["state"] == "verified", seconds=round(time.time() - t0, 1),
                    message=("Switch failed; the previous model is serving again." if back["state"] == "verified"
                             else "Switch failed and the previous model did not come back. Start a model from the Dashboard."))
    finally:
        _marker(None)


def switch(app, b):
    if SWAP.get("state") == "switching":
        return 400, {"error": "A model switch is already in progress."}
    s = tc.load_settings()
    target = str(b.get("model") or "")
    if target not in [m["path"] for m in tc.list_models(s["extra_model_dirs"])]:
        return 400, {"error": "Model must be inside a configured model directory."}
    if not app.llm.running():
        s["model"] = target
        tc.save_settings(s)
        return 200, {"ok": True, "started": False, "message": "Selected. No model was running, so start it from the Dashboard."}
    if target == s.get("model"):
        return 200, {"ok": True, "started": False, "message": "That model is already loaded."}
    old, new = dict(s), dict(s, model=target)
    SWAP.clear()
    SWAP.update(state="switching", frm=os.path.basename(old["model"] or ""), to=os.path.basename(target), since=time.time())
    _marker({"state": "switching", "from": SWAP["frm"], "to": SWAP["to"], "since": SWAP["since"]})
    threading.Thread(target=_run, args=(app, old, new), daemon=True).start()
    return 200, {"ok": True, "started": True, "message": f"Switching to {SWAP['to']}. API requests are held until it answers."}


def status(app, q):
    s = tc.load_settings()
    return 200, {"swap": dict(SWAP), "running": app.llm.running(), "model": s.get("model"),
                 "models": [{"path": m["path"], "name": m.get("name") or os.path.basename(m["path"]), "size": m.get("size")}
                            for m in tc.list_models(s["extra_model_dirs"])]}


def shutdown(app):
    _marker(None)


ROUTES = {("POST", "/api/llm/switch"): switch, ("GET", "/api/llm/switch"): status}
