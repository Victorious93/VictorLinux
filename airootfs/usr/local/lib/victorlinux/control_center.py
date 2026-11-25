#!/usr/bin/env python3
"""VictorLinux Control Center: local web UI + JSON API for managing AI services.

Listens on 127.0.0.1 only. Every API call needs the per-session token written
to ~/.cache/victorlinux/cc.token (0600); the Host header is checked to defeat DNS
rebinding. Standard library only. Runs as the ordinary user 'victor'; the few
privileged operations go through the restricted helper `victor-priv` via sudo.
"""
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets as pysecrets
import shlex
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import victorcommon as tc  # noqa: E402
import model_advisor  # noqa: E402
import hostplan  # noqa: E402
import apikeys  # noqa: E402
import qrsvg  # noqa: E402
import companion  # noqa: E402
import diskhealth  # noqa: E402
import vbackup  # noqa: E402
import docchat  # noqa: E402
import pccheck  # noqa: E402
import modellib  # noqa: E402
import victorinput  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PRIV = os.environ.get("VICTOR_PRIV", "/usr/local/lib/victorlinux/victor-priv")
CC_PORT = int(os.environ.get("VICTOR_CC_PORT", "8787"))
UPSTREAM_PORT = 18081
EMBED_PORT = 18082          # optional second llama-server with --embeddings, loopback only
HOME = os.path.expanduser("~")
TOKEN_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "victorlinux", "cc.token")

# Optional image-generation / chat packs. Shown only if installed in the image.
SERVICES = {
    "comfyui": {"label": "ComfyUI", "icon": "image", "dir": "ComfyUI", "port": 8188, "vram_hint_gib": 6,
                "cmd": ["venv/bin/python", "main.py", "--listen", "127.0.0.1", "--port", "8188"], "url": "http://127.0.0.1:8188/"},
    "automatic": {"label": "SD.Next (automatic)", "icon": "image", "dir": "automatic", "port": 7860, "vram_hint_gib": 6,
                  "cmd": ["venv/bin/python", "launch.py", "--listen=false", "--port", "7860", "--skip-all"], "url": "http://127.0.0.1:7860/"},
    "sillytavern": {"label": "SillyTavern", "icon": "chat", "bin": "sillytavern", "port": 8000, "vram_hint_gib": 0,
                    "cmd": ["sillytavern"], "url": "http://127.0.0.1:8000/"},
}


def collect_pc_facts(speed=None):
    """Facts for pccheck.evaluate(), read from this PC only (sysfs, /proc, vulkaninfo, nvidia-smi)."""
    cpu = model_advisor.cpu_info()
    try:
        with open("/proc/cpuinfo") as f:
            cpu["flags"] = sorted(pccheck.cpu_flags(f.read()))
    except OSError:
        cpu["flags"] = []
    gl = tc.gpus()
    vk = tc.vulkan_devices()
    be, reason = tc.choose_backend(tc.load_settings()["backend"], gl, vk)
    st = tc.persist_state()
    free = None
    root = tc.data_root()
    if root and os.path.isdir(root):
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            pass
    return {"cpu": cpu, "ram": tc.ram_info(), "gpus": gl, "render_nodes": tc.render_nodes(), "vulkan": vk,
            "backends": {k: bool(v) for k, v in tc.backends().items()}, "selected": {"backend": be, "reason": reason},
            "storage": {"mode": st.get("mode"), "free": free}, "speed": speed, "checked": tc.now()}


