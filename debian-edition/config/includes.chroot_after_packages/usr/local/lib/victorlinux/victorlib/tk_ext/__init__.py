"""Optional Control Center (Tk) tabs. Each module defines build(app) and adds one notebook tab to app.nb.
Modules use the app's helpers (app.bg, app.out, app.mkout, app.ctl), so every action is still a victor-ctl call."""
import importlib, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def build_all(app):
    for fn in sorted(os.listdir(HERE)):
        name = fn[:-3]
        if fn.endswith(".py") and name != "__init__" and re.match(r"^[a-z][a-z0-9_]{0,31}$", name):
            try:
                importlib.import_module(f"{__name__}.{name}").build(app)
            except Exception as e:  # noqa: BLE001 - a broken optional tab must not stop the Control Center
                print(f"victor-control: tab {name} skipped: {type(e).__name__}: {e}", file=sys.stderr)
