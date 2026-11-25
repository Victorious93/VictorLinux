#!/usr/bin/env bash
# VictorLinux Raspberry Pi edition: build an arm64 SD-card image with mmdebstrap.
# Usage: sudo ./build.sh [--profile pi-desktop|pi-headless] [--workdir DIR] [--out DIR] [--no-llama] [--keep-rootfs]
# Needs: root, mmdebstrap, e2fsprogs (mke2fs -d), dosfstools, mtools, fdisk (sfdisk), xz-utils, gpg, ~12 GiB free.
# On a non-arm64 host also qemu-user-static + binfmt-support (arm64 binfmt registered). Network at BUILD time only.
set -Eeuo pipefail
trap 'echo "pi build.sh: FAILED at line $LINENO (exit $?)" >&2' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEB="$REPO/debian-edition"
PROFILE=pi-headless; WORKDIR=/var/tmp/victorlinux-pi-build; OUT="$REPO/out/pi"; NO_LLAMA=0; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --no-llama) NO_LLAMA=1; shift;;
    --keep-rootfs) KEEP=1; shift;;
    -h|--help) sed -n '2,5p' "${BASH_SOURCE[0]}"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
[ -f "$HERE/profiles/$PROFILE.env" ] || { echo "profile must be pi-desktop or pi-headless" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "must run as root (mmdebstrap root mode, chroot hooks)" >&2; exit 1; }

_mirror_override="${DEBIAN_MIRROR:-}"; _sec_override="${DEBIAN_SECURITY_MIRROR:-}"
set -a
# shellcheck source=../debian-edition/versions.env
. "$DEB/versions.env"
# shellcheck source=versions.env
. "$HERE/versions.env"
# shellcheck source=profiles/pi-headless.env
. "$HERE/profiles/$PROFILE.env"
set +a
[ -n "$_mirror_override" ] && DEBIAN_MIRROR="$_mirror_override"
[ -n "$_sec_override" ] && DEBIAN_SECURITY_MIRROR="$_sec_override"
[ "$NO_LLAMA" -eq 1 ] && PI_LLAMACPP=0

for t in mmdebstrap mke2fs mkfs.vfat mcopy sfdisk xz gpg sha256sum; do
  command -v "$t" >/dev/null || { echo "missing prerequisite: $t (apt install mmdebstrap e2fsprogs dosfstools mtools fdisk xz-utils gpg)" >&2; exit 1; }