class Proc:
    def __init__(self, name):
        self.name, self.p, self.started, self.log = name, None, None, None

    def running(self):
        return self.p is not None and self.p.poll() is None


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.llm = Proc("llama")
        self.gw = Proc("gateway")
        self.svc = {k: Proc(k) for k in SERVICES}
        self.import_job = {"state": "idle"}
        self.health_cache = {"t": 0.0, "r": None}


        self.emb = Proc("embed")
        self.doc_job = docchat.IndexJob()
        self.hash_job = None
        self.llm_info = {}
        self.token = pysecrets.token_urlsafe(32)
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.token)

    # ---------------------------------------------------------- logging --
    def logdir(self):
        d = tc.data_path("logs") or os.path.join(tc.RUN_DIR, "logs")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = "/tmp/victor-logs"
            os.makedirs(d, exist_ok=True)
        return d

    def logpath(self, name):
        return os.path.join(self.logdir(), f"{name}.log")

    def spawn(self, proc, cmd, cwd=None, env=None):
        lp = self.logpath(proc.name)
        if os.path.exists(lp):
            try:
                os.replace(lp, lp + ".1")
            except OSError:
                pass
        logf = open(lp, "ab", buffering=0)
        logf.write(f"# {tc.now()} start: {' '.join(a if 'key' not in prev.lower() else '***' for prev, a in zip(['']+cmd, cmd))}\n".encode())
        e = dict(os.environ)
        e.update(env or {})
        proc.p = subprocess.Popen(cmd, cwd=cwd, env=e, stdout=logf, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        proc.started = time.time()
        proc.log = lp

    def stop(self, proc, timeout=10):
        if proc.running():
            try:
                os.killpg(proc.p.pid, signal.SIGTERM)
                proc.p.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.p.pid, signal.SIGKILL)
                proc.p.wait(5)
            except ProcessLookupError:
                pass

    FLEET_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
    FLEET_ACTIONS = {"start": 0, "stop": 0, "pair": 0, "pause": 1, "resume": 1, "enable": 1, "disable": 1, "revoke": 1,
                     "approve": 1, "cancel": 1, "review": 1, "report": 1}

    def fleet(self, args, timeout=30):
        """Thin wrapper over the victor-fleet CLI (single coordinator authority). No shell; args are validated by the caller."""
        try:
            r = subprocess.run([os.environ.get("VICTOR_FLEET_BIN", "/usr/local/bin/victor-fleet"), *args], capture_output=True, text=True, timeout=timeout,
                               env=dict(os.environ, PYTHONPATH=os.environ.get("VICTOR_FLEET_LIB", "/usr/local/lib/victorfleet")))
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"fleet CLI unavailable: {e}"}
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or f"exit {r.returncode}"}
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"message": r.stdout.strip()}

    FLEET_DRAFT_FIELDS = {"to", "cc", "bcc"}

    def fleet_terminal(self, b):
        """Open an interactive phone terminal in a local terminal window. Always `victor-fleet terminal ID`: the phone's owner still approves."""
        dev = b.get("device")
        if not (isinstance(dev, str) and self.FLEET_ID.match(dev)):
            return 400, {"error": "a valid device id is required"}
        emu = next((e for e in ("xfce4-terminal", "x-terminal-emulator", "xterm", "foot", "alacritty") if shutil.which(e)), None)
        cmdline = [os.environ.get("VICTOR_FLEET_BIN", "/usr/local/bin/victor-fleet"), "terminal", dev]
        if b.get("privileged") is True:
            cmdline.append("--privileged")
        if not emu:
            return 200, {"message": "No terminal emulator found; run in a terminal:  " + " ".join(cmdline)}
        try:
            subprocess.Popen([emu, "-e", " ".join(shlex.quote(x) for x in cmdline)] if emu == "xfce4-terminal" else [emu, "-e", *cmdline],
                             env=dict(os.environ, PYTHONPATH=os.environ.get("VICTOR_FLEET_LIB", "/usr/local/lib/victorfleet")), start_new_session=True)
        except OSError as e:
            return 400, {"error": f"could not open a terminal window: {e}"}
        return 200, {"message": "Terminal opened. Approve the request on the phone."}

    def fleet_draft(self, b):
        """Hand an SMS/email draft to VictorSMS on a phone. The phone's owner reviews it and alone decides to send. Fields are mapped
        to CLI arguments one by one (no shell); mail credentials never exist here."""
        dev, ch = b.get("device"), b.get("channel")
        if not (isinstance(dev, str) and self.FLEET_ID.match(dev)) or ch not in ("sms", "email"):
            return 400, {"error": "device and channel (sms|email) are required"}
        args = ["message", "draft-" + ch, dev]
        for key in ("to", "cc", "bcc"):
            vals = b.get(key) or []
            if not isinstance(vals, list) or any(not isinstance(v, str) or not v or len(v) > 320 or v.startswith("-") for v in vals):
                return 400, {"error": f"'{key}' must be a list of addresses"}
            if key != "to" and ch == "sms" and vals:
                return 400, {"error": "SMS has no cc/bcc"}
            for v in vals:
                args += ["--" + key, v]
        if not b.get("to"):
            return 400, {"error": "at least one recipient is required"}
        body = b.get("body")
        if not isinstance(body, str) or len(body) > 100_000:
            return 400, {"error": "body must be text"}
        args += ["--body", body]
        if ch == "email":
            subj = b.get("subject", "")
            if not isinstance(subj, str) or "\n" in subj or "\r" in subj or len(subj) > 998:
                return 400, {"error": "subject must be a single line"}
            args += ["--subject", subj]
        r = self.fleet(args, timeout=60)
        return (400 if isinstance(r, dict) and r.get("error") else 200), r

    def fleet_action(self, b):
        cmd, arg = b.get("cmd"), b.get("arg")
        want = self.FLEET_ACTIONS.get(cmd)
        if want is None:
            return 400, {"error": "unknown fleet action"}
        if want and not (isinstance(arg, str) and self.FLEET_ID.match(arg)):
            return 400, {"error": "a valid device/plan id is required"}
        r = self.fleet([cmd] + ([arg] if want else []), timeout=60)
        return (400 if isinstance(r, dict) and r.get("error") else 200), r

    def tail(self, name, n=200):
        p = self.logpath(name)
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])
        except OSError:
            return ""

    # ------------------------------------------------------------- state --
    def ensure_upstream_key(self):
        s = tc.load_secrets()
        if not s.get("upstream_key"):
            s["upstream_key"] = pysecrets.token_urlsafe(32)
            tc.save_secrets(s)
        return s["upstream_key"]

    # ---------------------------------------------------- storage health --
    def storage_health(self, refresh=False):
        """Free space on every place VictorLinux writes, plus read-only SMART for the disks behind them (cached 10 min)."""
        if not refresh and self.health_cache["r"] and time.time() - self.health_cache["t"] < 600:
            return self.health_cache["r"]
        root = tc.data_root()
        paths = [("VictorLinux data", root)] if root else []
        paths += [("Models folder", d) for d in tc.load_settings().get("extra_model_dirs", [])]
        space = diskhealth.free_space(paths)
        disks = []
        for item in space:
            r = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--target", item["path"]], capture_output=True, text=True, timeout=10)
            disk = diskhealth.disk_of(r.stdout.strip().split("[")[0]) if r.returncode == 0 else None
            item["disk"] = disk
            if disk and disk not in disks:
                disks.append(disk)
        smart = []
        for d in disks:
            j = self.priv_json(["smart", d])
            smart.append(diskhealth.parse_smart(j) if "error" not in j else
                         {"device": d, "model": "", "status": "unknown", "findings": [{"level": "unknown", "text": "SMART check unavailable: " + j["error"][:160],
                                                                                    "fix": "Run victor-priv smart as root or install smartmontools."}]})
        rep = diskhealth.report(space, smart)
        rep["checked"] = tc.now()
        if not root:
            rep["note"] = "No storage selected yet: choose a drive in Storage to check its space and health."
        self.health_cache = {"t": time.time(), "r": rep}
        return rep

    # ----------------------------------------------------------- backups --
    BACKUP_ITEMS = ["config", "chats", "docs/config.json", "reports"]
    BACKUP_SECRETS = ["config/secrets.json", "server"]

    def backup_dir(self):
        return tc.data_path("backups")

    def backup_file(self, b):
        """A backup in the backups folder (by name) or a .vlbak file elsewhere (e.g. a USB stick)."""
        name, path = b.get("name"), b.get("path")
        if isinstance(name, str) and vbackup.NAME_RE.match(name):
            return os.path.join(self.backup_dir(), name)
        if isinstance(path, str) and path.endswith(vbackup.SUFFIX):
            p = os.path.realpath(os.path.expanduser(path))
            if os.path.isfile(p) and os.path.getsize(p) < 3 * 1024 ** 3:
                return p
        raise vbackup.BackupError("Choose a backup from the list, or enter the full path of a .vlbak file.")

    def backups_action(self, b):
        root = tc.data_root()
        if not root:
            return 400, {"error": "Choose storage first."}
        act = b.get("action")
        allowed = self.BACKUP_ITEMS + self.BACKUP_SECRETS
        try:
            if act == "create":
                if b.get("passphrase") != b.get("confirm_passphrase"):
                    return 400, {"error": "The two passphrases do not match."}
                r = vbackup.create(root, self.backup_dir(), b.get("passphrase"), self.BACKUP_ITEMS, self.BACKUP_SECRETS,
                                   include_secrets=b.get("include_secrets") is True)
                r["message"] = f"Encrypted backup saved: {os.path.basename(r['path'])} ({r['files']} files" + \
                               (", includes API keys and TLS keys)" if r["includes_secrets"] else ", no secrets)")
                return 200, r
            if act == "inspect":
                return 200, vbackup.inspect(self.backup_file(b), b.get("passphrase") or "", allowed)
            if act == "restore":
                r = vbackup.restore(self.backup_file(b), b.get("passphrase") or "", root, allowed, self.backup_dir(), confirm=b.get("confirm") is True)
                r["message"] = f"Restored {r['restored']} files." + (f" Previous versions saved in {r['previous_dir']}." if r["previous_dir"] else "") + \
                               " Restart the model to use restored settings."
                return 200, r
        except vbackup.BackupError as e:
            return 400, {"error": str(e)}
        return 400, {"error": "unknown backup action"}


    # ------------------------------------------------------------- docs --
    def doc_index(self):
        p = tc.data_path("docs", "index.sqlite")
        return docchat.Index(p) if p else None

    def doc_config(self):
        try:
            with open(tc.data_path("docs", "config.json")) as f:
                c = json.load(f)
            return c if isinstance(c, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def doc_private_paths(self):
        """Never indexed: VictorLinux settings/secrets/TLS/state, the session token, SSH and GPG keys."""
        out = [os.path.dirname(TOKEN_FILE), os.path.join(HOME, ".ssh"), os.path.join(HOME, ".gnupg")]
        for part in ("secrets", "settings", "server", "docs", "backups"):
            q = tc.data_path(part)
            if q:
                out.append(q)
        try:
            out.append(os.path.dirname(tc.secrets_path()))
        except Exception:  # noqa: BLE001
            pass
        return [x for x in out if x]

    def start_embedder(self, model):
        with self.lock:
            if not self.emb.running():
                avail = tc.backends()
                binary = avail.get("cpu") or next((v for v in avail.values() if v), None)
                if not binary:
                    return {"ok": False, "error": "No llama-server build in this image."}
                cmd = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(EMBED_PORT), "--embeddings", "-c", "2048",
                       "-ngl", "0", "--api-key", self.ensure_upstream_key(), "--alias", "victor-embed",
                       "-t", str(hostplan.default_threads())]
                self.spawn(self.emb, cmd, env={"LD_LIBRARY_PATH": os.path.dirname(binary)})
        import urllib.request
        for _ in range(90):
            if not self.emb.running():
                return {"ok": False, "error": "Embedding server exited. See the 'embed' log."}
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{EMBED_PORT}/health", timeout=2) as r:
                    if r.status == 200:
                        return {"ok": True}
            except OSError:
                pass
            time.sleep(1)
        return {"ok": False, "error": "Embedding server did not become ready within 90 s."}

    def embedder(self, start=True):
        """Local embeddings only: the chosen embedding GGUF, else the chat server if it serves /v1/embeddings, else none."""
        model = self.doc_config().get("embed_model")
        if model and os.path.isfile(model):
            if not self.emb.running():
                if not start:
                    return None, None
                r = self.start_embedder(model)
                if not r.get("ok"):
                    raise RuntimeError(r["error"])
            return docchat.Embedder(f"http://127.0.0.1:{EMBED_PORT}", self.ensure_upstream_key()), os.path.basename(model)
        if self.llm.running():
            e = docchat.Embedder(f"http://127.0.0.1:{UPSTREAM_PORT}", self.ensure_upstream_key(), timeout=30)
            try:
                e.embed(["probe"])
                return e, os.path.basename(self.llm_info.get("model") or "chat-model")
            except (OSError, ValueError):
                return None, None
        return None, None

    def docs_status(self):
        idx = self.doc_index()
        if not idx:
            return {"error": "Choose storage first. The document index is kept on your storage drive."}
        c = self.doc_config()
        return {"sources": idx.sources(), "stats": idx.stats(), "job": dict(self.doc_job.state),
                "embed": {"model": c.get("embed_model") or "", "running": self.emb.running()},
                "llm_running": self.llm.running(), "pdf": bool(shutil.which("pdftotext"))}

    def docs_action(self, b):
        idx = self.doc_index()
        if not idx:
            return 400, {"error": "Choose storage first."}
        act = b.get("action")
        if act == "add":
            try:
                p = idx.add_source(str(b.get("path", "")), self.doc_private_paths())
            except docchat.DocError as e:
                return 400, {"error": str(e)}
            return 200, {"ok": True, "path": p, "message": f"Added {p}. Press Index now to read it."}
        if act == "remove":
            n = idx.remove_source(str(b.get("path", "")))
            return 200, {"ok": True, "message": f"Removed from the index ({n} files). Your files were not touched."}
        if act == "index":
            if self.doc_job.running():
                return 409, {"error": "Indexing is already running."}
            self.doc_job.start(idx, lambda: self.embedder(start=True), exclude=self.doc_private_paths())
            return 200, {"ok": True, "job": dict(self.doc_job.state)}
        if act == "cancel":
            self.doc_job.cancel.set()
            return 200, {"ok": True}
        if act == "set-embed":
            m = str(b.get("model") or "")
            if m and m not in [x["path"] for x in tc.list_models(tc.load_settings()["extra_model_dirs"])]:
                return 400, {"error": "The embedding model must be inside a configured model directory."}
            p = tc.data_path("docs", "config.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tc.atomic_write(p, json.dumps(self.doc_config() | {"embed_model": m}), 0o600)
            self.stop(self.emb)
            return 200, {"ok": True, "message": "Embedding model saved. Re-index to compute vectors." if m else "Keyword search only."}
        if act in ("ask", "search"):
            q = str(b.get("question", "")).strip()
            if not q:
                return 400, {"error": "Type a question."}
            if len(q) > 2000:
                return 400, {"error": "Question too long (2000 characters max)."}
            try:
                emb, _ = self.embedder(start=False)
            except RuntimeError:
                emb = None
            k = max(1, min(12, int(b.get("k") or 6)))
            hits = idx.search(q, k=k, embedder=emb)
            res = {"ok": True, "hits": [{"path": h["path"], "name": os.path.basename(h["path"]), "text": h["text"][:900],
                                          "score": h["score"], "mode": h["mode"]} for h in hits]}
            if not hits:
                res["message"] = "Nothing in your indexed documents matches. Add a folder or re-index."
                return 200, res
            if act == "search":
                return 200, res
            if not self.llm.running():
                res["message"] = "Start a model to get a written answer. These are the matching passages from your documents."
                return 200, res
            try:
                res["answer"] = docchat.answer(q, hits, f"http://127.0.0.1:{UPSTREAM_PORT}", self.ensure_upstream_key())
            except (OSError, ValueError, KeyError) as e:
                res["message"] = f"The local model did not answer ({type(e).__name__}). Matching passages are shown."
            return 200, res
        return 400, {"error": "unknown documents action"}

    def llama_flags(self, binary):
        try:
            r = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=30)
            return r.stdout + r.stderr
        except (OSError, subprocess.SubprocessError):
            return ""

    def plan_llm(self, settings):
        """Resolve backend, model, memory fit; returns dict with 'error' if it cannot start."""
        model = settings.get("model")
        if not model or not os.path.isfile(model):
            return {"error": "No model selected, or the file no longer exists. Select a model on the Models tab."}
        ok, why = tc.check_split_complete(model)
        if not ok:
            return {"error": why}
        gl = tc.gpus()
        backend, reason = tc.choose_backend(settings["backend"], gl)
        if not backend:
            return {"error": reason}
        ram = tc.ram_info()
        vk = tc.vulkan_devices() if backend == "vulkan" else None
        dev = hostplan.select_device(backend, gl, ram, vk)
        try:
            est = tc.estimate_memory(model, int(settings["ctx"]))
            ctx, ctx_note = int(settings["ctx"]), "Context set manually by the owner."
            if settings.get("ctx_auto", True):
                pool = dev["budget"] if dev["kind"] == "dedicated" and est["weights"] + est["overhead"] <= dev["budget"] \
                    else dev["budget"] + (int(ram.get("available", 0) * hostplan.RAM_FRACTION) if dev["kind"] == "dedicated" else 0)
                ctx, ok, ctx_note = hostplan.auto_context(
                    est["weights"], lambda c: tc.estimate_memory(model, c)["kv_cache"], est["overhead"], pool, est.get("ctx_train"))
                est = tc.estimate_memory(model, ctx)
        except tc.GGUFError as e:
            return {"error": f"This file is not a valid GGUF model: {e}"}
        vram_free = dev["free"] if dev["kind"] == "dedicated" else None
        fit = tc.fit_report(est, ram, vram_free)
        limit = int(settings.get("memory_limit_mb") or 0) * 1048576
        if limit and est["total"] > limit and not (vram_free and est["total"] <= vram_free):
            return {"error": f"Estimated memory {tc.human(est['total'])} exceeds your limit of {tc.human(limit)}. "
                             "Lower context length, pick a smaller quantisation, or raise the limit."}
        if fit["level"] == "bad":
            return {"error": "Not enough memory: " + " ".join(fit["notes"]), "estimate": est, "fit": fit}
        ngl = int(settings.get("gpu_layers", -1))
        if backend == "cpu":
            ngl = 0
        elif ngl < 0:
            ngl = hostplan.plan_layers(est, dev, ctx)
        threads = int(settings.get("threads") or 0) or hostplan.default_threads()
        return {"backend": backend, "reason": reason, "model": model, "estimate": est, "fit": fit,
                "ngl": ngl, "ctx": ctx, "ctx_note": ctx_note, "threads": threads,
                "device": {k: dev[k] for k in ("kind", "budget", "free", "ambiguous", "notes")} |
                          {"name": (dev["device"] or {}).get("name"), "index": dev.get("index"), "others": len(dev["others"])},
                "binary": tc.backends()[backend]}

    def start_llm(self, settings, force=False):
        with self.lock:
            if self.llm.running():
                return {"ok": True, "message": "Already running."}
            if tc.data_root() is None:
                return {"ok": False, "error": "No storage selected. Choose persistent storage or temporary-session mode first."}
            plan = self.plan_llm(settings)
            if "error" in plan:
                return {"ok": False, **plan}
            key = self.ensure_upstream_key()
            help_text = self.llama_flags(plan["binary"])
            cmd = [plan["binary"], "-m", plan["model"], "--host", "127.0.0.1", "--port", str(UPSTREAM_PORT),
                   "-c", str(plan["ctx"]), "-ngl", str(plan["ngl"]), "--api-key", key, "--alias", "victor-local"]
            # security-relevant flags: only passed when this llama-server build knows them
            if "--no-ui-mcp-proxy" in help_text:
                cmd.append("--no-ui-mcp-proxy")
            if "--jinja" in help_text:
                cmd.append("--jinja")
            # NOTE: '--tools' (built-in shell/file tools) is deliberately never passed.
            cmd += ["-t", str(plan["threads"])]
            idle = int(settings.get("idle_unload_s") or 0)
            if idle > 0 and "--sleep-idle-seconds" in help_text:
                cmd += ["--sleep-idle-seconds", str(idle)]
            # belt and braces: shared libs and dynamic ggml backends live next to the binary
            env = {"LD_LIBRARY_PATH": os.path.dirname(plan["binary"])}
            idx = plan["device"].get("index")
            if plan["backend"] == "cuda" and idx is not None and plan["device"]["others"]:
                env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"          # matches nvidia-smi numbering
                env["CUDA_VISIBLE_DEVICES"] = str(idx)           # memory is attributed to exactly this card
            if plan["backend"] == "vulkan":
                env["GGML_VK_VISIBLE_DEVICES"] = os.environ.get("GGML_VK_VISIBLE_DEVICES", "")
                if not env["GGML_VK_VISIBLE_DEVICES"]:
                    env.pop("GGML_VK_VISIBLE_DEVICES")
            self.spawn(self.llm, cmd, env=env)
            self.llm_info = {"backend": plan["backend"], "reason": plan["reason"], "ngl": plan["ngl"],
                             "model": plan["model"], "estimate": plan["estimate"], "fit": plan["fit"],
                             "ctx": plan["ctx"], "ctx_note": plan["ctx_note"], "threads": plan["threads"], "device": plan["device"]}
            hostplan.write_host_profile(hostplan.host_profile(settings))
        time.sleep(1.5)
        if not self.llm.running():
            return {"ok": False, "error": "llama-server exited immediately. See the log below.", "log": self.tail("llama", 40)}
        gw = self.start_gateway(settings)
        if not gw.get("ok"):
            return {"ok": False, "error": "Model started but the API gateway did not: " + gw.get("error", "unknown"),
                    "log": self.tail("gateway", 20)}
        self.write_runtime(True, {"state": "waiting"})
        threading.Thread(target=self.verify_generation, args=(dict(self.llm_info), key), daemon=True).start()
        return {"ok": True, "plan": self.llm_info}

    def write_runtime(self, running, verify=None):
        """Volatile, secret-free runtime summary for the gateway's read-only companion report."""
        path = os.path.join(tc.RUN_DIR, "runtime.json")
        try:
            if not running:
                if os.path.exists(path):
                    os.unlink(path)
                return
            os.makedirs(tc.RUN_DIR, exist_ok=True)
            i = self.llm_info
            tc.atomic_write(path, json.dumps({"running": True, "model": i.get("model"), "backend": i.get("backend"),
                                              "ctx": i.get("ctx"), "ngl": i.get("ngl"), "verify": verify}), 0o644)
        except OSError:
            pass

    def verify_generation(self, info, key):
        """Mark a model 'verified' only after llama-server really produced a token on this PC."""
        import urllib.request
        base = f"http://127.0.0.1:{UPSTREAM_PORT}"
        self.llm_verify = {"state": "waiting"}
        for _ in range(120):
            if not self.llm.running():
                self.llm_verify = {"state": "failed", "error": "llama-server exited before it finished loading."}
                self.write_runtime(True, {"state": "failed"})
                return
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            time.sleep(1)
        else:
            self.llm_verify = {"state": "failed", "error": "Model did not become ready within 2 minutes."}
            self.write_runtime(True, {"state": "failed"})
            return
        try:
            req = urllib.request.Request(base + "/v1/chat/completions", method="POST",
                                         data=json.dumps({"model": "victor-local", "max_tokens": 1,
                                                          "messages": [{"role": "user", "content": "Hi"}]}).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read().decode())
            if not body.get("choices"):
                raise ValueError("no choices in response")
        except (OSError, ValueError) as e:
            self.llm_verify = {"state": "failed", "error": f"Loaded but did not generate: {e}"}
            self.write_runtime(True, {"state": "failed"})
            self.record_library_run(info, False, str(e))
            return
        name = (info.get("device") or {}).get("name")
        hostplan.record_verified(info["model"], info["backend"], name, info["ngl"], info["ctx"])
        self.llm_verify = {"state": "verified", "backend": info["backend"], "device": name}
        self.record_library_run(info, True)
        self.write_runtime(True, {"state": "verified"})

    # ---------------------------------------------------------- pc check --
    def pc_check(self):
        facts = collect_pc_facts(getattr(self, "last_speed", None))
        report = pccheck.evaluate(facts)
        saved = None
        d = tc.data_path("reports")
        if d:
            try:
                saved = pccheck.save_profile(d, facts, report)
            except OSError as e:
                saved = {"error": str(e)}
        return {"report": report, "facts": facts, "saved": saved, "text": pccheck.as_text(report)}

    def speed_test(self, n_tokens=64):
        """Short real generation on the model that is already loaded. Never starts or downloads a model."""
        import urllib.request
        if not self.llm.running():
            return {"error": "Start a model first. The speed test only uses the model that is already loaded."}
        key = self.ensure_upstream_key()
        req = urllib.request.Request(f"http://127.0.0.1:{UPSTREAM_PORT}/v1/chat/completions", method="POST",
                                     data=json.dumps({"model": "victor-local", "max_tokens": n_tokens, "temperature": 0,
                                                      "messages": [{"role": "user", "content": "Count from one to fifty in words, comma separated."}]}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.loads(r.read().decode())
        except (OSError, ValueError) as e:
            return {"error": f"Speed test failed: {e}"}
        sp = pccheck.speed_from_response(body, time.time() - t0, n_tokens)
        sp.update(backend=self.llm_info.get("backend"), model=self.llm_info.get("model"), ts=tc.now())
        self.last_speed = sp
        return {"ok": True, "speed": sp}


    # ---------------------------------------------------------- library --
    def library(self):
        p = tc.data_path("settings", "library.json")
        return modellib.Library(p) if p else None

    def pc_id(self):
        return modellib.pc_signature(model_advisor.cpu_info()["name"], [g.get("name") for g in tc.gpus()], tc.ram_info().get("total"))

    def record_library_run(self, info, ok, note=None):
        lib = self.library()
        if lib and info.get("model"):
            try:
                lib.record_run(info["model"], self.pc_id(), info.get("backend"), (info.get("device") or {}).get("name"),
                               info.get("ngl"), info.get("ctx"), ok, note)
            except OSError:
                pass

    def library_list(self):
        lib = self.library()
        if not lib:
            return {"error": "Choose storage first; the library is kept on your storage drive."}
        s = tc.load_settings()
        models = lib.describe(tc.list_models(s["extra_model_dirs"]), self.pc_id(), lambda p: tc.read_gguf_header(p)["meta"])
        return {"models": models, "pc": self.pc_id(), "selected": s.get("model"),
                "job": self.hash_job.status() if self.hash_job else {"state": "idle"}}

    def library_action(self, b):
        lib = self.library()
        if not lib:
            return 400, {"error": "Choose storage first."}
        s = tc.load_settings()
        known = {m["path"] for m in tc.list_models(s["extra_model_dirs"])}
        act = b.get("action")
        paths = b.get("paths") if isinstance(b.get("paths"), list) else ([b["path"]] if isinstance(b.get("path"), str) else [])
        if act != "hash-all" and act != "cancel" and (not paths or any(p not in known for p in paths)):
            return 400, {"error": "Model must be inside a configured model directory."}
        if act in ("hash", "hash-all"):
            if self.hash_job and self.hash_job.thread and self.hash_job.thread.is_alive():
                return 409, {"error": "A hashing job is already running."}
            if act == "hash" and b.get("expected"):
                lib.set_expected(paths[0], str(b["expected"]))
            self.hash_job = modellib.HashJob(lib)
            return 200, {"ok": True, "job": self.hash_job.start(sorted(known) if act == "hash-all" else paths)}
        if act == "cancel":
            if self.hash_job:
                self.hash_job.stop()
            return 200, {"ok": True}
        if act == "accept":
            lib.reset_hash(paths[0])
            return 200, {"ok": True, "message": "Reference checksum cleared; hash again to record the current file."}
        if act == "remember":
            keys = ("backend", "ctx", "ctx_auto", "gpu_layers", "threads")
            src = b.get("settings") if isinstance(b.get("settings"), dict) else {k: s[k] for k in keys if k in s}
            return 200, {"ok": True, "settings": lib.remember(paths[0], src), "message": "Settings remembered for this model."}
        if act == "forget":
            lib.forget_settings(paths[0])
            return 200, {"ok": True, "message": "Remembered settings removed."}
        if act == "apply":
            saved = lib.entry(paths[0]).get("settings")
            s["model"] = paths[0]
            for k in ("backend", "ctx", "ctx_auto", "gpu_layers", "threads"):
                if saved and k in saved:
                    s[k] = saved[k]
            tc.save_settings(s)
            return 200, {"ok": True, "message": "Model selected" + (" with its remembered settings." if saved else ".")}
        return 400, {"error": "unknown library action"}

    def stop_llm(self):
        self.write_runtime(False)
        with self.lock:
            self.stop(self.llm)
            self.stop(self.gw)
            self.llm_info = {}

    def start_gateway(self, settings):
        with self.lock:
            self.stop(self.gw)
            rm = settings["remote"]
            cmd = [sys.executable, "-I", os.path.join(HERE, "gateway.py"), "--upstream-port", str(UPSTREAM_PORT)]
            env = {"PYTHONPATH": HERE}
            try:
                self.spawn(self.gw, cmd, env=env)
            except OSError as e:
                return {"ok": False, "error": str(e)}
        time.sleep(0.7)
        if not self.gw.running():
            return {"ok": False, "error": "Gateway did not start: " + self.tail("gateway", 5)}
        return {"ok": True}

    def accel_report(self):
        """Derive real acceleration facts from the llama-server log, not from package presence."""
        text = self.tail("llama", 400)
        info = {"offloaded_layers": None, "total_layers": None, "devices": [], "errors": []}
        m = re.search(r"offloaded (\d+)/(\d+) layers to GPU", text)
        if m:
            info["offloaded_layers"], info["total_layers"] = int(m.group(1)), int(m.group(2))
        for line in text.splitlines():
            if re.search(r"ggml_vulkan: \d+ = |Vulkan\d+ \(|ggml_cuda_init: found|ROCm\d* \(", line):
                info["devices"].append(line.strip()[:200])
            if re.search(r"\b(error|failed|out of memory|cannot|unable)\b", line, re.I):
                info["errors"].append(line.strip()[:200])
        info["errors"] = info["errors"][-8:]
        info["devices"] = info["devices"][-6:]
        if info["offloaded_layers"] is None:
            info["gpu_accelerated"] = None
        else:
            info["gpu_accelerated"] = info["offloaded_layers"] > 0
        return info

    def status(self):
        s = tc.load_settings()
        gl = tc.gpus()
        be, reason = tc.choose_backend(s["backend"], gl)
        svcs = []
        for k, d in SERVICES.items():
            base = os.path.join(HOME, d["dir"]) if "dir" in d else None
            installed = (base and os.path.isdir(base)) or (d.get("bin") and shutil.which(d["bin"]))
            svcs.append({"name": k, "label": d["label"], "icon": d["icon"], "installed": bool(installed),
                         "running": self.svc[k].running(), "url": d["url"]})
        sec = tc.load_secrets()
        up = self.llm.running()
        return {
            "persist": tc.persist_state(), "data_root": tc.data_root(),
            "storage_problems": [{"role": r["role"], "status": r["status"], "message": r["message"]} for r in tc.storage_status()["problems"]],
            "ram": tc.ram_info(), "gpus": gl, "render_nodes": tc.render_nodes(),
            "backends_present": tc.backends(), "backend": {"selected": be, "reason": reason},
            "llm": {"running": up, "info": self.llm_info if up else {}, "accel": self.accel_report() if up else None,
                    "uptime": int(time.time() - self.llm.started) if up else None},
            "gateway": {"running": self.gw.running()}, "verify": getattr(self, "llm_verify", None),
            "services": svcs, "settings": s, "has_api_key": bool(sec.get("api_keys")),
            "ssh": self.priv_json(["ssh-status"]),
            "offline_notice": "All inference is local. No telemetry, cloud inference or automatic downloads are configured.",
        }

    # -------------------------------------------------------------- priv --
    def priv(self, args, stdin=None, timeout=60):
        try:
            r = subprocess.run(["sudo", "-n", PRIV] + args, input=stdin, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
        return r.returncode, (r.stdout.strip() if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip()))

    def priv_json(self, args):
        rc, out = self.priv(args)
        if rc != 0:
            return {"error": out}
        try:
            return json.loads(out)
        except ValueError:
            return {"error": out}

    # ----------------------------------------------------------- remote --
    def set_remote(self, rem):
        s = tc.load_settings()
        r = s["remote"]
        bind = str(rem.get("bind", r["bind"])).strip()
        if not re.fullmatch(r"[0-9a-fA-F:.]+", bind):
            return {"ok": False, "error": "Bind must be an IP address (use 127.0.0.1 for local-only, a LAN/VPN address, or 0.0.0.0 for all interfaces)."}
        port = int(rem.get("port", r["port"]))
        if not 1024 <= port <= 65535:
            return {"ok": False, "error": "Port must be 1024-65535."}
        enabled = bool(rem.get("enabled", r["enabled"]))
        if enabled and not tc.load_secrets().get("api_keys"):
            return {"ok": False, "error": "Create an API key first (remote access always requires one)."}
        if enabled and bind in ("127.0.0.1", "::1"):
            enabled = False   # nothing remote about loopback; keep honest state
        tls = bool(rem.get("tls", r["tls"]))
        if tls and not (tc.data_path("server", "tls", "cert.pem") and os.path.exists(tc.data_path("server", "tls", "cert.pem"))):
            return {"ok": False, "error": "TLS needs a certificate. Generate one first (button below), or use a VPN/tunnel instead."}
        s["remote"] = {"enabled": enabled, "bind": bind if enabled else "127.0.0.1", "port": port, "tls": tls}
        tc.save_settings(s)
        if self.llm.running():
            self.start_gateway(s)
        return {"ok": True, "remote": s["remote"]}

    def new_api_key(self):
        key = "victor-" + pysecrets.token_urlsafe(32)
        sec = tc.load_secrets()
        sec["api_keys"] = [hashlib.sha256(key.encode()).hexdigest()]   # rotate: replaces every key (named ones too)
        sec["api_key_meta"] = {sec["api_keys"][0]: {"name": "Main key", "created": int(time.time()), "expires": None, "rpm": 0}}
        apikeys.cancel_pairing(sec)
        tc.save_secrets(sec)
        s = tc.load_settings()
        if self.gw.running():
            self.start_gateway(s)
        return key

    # ------------------------------------------------------ named keys --
    def usage_path(self):
        return tc.data_path("logs", "api-usage.jsonl")

    def keys_status(self):
        sec = tc.load_secrets()
        up = self.usage_path()
        use = apikeys.usage_summary(up) if up else {}
        keys = apikeys.listing(sec)
        for k in keys:
            k["usage"] = use.get(k["id"], {"last_24h": 0, "errors_24h": 0, "last": None})
        r = tc.load_settings()["remote"]
        cert = tc.data_path("server", "tls", "cert.pem")
        return {"keys": keys, "recent": apikeys.recent(up, 25) if up else [], "pairing": apikeys.pairing_state(sec),
                "pair_ready": {"remote": bool(r["enabled"]), "tls": bool(r["tls"]), "cert": bool(cert and os.path.exists(cert)),
                               "gateway": self.gw.running(), "bind": r["bind"], "port": r["port"]},
                "lan_discovery": False}

    def keys_action(self, b):
        act = b.get("action")
        sec = tc.load_secrets()
        try:
            if act == "create":
                key, pub = apikeys.create(sec, b.get("name"), b.get("expires_days") or 0, b.get("rpm") or 0)
                tc.save_secrets(sec)
                return 200, {"ok": True, "api_key": key, "key": pub, "message": "Copy this key now. It is shown only once."}
            if act == "revoke":
                apikeys.revoke(sec, str(b.get("id", "")))
                tc.save_secrets(sec)
                return 200, {"ok": True, "message": "Key revoked. It stops working immediately."}
            if act == "pair-cancel":
                apikeys.cancel_pairing(sec)
                tc.save_secrets(sec)
                return 200, {"ok": True, "message": "Pairing window closed."}
            if act == "pair-start":
                st = self.keys_status()["pair_ready"]
                if not (st["remote"] and st["tls"] and st["cert"]):
                    return 400, {"error": "Pair phone needs remote access turned on with TLS and a certificate, so the phone can pin this PC. "
                                          "Set that up above first. Pairing stays off until then."}
                host = str(b.get("host") or "").strip() or st["bind"]
                if host in ("0.0.0.0", "::") or not re.fullmatch(r"[A-Za-z0-9.\-:]{1,253}", host):
                    return 400, {"error": "Enter the address the phone should use (this PC's LAN or VPN IP, or its hostname)."}
                fp = companion.tls_fingerprint()
                if not fp:
                    return 400, {"error": "The TLS certificate could not be read."}
                pid = apikeys.profile_id(sec)
                token, exp = apikeys.start_pairing(sec)
                gw = f"https://{'[' + host + ']' if ':' in host else host}:{st['port']}"
                uri = apikeys.pair_uri(pid, "VictorLinux " + (os.uname().nodename or "PC")[:40], gw, fp, token, exp)
                tc.save_secrets(sec)
                return 200, {"ok": True, "uri": uri, "svg": qrsvg.svg(uri), "expires": exp, "gateway": gw, "fingerprint_sha256": fp,
                             "gateway_running": st["gateway"],
                             "message": "Scan with the phone within 10 minutes. The QR holds a single-use pairing token, never an API key."}
        except ValueError as e:
            return 400, {"error": str(e)}
        return 400, {"error": "unknown keys action"}

    def revoke_remote(self):
        sec = tc.load_secrets()
        sec["api_keys"] = []
        sec["api_key_meta"] = {}
        apikeys.cancel_pairing(sec)
        tc.save_secrets(sec)
        s = tc.load_settings()
        s["remote"] = {"enabled": False, "bind": "127.0.0.1", "port": s["remote"]["port"], "tls": False}
        tc.save_settings(s)
        if self.gw.running():
            self.start_gateway(s)
        return {"ok": True}


    def input_status(self):
        """Wi-Fi input is off unless the owner started it. Never includes Bearer tokens."""
        st = victorinput.default_off_status()
        root = os.environ.get("VICTOR_INPUT_DIR") or tc.data_path("input") or victorinput.default_state_dir()
        try:
            sec = victorinput.load_secrets(root)
            st["devices"] = victorinput.listing(sec)
            st["pairing"] = victorinput.pairing_state(sec)
        except OSError:
            pass
        # Prefer the live admin socket when the service is running (localhost only).
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:%d/status" % victorinput.ADMIN_PORT, timeout=0.4) as r:
                live = json.loads(r.read().decode())
                live.setdefault("default_off", True)
                return live
        except OSError:
            pass
        rc, out = self.priv(["input-status"])
        if rc == 0:
            try:
                st.update(json.loads(out))
            except ValueError:
                pass
        st["firewall_note"] = victorinput.firewall_note(st.get("bind") or "0.0.0.0", st.get("port") or victorinput.DEFAULT_PORT)
        return st

    # ------------------------------------------------------------ mesh --
    def mesh_priv(self, args, stdin=None, timeout=240):
        exe = os.environ.get("VICTOR_MESH_PRIV", "/usr/local/lib/victorlinux/victor-mesh-priv")
        try:
            r = subprocess.run(["sudo", "-n", exe] + args, input=stdin, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()

    def mesh_status(self):
        import victormesh as vm
        p = vm.paths()
        services = {"tailscaled": vm.unit_active("tailscaled.service"), "share": vm.unit_active("victor-mesh-share.service"),
                    "taildrop": vm.unit_active("victor-mesh-taildrop.service")}
        st = vm.ts_status() if services["tailscaled"] else None
        out = vm.summarize(vm.load_settings(p), st, p, services)
        out["edition_tag"] = vm.tag_for(vm.edition())
        return out

    def mesh_action(self, b):
        """Join Mesh screen. The pre-auth key goes to the root helper on stdin once; it is never stored or echoed."""
        import victormesh as vm
        act = b.get("action")
        if act == "status":
            return 200, self.mesh_status()
        if act == "join":
            try:
                url = vm.normalize_url(str(b.get("url", "")))
                name = vm.valid_hostname(str(b.get("name", "")))
                key = vm.valid_authkey(str(b.get("key", "")))
            except vm.MeshError as e:
                return 400, {"error": str(e)}
            rc, out = self.mesh_priv(["join", url] + ([name] if name else []), stdin=key + "\n")
            out = out.replace(key, "***")
            del key
        elif act in ("up", "down", "leave", "share-password"):
            rc, out = self.mesh_priv([act])
        elif act == "set":
            t, v = str(b.get("toggle", "")), "on" if b.get("on") else "off"
            if t not in ("exit-node", "wol", "taildrop", "share", "ssh"):
                return 400, {"error": "unknown mesh setting"}
            rc, out = self.mesh_priv(["set", t, v])
        elif act == "share-remove":
            try:
                vm.share_remove(vm.paths()["share_dir"], str(b.get("name", "")))
            except (vm.MeshError, OSError) as e:
                return 400, {"error": str(e)}
            return 200, {"ok": True}
        else:
            return 400, {"error": "unknown mesh action"}
        if rc != 0:
            return 400, {"error": out or "victor-mesh-priv failed"}
        try:
            return 200, json.loads(out)
        except ValueError:
            return 200, {"ok": True, "message": out}

    def input_action(self, b):
        act = b.get("action")
        root = os.environ.get("VICTOR_INPUT_DIR") or tc.data_path("input") or victorinput.default_state_dir()
        if act == "enable":
            bind = str(b.get("bind") or "0.0.0.0").strip()
            port = str(b.get("port") or victorinput.DEFAULT_PORT)
            rc, out = self.priv(["input-enable", bind, port])
            if rc != 0:
                return 400, {"error": out or "Could not start the input service."}
            try:
                return 200, json.loads(out)
            except ValueError:
                return 200, {"ok": True, "message": out}
        if act == "disable":
            rc, out = self.priv(["input-disable"])
            if rc != 0:
                return 400, {"error": out or "Could not stop the input service."}
            try:
                return 200, json.loads(out)
            except ValueError:
                return 200, {"ok": True, "message": out}
        if act == "pair-start":
            # Prefer the running admin socket so the service and the QR share the same secrets file.
            host = str(b.get("host") or "").strip()
            try:
                import urllib.request
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/pair-start" % victorinput.ADMIN_PORT,
                    data=json.dumps({"host": host}).encode(), method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2) as r:
                    data = json.loads(r.read().decode())
                if data.get("uri"):
                    data["svg"] = qrsvg.svg(data["uri"])
                return 200, data
            except OSError:
                app = victorinput.InputApp(root, sink=victorinput.MemorySink())
                try:
                    data = app.start_pairing_uri(host)
                except ValueError as e:
                    return 400, {"error": str(e)}
                data["svg"] = qrsvg.svg(data["uri"])
                data["message"] = data.get("message", "") + " Turn the service on so the phone can redeem the token."
                return 200, data
            except Exception as e:  # noqa: BLE001
                return 400, {"error": str(e)}
        if act == "pair-cancel":
            try:
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:%d/pair-cancel" % victorinput.ADMIN_PORT,
                                             data=b"{}", method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    return 200, json.loads(r.read().decode())
            except OSError:
                sec = victorinput.load_secrets(root)
                victorinput.cancel_pairing(sec)
                victorinput.save_secrets(sec, root)
                return 200, {"ok": True, "message": "Pairing window closed."}
        if act == "revoke":
            sec = victorinput.load_secrets(root)
            try:
                n = victorinput.revoke(sec, str(b.get("id", "")))
            except ValueError as e:
                return 400, {"error": str(e)}
            victorinput.save_secrets(sec, root)
            return 200, {"ok": True, "revoked": n, "message": "Device revoked. It can no longer send input."}
        return 400, {"error": "unknown input action"}

    def mic_status(self):
        import victormic
        pc, default = victormic.list_pc_sources()
        st = victormic.load_settings()
        reg = victormic.read_status_registry()
        r = victormic.resolve(st, pc, default, reg)
        return {"settings": st, "inputs": [vars(x) for x in pc],
                "phones": [{"label": x.label(), "device_id": x.device_id, "provider": x.provider, "live": x.live} for x in reg.active()],
                "selected": r.selected, "delivering": r.delivering or None, "state": r.state, "recovery": r.recovery}

    def mic_action(self, b):
        import victormic
        st = victormic.load_settings()
        act = b.get("action")
        if act == "default":
            st["mode"] = "default"
        elif act == "pc" and b.get("stable_id"):
            st.update(mode="pc", pc_stable_id=str(b["stable_id"]))
        elif act == "phone" and b.get("device_id") and b.get("provider") in victormic.PROVIDERS:
            st.update(mode="phone", phone={"device_id": str(b["device_id"]), "provider": b["provider"]})
        elif act == "fallback":
            st["fallback_to_preferred_phone"] = bool(b.get("on"))
            if b.get("on") and b.get("device_id") and b.get("provider") in victormic.PROVIDERS:
                st["preferred_phone"] = {"device_id": str(b["device_id"]), "provider": b["provider"]}
        elif act == "mute":
            st["muted"] = bool(b.get("on"))
        else:
            return 400, {"error": "unknown microphone action"}
        victormic.save_settings(st)
        return 200, self.mic_status()

    def bluetooth_status(self):
        st = {"default": "off", "pairing": {"active": False}, "devices": [],
              "message": "Bluetooth stays off by default. It is only started when you press Pair phone (Bluetooth)."}
        rc, out = self.priv(["bt-status"])
        if rc == 0:
            try:
                st.update(json.loads(out))
            except ValueError:
                st["message"] = out
        rc, out = self.priv(["bt-list"])
        if rc == 0:
            try:
                st["devices"] = json.loads(out).get("devices") or []
            except ValueError:
                pass
        return st

    def bluetooth_action(self, b):
        act = b.get("action")
        mapping = {"start": ["bt-start"], "confirm": ["bt-confirm"], "cancel": ["bt-cancel"]}
        if act in mapping:
            rc, out = self.priv(mapping[act])
            if rc != 0:
                return 400, {"error": out or "Bluetooth helper failed."}
            try:
                return 200, json.loads(out)
            except ValueError:
                return 200, {"ok": True, "message": out}
        if act == "unpair":
            rc, out = self.priv(["bt-unpair", str(b.get("mac") or "")])
            if rc != 0:
                return 400, {"error": out or "Could not unpair that device."}
            try:
                return 200, json.loads(out)
            except ValueError:
                return 200, {"ok": True, "message": out}
        return 400, {"error": "unknown bluetooth action"}

    # -------------------------------------------------------------- diag --
    def diagnostics(self):
        lines = [f"VictorLinux diagnostics {tc.now()}"]
        try:
            lines.append("Image: " + (open("/etc/victor-release").read().strip()))
        except OSError:
            lines.append("Image: (no /etc/victor-release)")
        lines.append("Kernel: " + os.uname().release)
        r = tc.ram_info()
        lines.append(f"RAM: total {tc.human(r['total'])}, available {tc.human(r['available'])}")
        gl = tc.gpus()
        if not gl:
            lines.append("GPU: none detected via sysfs/nvidia-smi")
        for g in gl:
            lines.append(f"GPU: {g['name']} [{g.get('pci_id','')}] driver={g.get('driver')} "
                         f"VRAM total={tc.human(g.get('vram_total'))} used={tc.human(g.get('vram_used'))}")
        for n in tc.render_nodes():
            lines.append(f"Render node: {n['path']} read/write={'yes' if n['accessible'] else 'NO - add user to video/render groups'}")
        lines.append("User groups: " + subprocess.run(["id", "-Gn"], capture_output=True, text=True).stdout.strip())
        vk = tc.vulkan_devices()
        lines.append(f"Vulkan: available={vk['available']} error={vk.get('error')}")
        for d in vk["devices"]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in d.items()))
        be, why = tc.choose_backend(tc.load_settings()["backend"], gl, vk)
        lines.append(f"Backends present: {tc.backends()}")
        dev = hostplan.select_device(be, gl, r, vk if be == "vulkan" else None)
        lines.append(f"Memory pool: {dev['kind']} budget={tc.human(dev['budget'])} free={tc.human(dev['free'])} "
                     f"device={(dev['device'] or {}).get('name')} other_devices={len(dev['others'])}")
        for n in dev["notes"]:
            lines.append("  note: " + n)
        lines.append(f"Default threads: {hostplan.default_threads()} (physical {hostplan.physical_threads()})")
        if getattr(self, "llm_verify", None):
            lines.append("Generation check: " + json.dumps(self.llm_verify))
        lines.append(f"Selected backend: {be} - {why}")
        lines.append(f"Storage: {json.dumps(tc.persist_state())}")
        if self.llm.running():
            lines.append("Accel (from llama-server log): " + json.dumps(self.accel_report()))
        lines.append("--- llama-server log (tail) ---")
        lines.append(self.tail("llama", 40))
        lines.append("--- gateway log (tail) ---")
        lines.append(self.tail("gateway", 10))
        text = "\n".join(lines)
        return re.sub(r"(victor-[A-Za-z0-9_\-]{20,})", "victor-***", text)


