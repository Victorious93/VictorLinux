"""Offline image generation with a user-supplied stable-diffusion.cpp program (both editions; pure helpers + one runner).

OFF by default. Nothing is bundled or downloaded: the owner copies a stable-diffusion.cpp build (`sd-cli` / `sd`) and a
model file (*.gguf, *.safetensors, *.ckpt) into the image-models folder (images/models on the data partition) and turns the feature on.
Prompts and images never leave the PC. Pictures are saved under images/out on the data partition.
"""
import json
import os
import re
import shutil
import subprocess
import time

BIN_NAMES = ("sd-cli", "sd", "stable-diffusion")
MODEL_EXT = (".gguf", ".safetensors", ".ckpt")
SAMPLERS = ("euler_a", "euler", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "lcm")
DEFAULTS = {"enabled": False, "bin": "", "model": "", "threads": 0}
JOB_DEFAULTS = {"prompt": "", "negative": "", "width": 512, "height": 512, "steps": 20, "cfg": 7.0, "seed": -1,
                "sampler": "euler_a"}
MAX_PROMPT = 1000
TIMEOUT_S = 1800


class ImageGenError(Exception):
    pass


def load(path):
    c = dict(DEFAULTS)
    try:
        with open(path) as f:
            c.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return c


def save(path, c):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: c[k] for k in DEFAULTS}, f, indent=1)
    os.replace(tmp, path)


def find_bin(configured):
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    for n in BIN_NAMES:
        p = shutil.which(n)
        if p:
            return p
    return None


def model_files(folder):
    out = []
    if folder and os.path.isdir(folder):
        for root, _dn, files in os.walk(folder):
            out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(MODEL_EXT)]
    return out


def update(c, b, files):
    """Apply a settings change: model must be one of the listed files, program must be an executable full path."""
    n = dict(c)
    if "enabled" in b:
        if not isinstance(b["enabled"], bool):
            raise ImageGenError("enabled must be true or false.")
        n["enabled"] = b["enabled"]
    if "model" in b:
        v = str(b["model"] or "")
        if v and v not in files:
            raise ImageGenError("Pick a model file from the image-models folder.")
        n["model"] = v
    if "bin" in b:
        v = str(b["bin"] or "").strip()
        if v and not (os.path.isabs(v) and os.path.isfile(v) and os.access(v, os.X_OK)):
            raise ImageGenError(f"{v} is not an executable program (use a full path).")
        n["bin"] = v
    if "threads" in b:
        v = b["threads"]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 256:
            raise ImageGenError("Threads must be 0 (automatic) to 256.")
        n["threads"] = v
    return n


def status(c, files):
    b = find_bin(c["bin"])
    missing = []
    if not b:
        missing.append("stable-diffusion.cpp program (sd-cli or sd) not found")
    if not c["model"]:
        missing.append("no image model selected")
    elif not os.path.isfile(c["model"]):
        missing.append("the selected model file is gone")
    ready = bool(c["enabled"] and b and c["model"] and os.path.isfile(c["model"]))
    return {"enabled": c["enabled"], "ready": ready, "bin": b, "model": c["model"], "missing": missing, "files": files}


def validate_job(j):
    n = dict(JOB_DEFAULTS)
    n.update({k: j[k] for k in JOB_DEFAULTS if k in j})
    for k in ("prompt", "negative"):
        v = " ".join(str(n[k] or "").split())
        if len(v) > MAX_PROMPT:
            raise ImageGenError(f"The {k} text is longer than {MAX_PROMPT} characters.")
        n[k] = v
    if not n["prompt"]:
        raise ImageGenError("Describe the picture first.")
    for k in ("width", "height"):
        v = n[k]
        if not isinstance(v, int) or isinstance(v, bool) or not 256 <= v <= 1024 or v % 64:
            raise ImageGenError("Width and height must be 256 to 1024 in steps of 64.")
    if not isinstance(n["steps"], int) or isinstance(n["steps"], bool) or not 1 <= n["steps"] <= 60:
        raise ImageGenError("Steps must be 1 to 60.")
    try:
        n["cfg"] = float(n["cfg"])
    except (TypeError, ValueError):
        raise ImageGenError("Guidance must be a number.")
    if not 1.0 <= n["cfg"] <= 20.0:
        raise ImageGenError("Guidance must be 1 to 20.")
    if not isinstance(n["seed"], int) or isinstance(n["seed"], bool) or not -1 <= n["seed"] < 2 ** 31:
        raise ImageGenError("Seed must be -1 (random) or a whole number.")
    if n["sampler"] not in SAMPLERS:
        raise ImageGenError("Unknown sampler.")
    return n


def output_name(now=None):
    return time.strftime("img-%Y%m%d-%H%M%S", time.localtime(now)) + ".png"


def command(binary, model, job, out_png, threads=0):
    argv = [binary, "-m", model, "-p", job["prompt"], "-W", str(job["width"]), "-H", str(job["height"]),
            "--steps", str(job["steps"]), "--cfg-scale", f"{job['cfg']:g}", "-s", str(job["seed"]),
            "--sampling-method", job["sampler"], "-o", out_png]
    if job["negative"]:
        argv[5:5] = ["-n", job["negative"]]
    if threads:
        argv += ["-t", str(threads)]
    return argv


STEP_RE = re.compile(r"(\d+)/(\d+)")


def progress(line):
    """Best-effort step counter from stable-diffusion.cpp's progress output ('  12/20 - 1.2s/it')."""
    m = None
    for m in STEP_RE.finditer(line):
        pass
    if m and 0 < int(m.group(2)) <= 1000 and int(m.group(1)) <= int(m.group(2)):
        return int(m.group(1)), int(m.group(2))
    return None


def generate(c, job, out_dir, popen=subprocess.Popen, on_progress=None, now=None):
    """Run one generation. Returns the PNG path. Raises ImageGenError with a short reason."""
    b = find_bin(c["bin"])
    if not c["enabled"]:
        raise ImageGenError("Image generation is off. Turn it on first.")
    if not b or not c["model"] or not os.path.isfile(c["model"]):
        raise ImageGenError("Image generation is not set up (stable-diffusion.cpp program and model).")
    job = validate_job(job)
    if not out_dir:
        raise ImageGenError("Set up storage first so the picture can be saved.")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, output_name(now))
    i = 1
    while os.path.exists(out):
        out = os.path.join(out_dir, output_name(now)[:-4] + f"-{i}.png"); i += 1
    p = popen(command(b, c["model"], job, out, int(c.get("threads") or 0)), stdout=subprocess.PIPE,
              stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    start = time.time()
    for line in p.stdout:
        tail = (tail + [line.strip()])[-5:]
        pr = progress(line)
        if pr and on_progress:
            on_progress(*pr)
        if time.time() - start > TIMEOUT_S:
            p.kill()
            raise ImageGenError("Stopped: the picture took longer than 30 minutes.")
    rc = p.wait()
    if rc != 0 or not os.path.isfile(out):
        last = next((t for t in reversed(tail) if t), f"exit {rc}")
        raise ImageGenError("stable-diffusion.cpp failed: " + last[:200])
    with open(out, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ImageGenError("stable-diffusion.cpp did not write a PNG.")
    return out
