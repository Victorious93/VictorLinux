#!/usr/bin/env bash
# Runs INSIDE an Arch Linux environment (container, chroot or native) as root.
# usage: build-inner.sh PLATFORM TYPE [--packs a,b] ; env: WORKDIR (default /var/tmp/victor-work), OUTDIR (default ./out)
# Requires network for pacman (build time only). Produces: ISO, .sha256, manifest.json, build.log, pkglist.
set -euo pipefail
cd "$(dirname "$0")/.."
root=$PWD
WORKDIR=${WORKDIR:-/var/tmp/victor-work}
OUTDIR=${OUTDIR:-$root/out}
MAX_TRIES=${MAX_TRIES:-3}

pacman -S --noconfirm --needed archiso python python-jinja git jq >/dev/null

# clean rendered files from any earlier run, then render for this profile
python3 .ci/configure.py "$@" | tee /tmp/configure.out
profile=$(sed -n 's/^PROFILE //p' /tmp/configure.out)

[ -f airootfs/root/sources/llama.cpp.rev ] || { echo "run scripts/prepare-sources.sh first" >&2; exit 1; }
if grep -q 'type=empty' <<<"$profile"; then rm -rf airootfs/root/sources; fi

# fleet coordinator: shared implementation (fleet/), installed into the profile tree (git-ignored there)
fleet/install.sh airootfs

# optional, OFF by default: build-only compiler cache (VICTOR_SCCACHE=1). Staged into the git-ignored profile tree; the squashfs excludes it.
scripts/sccache-stage.sh stage airootfs
trap 'rm -rf airootfs/usr/local/lib/victor-build airootfs/var/cache/victor-sccache' EXIT

# never ship stray bytecode caches from local test runs
find airootfs -name __pycache__ -type d -prune -exec rm -rf {} +

mkdir -p "$OUTDIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log="$OUTDIR/build-$stamp.log"
status=1
for ((i=1; i<=MAX_TRIES; i++)); do
  rm -rf "$WORKDIR"
  if mkarchiso -v -m iso -w "$WORKDIR" -o "$OUTDIR" "$root" 2>&1 | tee -a "$log"; then
    status=0; break
  fi
  echo "Build attempt $i/$MAX_TRIES failed" | tee -a "$log"
  # archive.archlinux.org throttles bursts (esp. CI runner IPs); back off before retrying
  if [ "$i" -lt "$MAX_TRIES" ]; then sleep $((i * ${RETRY_BACKOFF:-60})); fi
done
[ "$status" -eq 0 ] || { echo "BUILD FAILED after $MAX_TRIES attempts; see $log" >&2; exit 1; }
# harvest the compiler cache from the work dir BEFORE it is deleted (only reached on success: the work dir is wiped on every retry)
scripts/sccache-stage.sh harvest "$WORKDIR/x86_64/airootfs" || true
rm -rf "$WORKDIR"

iso=$(ls -t "$OUTDIR"/*.iso | head -n1)
( cd "$OUTDIR" && sha256sum "$(basename "$iso")" > "$(basename "$iso").sha256" )

# --- manifest ---------------------------------------------------------------------------
install_dir=$(sed -n 's/^install_dir="\(.*\)"/\1/p' profiledef.sh)
xorriso -osirrox on -indev "$iso" -extract "/$install_dir/pkglist.x86_64.txt" "$OUTDIR/packages-$stamp.txt" >/dev/null 2>&1 || true
python3 - "$iso" "$profile" "$stamp" "$OUTDIR" <<'PY'
import configparser, hashlib, json, os, subprocess, sys
iso, profile, stamp, out = sys.argv[1:5]
def sh(*a):
    try: return subprocess.run(a, capture_output=True, text=True).stdout.strip()
    except OSError: return None
c = configparser.ConfigParser(); c.read(".ci/options.ini")
pk = {}
pl = os.path.join(out, f"packages-{stamp}.txt")
if os.path.exists(pl):
    for line in open(pl):
        p = line.split()
        if len(p) == 2: pk[p[0]] = p[1]
gpu_stack = {k: pk.get(k) for k in ("linux", "linux-firmware", "mesa", "vulkan-radeon", "vulkan-icd-loader", "vulkan-tools",
                                    "shaderc", "rocm-hip-sdk", "cuda", "nvidia", "nvidia-utils") if pk.get(k)}
rev = open("airootfs/root/sources/llama.cpp.rev").read().strip() if os.path.exists("airootfs/root/sources/llama.cpp.rev") else None
m = {
  "built_utc": stamp, "profile": profile, "iso": os.path.basename(iso), "iso_bytes": os.path.getsize(iso),
  "iso_sha256": hashlib.sha256(open(iso, "rb").read()).hexdigest() if os.path.getsize(iso) < (4 << 30) else sh("sha256sum", iso).split()[0],
  "git": {"commit": sh("git", "rev-parse", "HEAD"), "dirty": bool(sh("git", "status", "--porcelain")), "branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD")},
  "pins": {s: dict(c.items(s)) for s in ("revisions", "build")},
  "llama_cpp_commit": rev,
  "nvidia_pstated_sha256": open("airootfs/root/sources/nvidia-pstated.sha256").read().strip() if os.path.exists("airootfs/root/sources/nvidia-pstated.sha256") else None,
  "llama_ui_bundle_sha256": open("airootfs/root/sources/llama.cpp.ui.sha256").read().strip() if os.path.exists("airootfs/root/sources/llama.cpp.ui.sha256") else None,
  "gpu_stack_packages": gpu_stack, "package_count": len(pk),
  "build_env": {"archiso": pk.get("archiso") or sh("pacman", "-Q", "archiso"), "host_kernel": sh("uname", "-r"),
                "host_pacman": sh("pacman", "-Q", "pacman"), "python": sh("python3", "--version")},
  "network": {"build_time": "pacman (Arch Linux Archive snapshot) + pinned git sources", "boot_time": "none required"},
}
json.dump(m, open(os.path.join(out, f"manifest-{stamp}.json"), "w"), indent=2)
print(json.dumps({k: m[k] for k in ("iso", "iso_bytes", "iso_sha256", "profile")}, indent=2))
PY
echo "Artifacts in $OUTDIR:"; ls -la "$OUTDIR"
