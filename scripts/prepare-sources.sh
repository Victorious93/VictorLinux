#!/usr/bin/env bash
# Fetch pinned third-party sources BEFORE the image build (build-time internet needed here, not at boot).
# Verifies the llama.cpp tag still resolves to the pinned commit and records the SHA for the manifest.
set -euo pipefail
cd "$(dirname "$0")/.."

opt() { python3 - "$1" "$2" <<'PY'
import configparser, sys
c = configparser.ConfigParser(); c.read(".ci/options.ini"); print(c[sys.argv[1]][sys.argv[2]])
PY
}

dest=airootfs/root/sources
rm -rf "$dest"; mkdir -p "$dest"
repo=$(opt repositories llamacpp); tag=$(opt revisions llamacpp); want=$(opt revisions llamacpp_commit)

echo "==> cloning llama.cpp $tag"
git clone --quiet --depth 1 --branch "$tag" "$repo.git" "$dest/llama.cpp"
got=$(git -C "$dest/llama.cpp" rev-parse HEAD)
if [ "$got" != "$want" ]; then
  echo "ERROR: tag $tag now resolves to $got but .ci/options.ini pins $want (tag moved?). Refusing to build." >&2
  exit 1
fi
echo "$got" > "$dest/llama.cpp.rev"
rm -rf "$dest/llama.cpp/.git"

# llama-server's embedded Web UI ("Open chat"). The llama.cpp build would otherwise download it from Hugging Face
# at compile time, which fails in a sandboxed chroot and leaves the server without a UI. Pre-fetch + verify here.
ui_base="https://huggingface.co/buckets/ggml-org/llama-ui/resolve/$tag"
ui_tmp=$(mktemp -d)
curl -fsSL -o "$ui_tmp/dist.tar.gz.sha256" "$ui_base/dist.tar.gz.sha256?download=true"
curl -fsSL -o "$ui_tmp/dist.tar.gz" "$ui_base/dist.tar.gz?download=true"
want_ui=$(awk '{print $1}' "$ui_tmp/dist.tar.gz.sha256")
got_ui=$(sha256sum "$ui_tmp/dist.tar.gz" | awk '{print $1}')
[ "$want_ui" = "$got_ui" ] || { echo "ERROR: llama UI bundle checksum mismatch" >&2; exit 1; }
# the checksum above comes from the same server as the bundle, so it only catches corruption: also compare with our own pin
pin_ui=$(opt checksums llama_ui)
[ "$pin_ui" = "$got_ui" ] || { echo "ERROR: llama UI bundle $got_ui does not match the pin in .ci/options.ini ($pin_ui)" >&2; exit 1; }
mkdir -p "$dest/llama.cpp/tools/ui/dist"
tar -xzf "$ui_tmp/dist.tar.gz" -C "$dest/llama.cpp/tools/ui/dist"
[ -f "$dest/llama.cpp/tools/ui/dist/index.html" ] || { echo "ERROR: UI bundle has no index.html" >&2; exit 1; }
echo "$got_ui" > "$dest/llama.cpp.ui.sha256"
rm -rf "$ui_tmp"
echo "llama UI bundle $got_ui"

# nvidia-pstated (CUDA platform only uses it): fetched here so the chroot needs no TLS/network access.
ps_url="$(opt repositories nvidia_pstated)/releases/download/$(opt revisions nvidia_pstated)/$(opt files nvidia_pstated)"
curl -fsSL -o "$dest/nvidia-pstated" "$ps_url"
pin_ps=$(opt checksums nvidia_pstated)
got_ps=$(sha256sum "$dest/nvidia-pstated" | awk '{print $1}')
[ "$pin_ps" = "$got_ps" ] || { rm -f "$dest/nvidia-pstated"; echo "ERROR: nvidia-pstated $got_ps does not match the pin in .ci/options.ini ($pin_ps)" >&2; exit 1; }
chmod +x "$dest/nvidia-pstated"
echo "$got_ps" > "$dest/nvidia-pstated.sha256"
echo "nvidia-pstated $(cat "$dest/nvidia-pstated.sha256")"
# RustDeskV (built into headless and gui images; skipped with VICTOR_RUSTDESKV=0). Fetched here so the chroot needs no network,
# and compared with the SHA-256 pinned in .ci/options.ini: a mismatch stops the build instead of shipping an unknown binary.
if [ "${VICTOR_RUSTDESKV:-1}" != "0" ]; then
  rd_ver=$(opt rustdeskv version); rd_file=$(opt rustdeskv arch_file)
  rd_url="$(opt rustdeskv repository)/releases/download/$rd_ver/$rd_file"
  mkdir -p "$dest/rustdeskv"
  curl -fsSL --proto '=https' --tlsv1.2 -o "$dest/rustdeskv/rustdeskv.pkg.tar.zst" "$rd_url"
  pin_rd=$(opt checksums rustdeskv_arch)
  got_rd=$(sha256sum "$dest/rustdeskv/rustdeskv.pkg.tar.zst" | awk '{print $1}')
  [ "$pin_rd" = "$got_rd" ] || { rm -rf "$dest/rustdeskv"; echo "ERROR: RustDeskV package $got_rd does not match the pin in .ci/options.ini ($pin_rd)" >&2; exit 1; }
  echo "$got_rd" > "$dest/rustdeskv/rustdeskv.sha256"
  echo "RustDeskV $rd_ver $got_rd"
else
  echo "RustDeskV skipped (VICTOR_RUSTDESKV=0)"
fi
echo "llama.cpp $tag = $got"
