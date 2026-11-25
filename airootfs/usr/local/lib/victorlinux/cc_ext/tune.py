"""Tune tab: one-time llama-bench run that picks GPU layers / threads / context for this model on this PC.

The model must be stopped (two copies would not fit and would skew the numbers). The run is local only.
"""
import os
import subprocess
import threading

import autotune
import hostplan
import victorcommon as tc

JOB = {"state": "idle"}
RUN = subprocess.run          # tests replace this


def _profiles():
    p = tc.data_path("config", "autotune.json")
    if not p:
        raise ValueError("Choose storage first (Storage tab). Profiles are saved on the data partition.")
    return autotune.Profiles(p)


def status(app, q):
    s = tc.load_settings()
    out = {"job": dict(JOB), "apply": bool(s.get("autotune_apply", True)), "model": s.get("model") or None,
           "llm_running": app.llm.running(), "profiles": {}, "current": None}
    if tc.data_path("config"):
        mid = app.machine_id()
        prof = _profiles().for_machine(mid)
        out.update(machine=mid, profiles=prof, current=prof.get(autotune.model_key(s["model"])) if s.get("model") else None)
    return 200, out


def _plan(app, s):
    auto = dict(s, gpu_layers=-1, threads=0)
    plan = app.plan_llm(auto)
    if "error" in plan:
        raise ValueError(plan["error"])
    binary = os.path.join(os.path.dirname(plan["binary"]), "llama-bench")
    if not os.access(binary, os.X_OK):
        raise ValueError(f"llama-bench is not in this image for the {plan['backend']} backend.")
    layers = autotune.layer_options(plan["estimate"].get("n_layer"), plan["ngl"], plan["backend"] != "cpu")
    threads = autotune.thread_options(hostplan.physical_threads(), os.cpu_count())
    return plan, binary, layers, threads


def run(app, b):
    if JOB.get("state") == "running":
        return 400, {"error": "A benchmark is already running."}
    if app.llm.running():
        return 400, {"error": "Stop the model first. The benchmark loads it by itself, and two copies would not fit."}
    s = tc.load_settings()
    try:
        plan, binary, layers, threads = _plan(app, s)
        profiles = _profiles()
    except ValueError as e:
        return 400, {"error": str(e)}
    cmd = autotune.bench_command(binary, plan["model"], layers, threads)
    mid, mkey = app.machine_id(), autotune.model_key(plan["model"])
    JOB.clear()
    JOB.update(state="running", model=os.path.basename(plan["model"]), layers=layers, threads=threads,
               trials=len(layers) * len(threads), backend=plan["backend"])

    def work():
        try:
            env = dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(binary))
            r = RUN(cmd, capture_output=True, text=True, timeout=3600, env=env)
            if r.returncode != 0:
                raise ValueError("llama-bench failed: " + (r.stderr or r.stdout).strip()[-400:])
            results = autotune.parse_bench(r.stdout)
            best = autotune.pick(results)
            prof = profiles.put(mid, mkey, dict(best, ctx=plan["ctx"], backend=plan["backend"], device=plan["device"].get("name"),
                                                trials=[{"ngl": k[0], "threads": k[1], **v} for k, v in sorted(results.items())]))
            JOB.update(state="done", profile=prof)
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            JOB.update(state="error", error=str(e))
    threading.Thread(target=work, daemon=True).start()
    return 200, {"ok": True, "message": f"Benchmark started: {JOB['trials']} combinations. This takes a few minutes."}


def forget(app, b):
    s = tc.load_settings()
    key = None if b.get("all") else autotune.model_key(s.get("model") or "")
    n = _profiles().remove(app.machine_id(), key)
    return 200, {"ok": True, "message": f"Removed {n} saved profile(s) for this PC."}


def set_apply(app, b):
    s = tc.load_settings()
    s["autotune_apply"] = bool(b.get("enabled"))
    tc.save_settings(s)
    return 200, {"ok": True, "message": "Saved profiles will be used at start." if s["autotune_apply"] else "Saved profiles are ignored."}


ROUTES = {("GET", "/api/tune/status"): status, ("POST", "/api/tune/run"): run,
          ("POST", "/api/tune/forget"): forget, ("POST", "/api/tune/apply"): set_apply}
