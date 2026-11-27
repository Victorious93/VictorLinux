#!/usr/bin/env bash
# JSON manifest for a Pi image: identity, pins, board support levels, package versions.
# Usage: gen-manifest.sh ROOTFS IMG PROFILE > manifest.json
set -Eeuo pipefail
R="$1"; IMG="$2"; PROFILE="$3"; HERE="$(cd "$(dirname "$0")/.." && pwd)"
python3 -I - "$R" "$IMG" "$PROFILE" "$HERE" <<'PY'
import hashlib, json, os, platform, subprocess, sys, time
r, img, profile, here = sys.argv[1:5]
def rd(p):
    try: return open(p).read()
    except OSError: return ""
def kv(t):
    return dict(l.split("#")[0].strip().split("=", 1) for l in t.splitlines() if "=" in l.split("#")[0])
h = hashlib.sha256()
with open(img, "rb") as f:
    while c := f.read(1 << 24): h.update(c)
pk = [l.split("\t") for l in rd(f"{r}/usr/share/victorlinux/packages.tsv").splitlines() if l]
repo = os.path.dirname(here)
try:
    commit = subprocess.check_output(["git", "-c", f"safe.directory={repo}", "-C", repo, "rev-parse", "HEAD"],
                                     text=True, stderr=subprocess.DEVNULL, timeout=20).strip()
except (OSError, subprocess.SubprocessError):
    commit = "unknown"
print(json.dumps({
  "image": "VictorLinux (Raspberry Pi edition)", "profile": profile,
  "git": {"commit": commit},
  "img": {"file": os.path.basename(img), "bytes": os.path.getsize(img), "sha256_uncompressed": h.hexdigest()},
  "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "pins": {**kv(rd(f"{here}/../debian-edition/versions.env")), **kv(rd(f"{here}/versions.env"))},
  "llama_cpp_build": kv(rd(f"{r}/usr/share/victorlinux/llamacpp.build-info")),
  "image_facts": kv(rd(f"{r}/usr/share/victorlinux/image-facts")),
  "support": {"pi5": "Likely (never booted)", "pi4": "Likely (never booted)",
              "zero2w": "Likely, pi-headless only, node mode, no LLM (never booted)"},
  "package_count": len(pk), "packages": [{"name": a[0], "version": a[1], "arch": a[2]} for a in pk if len(a) == 3],
  "build_host": platform.platform(),
  "notes": ["No models included.", "Build-time internet only; no runtime downloads.",
            "Pi firmware/kernels/BT+Wi-Fi firmware from archive.raspberrypi.com (key fingerprint pinned)."],
}, indent=1))
PY
