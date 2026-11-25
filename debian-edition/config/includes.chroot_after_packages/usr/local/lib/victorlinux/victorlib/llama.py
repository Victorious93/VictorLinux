"""llama.cpp service control (transient user unit; single instance; no tools/MCP flags ever passed)."""
import ipaddress, os, secrets, shlex, json
from .common import *
from . import hw, models

UNIT = "victor-llama"
DEFAULTS = {"model": "", "backend": "auto", "ctx": 4096, "mem_limit_gb": 0.0, "port": 8080, "bind": "127.0.0.1",
            "parallel": 1, "cache_ram_mb": "auto", "sleep_idle_minutes": 15, "jinja": False, "threads": 0, "autotune_apply": True,
            "remote": {"enabled": False, "tls": True, "vpn_plain_http_ack": False}}
FORBIDDEN_FLAGS = ("--tools", "--webui-mcp-proxy", "--ui-mcp-proxy", "--mcp-servers", "--models-dir")


def cfg_path():
    return settings_file("llama.json")


def load_config():
    c = json.loads(json.dumps(DEFAULTS))
    saved = read_json(cfg_path(), {}) if os.path.isdir(ROOT_LINK) else {}
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


def validate(c):
    errs = []
    if c["backend"] not in ("auto", "vulkan", "cpu"):
        errs.append("backend must be auto, vulkan or cpu")
    if not (256 <= int(c["ctx"]) <= 1_048_576):
        errs.append("ctx must be between 256 and 1048576")
    if not (1024 <= int(c["port"]) <= 65535):
        errs.append("port must be 1024-65535")
    if int(c["parallel"]) < 1 or int(c["parallel"]) > 8:
        errs.append("parallel must be 1-8")
    cr = c.get("cache_ram_mb", "auto")
    if cr != "auto" and not (isinstance(cr, int) and not isinstance(cr, bool) and 0 <= cr <= 65536):
        errs.append("cache_ram_mb must be 'auto' or an integer 0-65536 MiB (0 = no prompt cache beyond the live slots)")
    if c["mem_limit_gb"] < 0:
        errs.append("mem_limit_gb must be >= 0 (0 = automatic)")
    try:
        ip = ipaddress.ip_address(c["bind"])
    except ValueError:
        errs.append(f"bind '{c['bind']}' is not an IP address")
        ip = None
    r = c["remote"]
    if ip is not None and not ip.is_loopback:
        if not r["enabled"]:
            errs.append("bind is not loopback but remote access is not enabled; enable it explicitly in Remote Access")
        elif not r["tls"] and not r["vpn_plain_http_ack"]:
            errs.append("remote access without TLS needs the explicit VPN-only acknowledgement (vpn_plain_http_ack)")
    if r["enabled"] and ip is not None and ip.is_loopback:
        errs.append("remote access is enabled but bind is loopback; choose the LAN/VPN address to listen on")
    return errs


def apply_set(c, assignments):
    """Apply KEY=VALUE / dotted.KEY=VALUE strings to a config dict (types follow the existing value). Unknown keys are errors."""
    for kv in assignments:
        if "=" not in kv:
            raise VictorError(f"bad setting '{kv}' (expected KEY=VALUE)")
        k, v = kv.split("=", 1)
        path = k.split(".")
        t = c
        for part in path[:-1]:
            if not isinstance(t.get(part), dict):
                raise VictorError(f"unknown setting '{k}'")
            t = t[part]
        if path[-1] not in t:
            raise VictorError(f"unknown setting '{k}'")
        prev = t[path[-1]]
        try:
            t[path[-1]] = (v.lower() in ("1", "true", "yes", "on")) if isinstance(prev, bool) else type(prev)(v)
        except ValueError:
            raise VictorError(f"setting '{k}' expects {type(prev).__name__}, got '{v}'")
    return c


