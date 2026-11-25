"""victor-ctl switch MODEL: change the loaded model without a reboot.

Debian serves the API straight from llama-server (no gateway in front), so clients see the API as unavailable
(connection refused / 503) for the few seconds to minutes the new model takes to load. If the new model fails to
start or answer, the previous model is started again.
"""
import json, os, time, urllib.request
from ..common import VictorError, data_path
from .. import llama

NAME = "switch"


def register(sp):
    p = sp.add_parser(NAME, help="switch the loaded model without a reboot (rolls back on failure)")
    p.add_argument("model", help="file name in the models folder")
    p.add_argument("--wait", type=int, default=300, help="seconds to wait for the new model to answer")


def _healthy(c, wait):
    scheme = "https" if (c["remote"]["enabled"] and c["remote"]["tls"]) else "http"
    host = "127.0.0.1" if c["bind"] in ("0.0.0.0", "::") else c["bind"]
    url = f"{scheme}://{host}:{c['port']}/health"
    ctx = None
    if scheme == "https":
        import ssl
        ctx = ssl.create_default_context(cafile=llama.tls_paths()[0])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), *( [urllib.request.HTTPSHandler(context=ctx)] if ctx else []))
    end = time.time() + wait
    while time.time() < end:
        if not llama.is_running():
            return False
        try:
            with opener.open(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def handle(a):
    c = llama.load_config()
    if os.path.isabs(a.model) or ".." in a.model.split(os.sep) or not os.path.isfile(os.path.join(data_path("models"), a.model)):
        raise VictorError("No such model in the models folder.")
    old = c["model"]
    if not llama.is_running():
        c["model"] = a.model; llama.save_config(c)
        return {"switched": False, "message": "Selected. Inference was not running; start it to load the model."}
    if old == a.model:
        return {"switched": False, "message": "That model is already loaded."}
    t0 = time.time()
    llama.stop()
    c["model"] = a.model; llama.save_config(c)
    err = None
    try:
        llama.start()
        if not _healthy(c, a.wait):
            err = "the new model did not become ready"
    except VictorError as e:
        err = str(e)
    if err is None:
        return {"switched": True, "model": a.model, "seconds": round(time.time() - t0, 1)}
    llama.stop()
    c["model"] = old; llama.save_config(c)
    try:
        llama.start(); back = _healthy(c, a.wait)
    except VictorError:
        back = False
    raise VictorError(f"Switch to {a.model} failed ({err}). " + ("The previous model is running again." if back else
                      "The previous model did not come back; start inference from the Control Center."))
