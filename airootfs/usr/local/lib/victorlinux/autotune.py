"""One-time benchmark that picks GPU layers, threads and context for one model on one PC (both editions).

* The trial grid is small and bounded: up to 3 GPU-layer values x up to 3 thread counts, run by llama-bench in ONE
  call (`-ngl a,b,c -t x,y,z -p 256 -n 64 -r 2 -o json`). Typical runtime is a few minutes.
* Winner = fastest generation (tokens/s), ties broken by prompt speed, then by fewer GPU layers (leaves VRAM free).
* The context is NOT a speed question: it is the largest size that still fits in memory with the chosen GPU layers,
  computed by the caller's memory estimate and capped by the model's training context.
* Profiles are saved per machine (hash of CPU model, GPU PCI ids and RAM size) and per model (file name + size),
  so a stick moved to another PC never applies the wrong numbers. Nothing leaves the PC.
"""
import hashlib
import json
import os
import time

VERSION = 1
BENCH_ARGS = ["-p", "256", "-n", "64", "-r", "2", "-o", "json"]


def machine_id(cpu_model, gpu_ids, ram_total):
    ram_gib = int(round((ram_total or 0) / 2 ** 30))
    raw = json.dumps([str(cpu_model or "").strip(), sorted(str(g) for g in (gpu_ids or [])), ram_gib])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def model_key(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return f"{os.path.basename(path)}:{size}"


def thread_options(physical, logical):
    physical, logical = max(1, int(physical or 1)), max(1, int(logical or physical or 1))
    opts = {physical, max(1, physical // 2), logical}
    return sorted(opts)[-3:]


def layer_options(total_layers, planned, gpu):
    """GPU-layer values to try. CPU-only -> [0]. Otherwise the planned value, a quarter fewer, and all layers when the
    plan already offloads (nearly) everything. Never more than the model has (+1 for the output layer)."""
    if not gpu or not total_layers:
        return [0]
    full = int(total_layers) + 1
    planned = max(0, min(int(planned if planned is not None and planned >= 0 else full), full))
    opts = {planned, max(0, planned - max(1, full // 4))}
    if planned >= full - 2:
        opts.add(full)
    return sorted(opts)[-3:]


def bench_command(binary, model, layers, threads, extra=()):
    return [binary, "-m", model, "-ngl", ",".join(map(str, layers)), "-t", ",".join(map(str, threads)), *BENCH_ARGS, *extra]


def parse_bench(stdout):
    """llama-bench JSON rows -> {(ngl, threads): {"pp": t/s, "tg": t/s}}."""
    rows = json.loads(stdout or "[]")
    out = {}
    for r in rows:
        k = (int(r.get("n_gpu_layers", 0)), int(r.get("n_threads", 0)))
        d = out.setdefault(k, {"pp": 0.0, "tg": 0.0})
        if int(r.get("n_gen", 0)) > 0:
            d["tg"] = round(float(r.get("avg_ts", 0)), 2)
        elif int(r.get("n_prompt", 0)) > 0:
            d["pp"] = round(float(r.get("avg_ts", 0)), 2)
    return out


def pick(results):
    if not results:
        raise ValueError("The benchmark produced no results.")
    (ngl, threads), speed = max(results.items(), key=lambda kv: (kv[1]["tg"], kv[1]["pp"], -kv[0][0]))
    if speed["tg"] <= 0:
        raise ValueError("The benchmark ran but measured no generation speed.")
    return {"ngl": ngl, "threads": threads, "tg": speed["tg"], "pp": speed["pp"]}


class Profiles:
    """{machine_id: {model_key: profile}} in one JSON file on the data partition (0600, atomic writes)."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) and d.get("v") == VERSION else {"v": VERSION, "machines": {}}
        except (OSError, ValueError):
            return {"v": VERSION, "machines": {}}

    def _save(self, d):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def get(self, mid, mkey):
        return self.load()["machines"].get(mid, {}).get(mkey)

    def put(self, mid, mkey, profile):
        d = self.load()
        d["machines"].setdefault(mid, {})[mkey] = dict(profile, measured_at=int(time.time()))
        self._save(d)
        return d["machines"][mid][mkey]

    def remove(self, mid, mkey=None):
        d = self.load()
        m = d["machines"].get(mid, {})
        n = 0
        if mkey is None:
            n = len(m)
            d["machines"].pop(mid, None)
        elif mkey in m:
            m.pop(mkey)
            n = 1
        self._save(d)
        return n

    def for_machine(self, mid):
        return self.load()["machines"].get(mid, {})


def apply(profile, settings_ngl, settings_threads, ctx_auto, planned_ctx):
    """Overrides for one start, used only where the owner left the automatic value:
    GPU layers -1 (auto), threads 0 (auto), context auto. The context is never raised above what fits now."""
    if not profile:
        return {}
    o = {}
    if int(settings_ngl) < 0:
        o["ngl"] = int(profile["ngl"])
    if not int(settings_threads or 0):
        o["threads"] = int(profile["threads"])
    if ctx_auto and profile.get("ctx"):
        o["ctx"] = min(int(profile["ctx"]), int(planned_ctx))
    return o