def save_config(c):
    errs = validate(c)
    if errs:
        raise VictorError("; ".join(errs))
    write_json_atomic(cfg_path(), c)


def key_path():
    return data_path("secrets", "api.key")


def ensure_key(rotate=False):
    p = key_path()
    if rotate or not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return p


def tls_paths():
    d = data_path("secrets", "tls")
    return f"{d}/cert.pem", f"{d}/key.pem"


def ensure_tls(bind):
    cert, key = tls_paths()
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    san = f"subjectAltName=DNS:victorlinux,DNS:localhost,IP:127.0.0.1,IP:{bind}"
    run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", key, "-out", cert, "-days", "825", "-subj", "/CN=victorlinux", "-addext", san],
        timeout=30, check=True)
    os.chmod(key, 0o600)
    return cert, key


def cert_fingerprint():
    cert, _ = tls_paths()
    p = run(["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"])
    return p.stdout.strip().split("=", 1)[-1] if p.returncode == 0 else None


def is_running():
    return run(["systemctl", "--user", "is-active", UNIT]).stdout.strip() == "active"


def resolve_cache_ram_mb(c, model_bytes, mem_available_bytes):
    """llama.cpp's own default is an 8192 MiB host-RAM prompt cache regardless of the machine. Bound it by what is actually free:
    auto = min(8192, a quarter of the RAM left after the model and 2 GiB of headroom); an explicit number is honoured (0 disables)."""
    v = c.get("cache_ram_mb", "auto")
    if v != "auto":
        return int(v)
    if not mem_available_bytes:
        return 1024                       # RAM unknown: stay small instead of inheriting the 8 GiB default
    spare = mem_available_bytes - (model_bytes or 0) - (2 << 30)
    return int(max(0, min(8192, spare // 4 // (1 << 20))))


def build_command(c, backend, model_path, key_file=None, tls=None, cache_ram_mb=None):
    """Pure: returns llama-server argv. Never includes tool/MCP flags."""
    a = [LLAMA_SERVER, "-m", model_path, "-c", str(int(c["ctx"])), "--host", c["bind"], "--port", str(int(c["port"])),
         "-np", str(int(c["parallel"])), "--no-warmup"]
    if c.get("threads"):
        a += ["-t", str(int(c["threads"]))]
    if cache_ram_mb is not None:
        a += ["--cache-ram", str(int(cache_ram_mb))]
    if backend["accelerated"]:
        a += ["--device", backend["device"], "-ngl", "999"]
    else:
        a += ["--device", "none", "-ngl", "0"]
    if c["sleep_idle_minutes"] and int(c["sleep_idle_minutes"]) > 0:
        a += ["--sleep-idle-seconds", str(int(c["sleep_idle_minutes"]) * 60)]
    if c["jinja"]:
        a += ["--jinja"]
    if key_file:
        a += ["--api-key-file", key_file]
    if tls:
        a += ["--ssl-cert-file", tls[0], "--ssl-key-file", tls[1]]
    for bad in FORBIDDEN_FLAGS:
        assert bad not in a
    return a


def preflight(c, backend, size):
    mem = mem_info()
    limit = int(c["mem_limit_gb"] * 2**30) or int((mem["available"] or 0) * 0.85)
    need = models.estimate(size, int(c["ctx"]))["estimated_total"]
    warn = []
    if backend["accelerated"]:
        # weights live in VRAM; host RAM still needs headroom for mapping/staging
        vram_free = next((d["free_mib"] for d in hw.llama_devices()["devices"] if d["id"] == backend["device"]), None)
        if vram_free is not None and need > vram_free * 2**20:
            warn.append(f"Model (~{human(need)}) exceeds free VRAM ({vram_free} MiB): llama.cpp will offload fewer "
                        "layers or fail. Lower context, pick a smaller quantisation, or select CPU.")
    else:
        if need > limit:
            raise VictorError(f"Not enough memory for CPU inference: need ~{human(need)}, allowed {human(limit)} "
                              f"(available {human(mem['available'])}). Choose a smaller model/context or raise the limit.")
    return limit, warn


LLM_DISABLED_FLAG = os.path.join(RUN, "llm-disabled")   # written at boot by boards that act only as nodes (Pi edition)


def llm_disabled_reason():
    try:
        return open(LLM_DISABLED_FLAG).read().strip() or "LLM serving is disabled on this device."
    except OSError:
        return None


def tuned_threads(c, backend, model_path):
    """Thread count from `victor-ctl tune run` for this PC + model + backend, or None."""
    if int(c.get("threads") or 0) or not c.get("autotune_apply", True):
        return None
    from . import autotune
    prof = autotune.Profiles(settings_file("autotune.json")).get(hw.machine_id(), autotune.model_key(model_path))
    if not prof or prof.get("accelerated") != bool(backend["accelerated"]):
        return None
    return int(prof["threads"])


def start():
    why = llm_disabled_reason()
    if why:
        raise VictorError(why)
    c = load_config()
    errs = validate(c)
    if errs:
        raise VictorError("Invalid settings: " + "; ".join(errs))
    if not c["model"]:
        raise VictorError("No model selected. Import a GGUF model, then select it in Models.")
    mp = os.path.join(data_path("models"), c["model"])
    if not os.path.isfile(mp):
        raise VictorError(f"Selected model {c['model']} is missing from the data partition.")
    info = models.inspect_gguf(mp)
    if is_running():
        raise VictorError("The inference service is already running (only one model is loaded at a time). Stop it first.")
    snap = hw.llama_devices()
    backend = hw.select_backend(c["backend"], snap["devices"], None)
    if c["backend"] == "vulkan" and not backend["accelerated"]:
        raise VictorError("Vulkan was requested but no hardware Vulkan GPU is usable: " + backend["reason"])
    limit, warn = preflight(c, backend, info["size"])
    tuned = tuned_threads(c, backend, mp)
    if tuned:                       # this PC's saved benchmark; only when threads are on automatic (0)
        c = dict(c, threads=tuned)
    ip = ipaddress.ip_address(c["bind"])
    remote = not ip.is_loopback
    key_file = ensure_key() if remote else None
    tls = ensure_tls(c["bind"]) if (remote and c["remote"]["tls"]) else None
    cache_mb = resolve_cache_ram_mb(c, info["size"], mem_info().get("available"))
    argv = build_command(c, backend, mp, key_file, tls, cache_ram_mb=cache_mb)
    props = ["-p", f"MemoryMax={limit}", "-p", "MemorySwapMax=0", "-p", "NoNewPrivileges=yes", "-p", "PrivateTmp=yes",
             "-p", "ProtectSystem=strict", "-p", f"ReadWritePaths={RUN}"]
    r = run(["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet", *props, "--", *argv], timeout=30)
    if r.returncode != 0:
        raise VictorError("Could not start the service: " + (r.stderr.strip() or r.stdout.strip()))
    write_json_atomic(f"{RUN}/llama.runtime.json", {"backend": backend, "model": c["model"], "bind": c["bind"],
                      "port": c["port"], "tls": bool(tls), "memory_limit": limit, "warnings": warn}, 0o644)
    return {"started": True, "backend": backend, "warnings": warn, "argv_redacted": [shlex.quote(x) for x in argv]}


def stop():
    r = run(["systemctl", "--user", "stop", UNIT])
    if r.returncode not in (0, 5):
        raise VictorError(r.stderr.strip())
    return {"stopped": True}


def status():
    st = {"running": is_running(), "runtime": read_json(f"{RUN}/llama.runtime.json", None)}
    if st["running"]:
        p = run(["systemctl", "--user", "show", UNIT, "-p", "MemoryCurrent", "-p", "MainPID"])
        st["systemd"] = dict(l.split("=", 1) for l in p.stdout.split() if "=" in l)
    return st
