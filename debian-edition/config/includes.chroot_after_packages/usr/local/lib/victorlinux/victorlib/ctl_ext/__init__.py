"""Optional victor-ctl feature groups. Each module defines NAME, register(subparsers) and handle(args) -> result.
A module that fails to import is skipped (reported on stderr) so the core CLI keeps working."""
import importlib, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    mods = {}
    for fn in sorted(os.listdir(HERE)):
        name = fn[:-3]
        if fn.endswith(".py") and name != "__init__" and re.match(r"^[a-z][a-z0-9_]{0,31}$", name):
            try:
                m = importlib.import_module(f"{__name__}.{name}")
            except Exception as e:  # noqa: BLE001
                print(f"victor-ctl: feature {name} skipped: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            mods[m.NAME] = m
    return mods


def register(sp):
    mods = load()
    for m in mods.values():
        m.register(sp)
    return mods