APP = None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "VictorCC"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json", nonce=None):
        b = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        script_src = f"'self' 'nonce-{nonce}'" if nonce else "'self'"
        self.send_header("Content-Security-Policy", f"default-src 'self'; script-src {script_src}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _auth(self):
        t = self.headers.get("X-Victor-Token", "")
        return hmac.compare_digest(t, APP.token)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 1 << 20:
            raise ValueError("body too large")
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            # The single page has one inline <script>; allow only that one via a per-response nonce.
            nonce = pysecrets.token_urlsafe(16)
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                page = f.read().replace(b"<script>", b'<script nonce="' + nonce.encode() + b'">')
            return self._send(200, page, "text/html; charset=utf-8", nonce=nonce)
        if not u.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._auth():
            return self._send(401, {"error": "invalid token - reopen the Control Center from the desktop/menu entry"})
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/status":
                return self._send(200, APP.status())
            if u.path == "/api/models":
                s = tc.load_settings()
                return self._send(200, {"models": tc.list_models(s["extra_model_dirs"]), "dirs": [tc.data_path("models")] + s["extra_model_dirs"]})
            if u.path == "/api/library":
                r = APP.library_list()
                return self._send(400 if "error" in r else 200, r)
            if u.path == "/api/recommendations":
                gl = tc.gpus()
                s = tc.load_settings()
                backend, _reason = tc.choose_backend("auto", gl)
                ram = tc.ram_info()
                dev = hostplan.select_device(backend, gl, ram, tc.vulkan_devices() if backend == "vulkan" else None)
                rep = model_advisor.report(ram, gl, backend, s["ctx"])
                rep["host"] = {"backend": backend, "memory_kind": dev["kind"], "budget": dev["budget"], "notes": dev["notes"],
                               "device": (dev["device"] or {}).get("name"), "other_devices": len(dev["others"]),
                               "threads_default": hostplan.default_threads()}
                rep["local"] = hostplan.rank_local(tc.list_models(s["extra_model_dirs"]), ram, dev, backend)
                rep["local_note"] = ("Fit is an estimate. 'verified-here' means this exact file loaded and produced a token on "
                                     "this backend/device; 'untested' means it has not been proven on this PC.")
                return self._send(200, rep)
            if u.path == "/api/estimate":
                path = q["path"][0]
                ctx = int(q.get("ctx", ["4096"])[0])
                s = tc.load_settings()
                if path not in [m["path"] for m in tc.list_models(s["extra_model_dirs"])]:
                    return self._send(400, {"error": "path is not in a configured model directory"})
                est = tc.estimate_memory(path, ctx)
                gl = tc.gpus()
                vf = None
                for g in gl:
                    if g.get("vram_total") is not None:
                        vf = g["vram_total"] - (g.get("vram_used") or 0)
                        break
                return self._send(200, {"estimate": est, "fit": tc.fit_report(est, tc.ram_info(), vf), "vram_free": vf,
                                        "disk": shutil.disk_usage(os.path.dirname(path)).free})
            if u.path == "/api/logs":
                name = q.get("name", ["llama"])[0]
                if name not in ("llama", "gateway", "embed") + tuple(SERVICES):
                    return self._send(400, {"error": "unknown log"})
                return self._send(200, {"log": APP.tail(name, int(q.get("n", ["300"])[0]))})
            if u.path == "/api/fleet":
                st = APP.fleet(["status"])
                st["plans"] = APP.fleet(["plans"]) if st.get("running") else []
                return self._send(200, st)
            if u.path == "/api/keys":
                return self._send(200, APP.keys_status())
            if u.path == "/api/input":
                return self._send(200, APP.input_status())
            if u.path == "/api/mic":
                return self._send(200, APP.mic_status())
            if u.path == "/api/bluetooth":
                return self._send(200, APP.bluetooth_status())


            if u.path == "/api/health":
                return self._send(200, APP.storage_health(q.get("refresh", ["0"])[0] == "1"))
            if u.path == "/api/backups":
                d = APP.backup_dir()
                return self._send(200, {"backups": vbackup.list_backups(d) if d else [], "dir": d,
                                        "gpg": bool(shutil.which("gpg")), "items": APP.BACKUP_ITEMS, "secret_items": APP.BACKUP_SECRETS})


            if u.path == "/api/docs":
                r = APP.docs_status()
                return self._send(400 if "error" in r else 200, r)


            if u.path == "/api/pccheck":
                return self._send(200, APP.pc_check())
            if u.path == "/api/diagnostics":
                return self._send(200, {"text": APP.diagnostics()})
            if u.path == "/api/import/status":
                return self._send(200, APP.import_job)
            if u.path == "/api/storage":
                rc, out = APP.priv(["list-storage"])
                if rc != 0:
                    return self._send(500, {"error": out})
                j = json.loads(out)
                j["summary"] = tc.storage_status()          # same dictionary `victor-storage status --json` prints
                return self._send(200, j)
        except tc.GGUFError as e:
            return self._send(400, {"error": f"Invalid GGUF: {e}"})
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface to UI instead of silent failure
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._auth():
            return self._send(401, {"error": "invalid token"})
        p = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
            if p == "/api/settings":
                s = tc.load_settings()
                for k in ("backend", "model", "ctx", "gpu_layers", "threads", "memory_limit_mb", "idle_unload_s"):
                    if k in b:
                        s[k] = b[k]
                if "ctx_auto" in b:
                    if not isinstance(b["ctx_auto"], bool):
                        return self._send(400, {"error": "ctx_auto must be true or false."})
                    s["ctx_auto"] = b["ctx_auto"]
                if s["backend"] not in ("auto", "vulkan", "hip", "cuda", "cpu"):
                    return self._send(400, {"error": "unknown backend"})
                s["ctx"] = max(256, min(int(s["ctx"]), 1 << 20))
                for k in ("gpu_layers", "threads", "memory_limit_mb", "idle_unload_s"):
                    s[k] = int(s[k])
                    if s[k] < (-1 if k == "gpu_layers" else 0):
                        return self._send(400, {"error": f"Invalid {k}: use a nonnegative value (GPU layers also accepts -1 for automatic)."})
                if "extra_model_dirs" in b:
                    dirs = []
                    for d in b["extra_model_dirs"]:
                        d = os.path.realpath(d)
                        if not os.path.isdir(d):
                            return self._send(400, {"error": f"Not a directory: {d}"})
                        dirs.append(d)
                    s["extra_model_dirs"] = dirs
                if s["model"] and s["model"] not in [m["path"] for m in tc.list_models(s["extra_model_dirs"])]:
                    return self._send(400, {"error": "Model must be inside a configured model directory."})
                tc.save_settings(s)
                return self._send(200, {"ok": True})
            if p == "/api/keys":
                return self._send(*APP.keys_action(b))


            if p == "/api/backups":
                return self._send(*APP.backups_action(b))


            if p == "/api/docs":
                return self._send(*APP.docs_action(b))


            if p == "/api/pccheck/speed":
                r = APP.speed_test()
                return self._send(400 if "error" in r else 200, r)


            if p == "/api/library":
                return self._send(*APP.library_action(b))
            if p == "/api/fleet/action":
                return self._send(*APP.fleet_action(b))
            if p == "/api/fleet/terminal":
                return self._send(*APP.fleet_terminal(b))
            if p == "/api/fleet/draft":
                return self._send(*APP.fleet_draft(b))
            if p == "/api/fleet/drafts":
                return self._send(200, APP.fleet(["message", "list"]))
            if p == "/api/llm/start":
                return self._send(200, APP.start_llm(tc.load_settings()))
            if p == "/api/llm/stop":
                APP.stop_llm()
                return self._send(200, {"ok": True})
            if p.startswith("/api/service/"):
                _, _, _, name, act = p.split("/")
                if name not in SERVICES or act not in ("start", "stop"):
                    return self._send(400, {"error": "unknown service/action"})
                return self._send(200, self.service(name, act, b))
            if p == "/api/import":
                return self._send(200, self.start_import(b))
            if p == "/api/storage/select":
                rc, out = APP.priv(["persist-mount", str(b.get("uuid", ""))])
                if rc == 0:
                    APP.priv(["dest-apply"])
                    tc.link_destinations()
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/storage/dest":
                try:
                    return self._send(200, tc.storage_use(str(b.get("role", "")), str(b.get("drive", "")), str(b.get("folder", "")),
                                                          bool(b.get("readonly"))))
                except tc.vs.StorageError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
            if p == "/api/storage/dest/clear":
                try:
                    return self._send(200, tc.storage_clear(str(b.get("role", "")), b.get("uuid") or None))
                except tc.vs.StorageError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
            if p in ("/api/storage/mount", "/api/storage/eject"):
                args = ["volume-mount", str(b.get("uuid", "")), "ro" if b.get("readonly") else "rw"] if p.endswith("mount") \
                    else ["volume-umount", str(b.get("uuid", ""))]
                rc, out = APP.priv(args)
                tc._VOL_CACHE["t"] = 0.0
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/storage/temp":
                rc, out = APP.priv(["persist-temp"])
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/storage/mount-ro":
                rc, out = APP.priv(["mount-ro", str(b.get("uuid", ""))])
                if rc == 0:
                    s = tc.load_settings()
                    if out not in s["extra_model_dirs"]:
                        s["extra_model_dirs"].append(out)
                        tc.save_settings(s)
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/remote":
                return self._send(200, APP.set_remote(b))
            if p == "/api/remote/new-key":
                return self._send(200, {"ok": True, "api_key": APP.new_api_key(),
                                        "message": "Copy this key now - it is shown only once."})
            if p == "/api/remote/revoke":
                return self._send(200, APP.revoke_remote())
            if p == "/api/remote/tls-cert":
                host = str(b.get("host", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
                    return self._send(400, {"error": "Enter a hostname or IP address."})
                r = subprocess.run([os.path.join(HERE, "..", "..", "bin", "victor-tls-cert"), host], capture_output=True, text=True)
                return self._send(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "message": r.stdout.strip() or r.stderr.strip()})
            if p == "/api/input":
                code, payload = APP.input_action(b)
                return self._send(code, payload)
            if p == "/api/mic":
                code, payload = APP.mic_action(b)
                return self._send(code, payload)
            if p == "/api/bluetooth":
                code, payload = APP.bluetooth_action(b)
                return self._send(code, payload)
            if p == "/api/mesh":
                code, payload = APP.mesh_action(b)
                return self._send(code, payload)
            if p == "/api/ssh":
                act = b.get("action")
                if act == "add-key":
                    rc, out = APP.priv(["ssh-add-key"], stdin=str(b.get("key", "")) + "\n")
                elif act in ("enable", "disable", "reset"):
                    args = ["ssh-" + act] + ([str(b.get("listen", "127.0.0.1"))] if act == "enable" else [])
                    rc, out = APP.priv(args)
                else:
                    return self._send(400, {"error": "unknown ssh action"})
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/capcheck":
                r = subprocess.run([os.path.join(HERE, "..", "..", "bin", "victor-capcheck")], capture_output=True, text=True, timeout=300)
                return self._send(200, {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-4000:]})
            if p == "/api/power":
                act = b.get("action")
                if act not in ("reboot", "poweroff"):
                    return self._send(400, {"error": "bad action"})
                rc, out = APP.priv(["power", act])
                return self._send(200, {"ok": rc == 0, "message": out})
        except tc.GGUFError as e:
            return self._send(400, {"error": f"Invalid GGUF: {e}"})
        except (KeyError, ValueError, RuntimeError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def service(self, name, act, b):
        d = SERVICES[name]
        pr = APP.svc[name]
        if act == "stop":
            APP.stop(pr)
            return {"ok": True}
        if pr.running():
            return {"ok": True, "message": "Already running."}
        if tc.data_root() is None:
            return {"ok": False, "error": "Choose storage first."}
        if "dir" in d:
            cwd = os.path.join(HOME, d["dir"])
            if not os.path.isdir(cwd):
                return {"ok": False, "error": f"{d['label']} is not installed in this image (build it with the matching pack)."}
            cmd = [os.path.join(cwd, d["cmd"][0])] + d["cmd"][1:]
        else:
            cwd = HOME
            cmd = [shutil.which(d["bin"]) or d["bin"]] + d["cmd"][1:]
        # VRAM guard: do not silently start a second GPU consumer on top of a loaded LLM
        if d["vram_hint_gib"] and APP.llm.running() and not b.get("force"):
            for g in tc.gpus():
                if g.get("vram_total"):
                    free = (g["vram_total"] - (g.get("vram_used") or 0)) / 2**30
                    if free < d["vram_hint_gib"]:
                        return {"ok": False, "needs_force": True,
                                "error": f"Only {free:.1f} GiB VRAM free while the LLM is loaded; {d['label']} typically needs ~{d['vram_hint_gib']} GiB. "
                                         "Stop the LLM first, or start anyway."}
        # image tools keep their data on the persistent store
        env = {"VICTOR_DATA": tc.data_root()}
        try:
            APP.spawn(pr, cmd, cwd=cwd, env=env)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        time.sleep(1.0)
        if not pr.running():
            return {"ok": False, "error": "Exited immediately.", "log": APP.tail(name, 30)}
        return {"ok": True}

    def start_import(self, b):
        job = APP.import_job
        if job.get("state") == "running":
            return {"ok": False, "error": "An import is already running."}
        src = str(b.get("src", ""))
        if b.get("destination") == "storage":          # a models library chosen in the Storage tab (any drive)
            dest = tc.dest_dir("models")
            if not dest:
                return {"ok": False, "error": "The chosen models drive is not connected or not writable. Open the Storage tab."}
        else:
            dest = tc.data_path("models")
        if not dest:
            return {"ok": False, "error": "Choose storage first."}
        sha = (b.get("sha256") or "").strip() or None

        def run():
            def prog(d, t):
                job.update(done=d, total=t)
            job.clear()
            job.update(state="running", src=src, done=0, total=0)
            try:
                path, digest = tc.import_model(src, dest, sha, prog)
                job.update(state="done", path=path, sha256=digest, verified=bool(sha))
            except (ValueError, OSError, tc.GGUFError) as e:
                job.update(state="error", error=str(e))
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global APP
    APP = App()
    srv = Server(("127.0.0.1", CC_PORT), Handler)
    print(f"VictorLinux Control Center on http://127.0.0.1:{CC_PORT}/ (token in {TOKEN_FILE})", flush=True)

    def bye(*_):
        APP.stop_llm()
        APP.stop(APP.emb)
        for p in APP.svc.values():
            APP.stop(p)
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        bye()


if __name__ == "__main__":
    main()