done
if [ "$(dpkg --print-architecture 2>/dev/null || uname -m)" != arm64 ] && [ "$(uname -m)" != aarch64 ]; then
  [ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || { echo "non-arm64 host: register qemu-aarch64 binfmt (apt install qemu-user-static binfmt-support)" >&2; exit 1; }
fi

# Verify the Raspberry Pi archive key we ship matches the pinned fingerprint before trusting it.
fpr="$(gpg --show-keys --with-colons "$HERE/keys/raspberrypi-archive.asc" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')"
[ "$fpr" = "$RPI_KEY_FPR" ] || { echo "Raspberry Pi archive key fingerprint mismatch: got '$fpr', want $RPI_KEY_FPR" >&2; exit 1; }

pkgs="$(cat "$HERE/profiles/common.list" "$HERE/profiles/$PROFILE.list" | sed 's/#.*//' | tr -s ' \t' '\n' | sed '/^$/d' | sort -u | paste -sd, -)"
[ -n "$pkgs" ] || { echo "empty package list" >&2; exit 1; }

mkdir -p "$WORKDIR" "$OUT"; OUT="$(cd "$OUT" && pwd)"
ROOTFS="$WORKDIR/rootfs"; STAGE="$WORKDIR/stage"
rm -rf "$ROOTFS" "$STAGE"; mkdir -p "$STAGE"
log="$OUT/build-$PROFILE-$(date -u +%Y%m%dT%H%M%SZ).log"
echo "profile: $PROFILE ($PI_PROFILE_DESC); log: $log"

# ---- stage: everything that goes into the image, assembled on the host first
T="$STAGE/tree"; mkdir -p "$T"
cp -a "$DEB/config/includes.chroot_after_packages/." "$T/"            # Debian edition victor-* tools, Tk Control Center, theme
"$REPO/fleet/install.sh" "$T"                                          # shared fleet coordinator
install -m 0644 "$REPO/airootfs/usr/local/lib/victorlinux/model_advisor.py" "$T/usr/local/lib/victorlinux/model_advisor.py"
cp -a "$HERE/overlay/." "$T/"                                          # Pi-specific files win
install -d "$T/usr/share/victorlinux"
cat "$DEB/versions.env" "$HERE/versions.env" > "$T/usr/share/victorlinux/versions.env"
printf '%s\n' "$PROFILE" > "$T/usr/share/victorlinux/profile"
printf 'edition=pi\nprofile=%s\nllamacpp=%s\ndesktop=%s\n' "$PROFILE" "$PI_LLAMACPP" "$PI_DESKTOP" > "$T/usr/share/victorlinux/pi-build.env"
if [ "$PI_DESKTOP" != 1 ]; then
  # headless: no desktop entries/autostarts/greeter config (the files are harmless but would mislead)
  rm -rf "$T/etc/skel/.config/autostart" "$T/etc/lightdm" "$T/usr/share/applications" "$T/usr/lib/firefox-esr"
fi
if [ -n "${EXTRA_CA_BUNDLE:-}" ]; then   # same opt-in as the Debian edition; removed by the cleanup hook
  install -m 0644 "$EXTRA_CA_BUNDLE" "$T/usr/share/victorlinux/build-ca.crt"
  { echo "export GIT_SSL_CAINFO=/usr/share/victorlinux/build-ca.crt SSL_CERT_FILE=/usr/share/victorlinux/build-ca.crt"
    [ -n "${BUILD_HTTPS_PROXY:-}" ] && echo "export https_proxy='$BUILD_HTTPS_PROXY' HTTPS_PROXY='$BUILD_HTTPS_PROXY'"
    true; } > "$T/usr/share/victorlinux/build-env"; chmod 0600 "$T/usr/share/victorlinux/build-env"
fi
# chroot hooks, in order. Shared hooks are reused from the Debian edition, never copied into this tree.
H="$STAGE/hooks"; mkdir -p "$H"
cp "$HERE/hooks/10-pi-config.sh" "$H/10-pi-config.sh"
[ "$PI_LLAMACPP" = 1 ] && cp "$HERE/hooks/20-llamacpp-cpu.sh" "$H/20-llamacpp-cpu.sh"
cp "$DEB/config/hooks/live/0250-optional-scrcpy.hook.chroot" "$H/30-optional-scrcpy.sh"
cp "$DEB/config/hooks/live/0260-optional-tailscale.hook.chroot" "$H/35-optional-tailscale.sh"   # Victorious Mesh (off by default)
cp "$HERE/hooks/90-pi-verify.sh" "$H/90-pi-verify.sh"
cp "$DEB/config/hooks/live/9999-cleanup.hook.chroot" "$H/99-cleanup.sh"
chmod 0755 "$H"/*.sh
for f in "$H"/*.sh; do bash -n "$f"; done

DSRC="main contrib non-free non-free-firmware"
# apt on the build host (mmdebstrap) needs both archive keys; the Pi key is also installed into the image for updates.
gpg --dearmor < "$HERE/keys/raspberrypi-archive.asc" > "$STAGE/raspberrypi-archive.gpg"
DEB_KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$DEB_KEYRING" ] || { echo "missing $DEB_KEYRING (apt install debian-archive-keyring)" >&2; exit 1; }
{
  mmdebstrap --mode=root --variant=apt --architectures="$PI_ARCH" \
    --components="$DSRC" --include="$pkgs" \
    --keyring="$DEB_KEYRING" --keyring="$STAGE/raspberrypi-archive.gpg" \
    --aptopt='APT::Install-Recommends "false"' --dpkgopt='path-exclude=/usr/share/doc/*' --dpkgopt='path-include=/usr/share/doc/*/copyright' \
    --setup-hook='mkdir -p "$1/etc/apt/trusted.gpg.d" "$1/etc/apt/preferences.d" "$1/boot/firmware"' \
    --setup-hook="cp '$HERE/keys/raspberrypi-archive.asc' \"\$1/etc/apt/trusted.gpg.d/raspberrypi-archive.asc\"" \
    --setup-hook="cp '$HERE/apt/victor-rpi.pref' \"\$1/etc/apt/preferences.d/victor-rpi.pref\"" \
    --customize-hook='if [ -L "$1/usr/lib/firefox-esr/distribution" ]; then rm "$1/usr/lib/firefox-esr/distribution"; fi' \
    --customize-hook="tar -C '$T' -cf - . | tar -C \"\$1\" -xpf - --keep-directory-symlink --no-overwrite-dir" \
    --customize-hook="mkdir -p \"\$1/tmp/victor-hooks\" && cp -a '$H/.' \"\$1/tmp/victor-hooks/\"" \
    --customize-hook='cp /etc/resolv.conf "$1/etc/resolv.conf" 2>/dev/null || true' \
    --customize-hook='chroot "$1" apt-get update' \
    --customize-hook='for h in "$1"/tmp/victor-hooks/*.sh; do echo "== hook $(basename "$h")"; chroot "$1" /bin/bash "/tmp/victor-hooks/$(basename "$h")" || exit 1; done' \
    --customize-hook='rm -rf "$1/tmp/victor-hooks"; rm -f "$1/etc/resolv.conf"; ln -s ../run/NetworkManager/resolv.conf "$1/etc/resolv.conf"' \
    "$DEBIAN_SUITE" "$ROOTFS" \
    "deb $DEBIAN_MIRROR $DEBIAN_SUITE $DSRC" \
    "deb $DEBIAN_MIRROR $DEBIAN_SUITE-updates $DSRC" \
    "deb $DEBIAN_SECURITY_MIRROR $DEBIAN_SUITE-security $DSRC" \
    "deb $RPI_ARCHIVE $RPI_SUITE $RPI_COMPONENT"
} 2>&1 | tee "$log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "mmdebstrap failed; see $log" >&2; exit 1; }

stamp="$(date -u +%Y%m%d)"
img="$OUT/${PI_IMAGE_NAME}-${PROFILE}-${DEBIAN_SUITE}-arm64-${stamp}.img"
"$HERE/scripts/mkimage.sh" "$ROOTFS" "$img" "$PI_BOOT_MIB" "$PI_DATA_MIB" 2>&1 | tee -a "$log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "mkimage failed; see $log" >&2; exit 1; }
"$HERE/scripts/gen-manifest.sh" "$ROOTFS" "$img" "$PROFILE" > "$img.manifest.json"
[ "$KEEP" -eq 1 ] || rm -rf "$ROOTFS"
xz -T0 -6 -f "$img"
( cd "$OUT" && sha256sum "$(basename "$img").xz" > "$(basename "$img").xz.sha256" )
echo "IMAGE:    $img.xz"; cat "$img.xz.sha256"; echo "MANIFEST: $img.manifest.json"
