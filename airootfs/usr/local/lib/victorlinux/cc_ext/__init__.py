"""Optional Control Center feature modules.

A module here (e.g. cc_ext/chats.py) may define:
* ROUTES: {("GET"|"POST", "/api/..."): fn(app, data) -> (status, json_body)}; data is the parsed query (GET) or JSON body (POST).
* shutdown(app): called when the Control Center exits (stop child processes, drop keys from memory).
Routes inherit every Control Center guard: 127.0.0.1 listener, Host-header check, per-session token.
A module that fails to import is logged and skipped. Screens live in web/ext/<name>.html (+ .js).
"""
import importlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web", "ext")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MODULES, ROUTES, ERRORS = {}, {}, {}


def load():
    MODULES.clear(); ROUTES.clear(); ERRORS.clear()
    for fn in sorted(os.listdir(HERE)):
        name = fn[:-3]
        if not fn.endswith(".py") or name == "__init__" or not NAME_RE.match(name):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception as e:  # noqa: BLE001 - one broken feature must not take the Control Center down
            ERRORS[name] = f"{type(e).__name__}: {e}"
            print(f"cc_ext: skipped {name}: {ERRORS[name]}", file=sys.stderr, flush=True)
            continue
        MODULES[name] = mod
        for key, fnc in getattr(mod, "ROUTES", {}).items():
            if key in ROUTES:
                raise RuntimeError(f"route {key} defined twice")
            ROUTES[key] = fnc
    return MODULES


def dispatch(app, method, path, data):
    fnc = ROUTES.get((method, path))
    return None if fnc is None else fnc(app, data)


def shutdown(app):
    for mod in MODULES.values():
        try:
            getattr(mod, "shutdown", lambda a: None)(app)
        except Exception:  # noqa: BLE001
            pass


def web_parts():
    """(html of every web/ext/<name>.html, [script names]) - injected into the single page."""
    html, scripts = [], []
    try:
        names = sorted(os.listdir(WEB))
    except OSError:
        return "", []
    for fn in names:
        if fn.endswith(".html") and NAME_RE.match(fn[:-5]):
            with open(os.path.join(WEB, fn), encoding="utf-8") as f:
                html.append(f.read())
            if os.path.exists(os.path.join(WEB, fn[:-5] + ".js")):
                scripts.append(fn[:-5] + ".js")
    return "\n".join(html), scripts


def read_js(name):
    if not (name.endswith(".js") and NAME_RE.match(name[:-3])):
        return None
    p = os.path.join(WEB, name)
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return f.read()
