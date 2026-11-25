#!/usr/bin/env bash
# Local build entry point (infrastructure you control; no secrets, no self-hosted runner).
#
#   scripts/build.sh [PLATFORM] [TYPE] [--packs a,b] [--no-rustdeskv]      e.g.  scripts/build.sh amd gui
#
# Needs: git, python3, and either Docker/Podman (recommended) or to be running as root on Arch Linux.
# Build-time internet is required (Arch package archive + GitHub for llama.cpp). The result boots offline.
set -euo pipefail
cd "$(dirname "$0")/.."
# --no-rustdeskv leaves the built-in RustDeskV out (same as VICTOR_RUSTDESKV=0)
args=(); for a in "$@"; do if [ "$a" = "--no-rustdeskv" ]; then export VICTOR_RUSTDESKV=0; else args+=("$a"); fi; done
[ ${#args[@]} -ge 2 ] || args=("${args[0]:-amd}" "headless")

scripts/prepare-sources.sh

if [ -f /etc/arch-release ] && [ "$(id -u)" -eq 0 ]; then
  exec scripts/build-inner.sh "${args[@]}"
fi
rt=$(command -v docker || command -v podman || true)
[ -n "$rt" ] || { echo "Install Docker or Podman, or run this as root on Arch Linux." >&2; exit 1; }
# --privileged: mkarchiso needs mount/chroot capabilities
exec "$rt" run --rm --privileged -e VICTOR_RUSTDESKV -v "$PWD:/workspace" -w /workspace archlinux:latest \
  bash -c 'pacman -Syu --noconfirm >/dev/null && scripts/build-inner.sh "$@"' _ "${args[@]}"
