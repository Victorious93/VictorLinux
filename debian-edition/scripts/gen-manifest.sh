#!/usr/bin/env bash
# Emit a JSON build manifest: ISO identity, pinned sources, kernel/firmware/driver/runtime versions, build env.
# Usage: gen-manifest.sh WORKDIR ISO PROFILE LOGFILE > manifest.json
set -Eeuo pipefail
W="$1"; ISO="$2"; PROFILE="$3"; LOG="$4"; HERE="$(cd "$(dirname "$0")/.." && pwd)"
set -a; . "$HERE/versions.env"; set +a
CH="$W/chroot"
facts() { [ -f "$CH/usr/share/victorlinux/$1" ] && cat "$CH/usr/share/victorlinux/$1" || true; }
python3 -I - "$ISO" "$PROFILE" "$LOG" "$CH" "$HERE" <<'PY'
import hashlib, json, os, platform, subprocess, sys, time
iso, profile, log, ch, here = sys.argv[1:6]
def kv(text):
    return dict(l.strip().split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
def rd(p):
    try: return open(p).read()
    except OSError: return ""
h = hashlib.sha256()
with open(iso, "rb") as f:
    while c := f.read(1 << 24): h.update(c)
pk = [l.split("\t") for l in rd(f"{ch}/usr/share/victorlinux/packages.tsv").splitlines() if l]
def sh(c):
    try: return subprocess.run(c, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception: return "unknown"
repo = os.path.dirname(here)
commit = sh(["git", "-c", f"safe.directory={repo}", "-C", repo, "rev-parse", "HEAD"])
m = {
 "image": "VictorLinux (Debian edition)", "maintainer": "Victorious / Victorious Framework",
 "git": {"commit": commit or "unknown"},
 "upstream": "Derived from ToriLinux by sasha0552 (Arch-based); Debian edition is a separate live-build implementation",
 "iso": {"file": os.path.basename(iso), "bytes": os.path.getsize(iso), "sha256": h.hexdigest()},
 "profile": profile, "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
 "pins": kv(rd(f"{here}/versions.env").replace("\t", " ").split("#")[0] if False else "\n".join(l.split("#")[0].strip() for l in rd(f"{here}/versions.env").splitlines())),
 "image_facts": kv(rd(f"{ch}/usr/share/victorlinux/image-facts")),
 "llama_cpp_build": kv(rd(f"{ch}/usr/share/victorlinux/llamacpp.build-info")),
 "package_count": len(pk), "packages": [{"name": a[0], "version": a[1], "arch": a[2]} for a in pk if len(a) == 3],
 "build_environment": {"host": platform.platform(), "live_build": sh(["dpkg-query", "-W", "-f", "${Version}", "live-build"]),
                       "debootstrap": sh(["dpkg-query", "-W", "-f", "${Version}", "debootstrap"]), "python": platform.python_version(),
                       "log": os.path.basename(log)},
 "notes": ["Models are NOT included; supply GGUF files separately.", "Build-time internet was used; the image runs fully offline.",
           "ROCm/HIP is NOT in this image (see docs/GPU.md); Vulkan (Mesa RADV) is the AMD path."],
}
print(json.dumps(m, indent=1))
PY
