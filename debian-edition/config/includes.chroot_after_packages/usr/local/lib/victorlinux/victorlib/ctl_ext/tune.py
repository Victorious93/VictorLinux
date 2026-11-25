"""victor-ctl tune run|status|forget: one-time llama-bench thread benchmark for the selected model on this PC.

Debian runs every layer on the GPU when one is usable (-ngl 999) and keeps the context you set, so only the thread
count is tuned here. The profile is saved per PC and per model in settings/autotune.json and used at start while
threads are on automatic (0). Disable with: victor-ctl llama config --set autotune_apply=0
"""
import os
from ..common import VictorError, LLAMA_BENCH, data_path, settings_file, run
from .. import autotune, hw, llama

NAME = "tune"


def register(sp):
    p = sp.add_parser(NAME, help="benchmark and auto-tune threads for the selected model")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("run"); s.add_parser("status")
    s.add_parser("forget").add_argument("--all", action="store_true")


def _profiles():
    return autotune.Profiles(settings_file("autotune.json"))


def handle(a):
    c = llama.load_config()
    mp = os.path.join(data_path("models"), c["model"]) if c["model"] else None
    mid = hw.machine_id()
    if a.cmd == "status":
        prof = _profiles().for_machine(mid)
        return {"machine": mid, "model": c["model"], "apply": c.get("autotune_apply", True),
                "current": prof.get(autotune.model_key(mp)) if mp else None, "profiles": prof}
    if a.cmd == "forget":
        return {"removed": _profiles().remove(mid, None if a.all else autotune.model_key(mp or ""))}
    if not mp or not os.path.isfile(mp):
        raise VictorError("Select a model first.")
    if llama.is_running():
        raise VictorError("Stop the inference service before benchmarking (two copies would not fit).")
    backend = hw.select_backend(c["backend"], hw.llama_devices()["devices"], None)
    threads = autotune.thread_options(hw.physical_cores(), os.cpu_count())
    layers = [999] if backend["accelerated"] else [0]
    extra = ["--device", backend["device"]] if backend["accelerated"] else ["--device", "none"]
    p = run(autotune.bench_command(LLAMA_BENCH, mp, layers, threads, extra), timeout=3600)
    if p.returncode != 0:
        raise VictorError("llama-bench failed: " + (p.stderr.strip()[-400:] or p.stdout.strip()[-400:]))
    results = autotune.parse_bench(p.stdout)
    try:
        best = autotune.pick(results)
    except ValueError as e:
        raise VictorError(str(e))
    return _profiles().put(mid, autotune.model_key(mp), dict(best, ctx=int(c["ctx"]), accelerated=bool(backend["accelerated"]),
                           device=backend.get("device"), trials=[{"ngl": k[0], "threads": k[1], **v} for k, v in sorted(results.items())]))
