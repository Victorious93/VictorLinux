# VictorLinux on Raspberry Pi

A Raspberry Pi edition built from the [Debian edition](../debian-edition/README.md): Debian 13 (trixie), arm64, with the
Raspberry Pi boot firmware, kernels and Wi-Fi/Bluetooth firmware from Raspberry Pi Ltd's apt archive. Same `victor-*`
tools, Tk Control Center, gateway, fleet coordinator and brand look (#0B0F1A / #00E5FF / #FF2BD6). English only.
No telemetry, no runtime downloads, no bundled models.

> **Status: never booted.** No image from this edition has been started on a Raspberry Pi or in an emulator.
> Support levels below are expectations, not results.

## Support levels

| Board | Profile | Role | Support |
|---|---|---|---|
| Raspberry Pi 5 (4/8/16 GB) | `pi-desktop`, `pi-headless` | AI box: llama.cpp CPU (NEON), gateway, GUI tools | **[Likely]** — never booted |
| Raspberry Pi 4 / 400 (2 GB+) | `pi-desktop`, `pi-headless` | AI box (small models) | **[Likely]** — never booted |
| Raspberry Pi 4 (1 GB) | `pi-headless` | Node (LLM disabled: under 1.8 GiB RAM) | **[Likely]** — never booted |
| Raspberry Pi Zero 2 W (512 MB) | `pi-headless` only | **Node only**: gateway/CLI, Bluetooth/Wi-Fi bridge, mesh node (`victor-mesh`). LLM serving disabled | **[Likely]** — never booted |
| Pi 3 and older, Pi Zero / Zero W (32-bit) | — | Not supported (LLM disabled if it boots at all) | **[Guessing]** may boot the v8 kernel on Pi 3; untested |

Why "node only" on the Zero 2 W: 512 MB RAM cannot hold even a 0.5B model plus the OS with useful context. At every boot
`victor-pi-board.service` reads `/proc/device-tree/model` and RAM and, on a Zero 2 W or any board under ~1.8 GiB, writes
`/run/victor/llm-disabled`; `victor-ctl llama start` then refuses with the reason. The check is per boot, so the same card
moved to a Pi 5 serves models again. **[Certain]** for the code path (unit-tested); **[Likely]** on hardware.

## Profiles

* **`pi-desktop`** (Pi 5/4): Xfce + LightDM autologin, the Debian edition's Tk Control Center (`victor-control`),
  Blueman for Bluetooth, Firefox ESR, phone tools, llama.cpp CPU build. First boot opens the setup screen.
* **`pi-headless`** (Pi 5/4/Zero 2 W): no desktop. `victor-ctl` (gateway/llama service/models/storage), `victor-fleet`,
  `victor-pi`, phone tools. First boot runs a console setup on the screen (tty1), or applies a setup file (below).

Both profiles carry llama.cpp, built from the **same pinned commit as the Debian edition** (`debian-edition/versions.env`)
with `GGML_NATIVE=OFF`: ARM CPU variants selected at runtime (dotprod/fp16 on the Pi 5's Cortex-A76), falling back to a
plain armv8-a NEON build if that commit cannot configure ARM variants. No Vulkan/GPU backend on the Pi. **[Likely]**

### Model suggestion (nothing is downloaded)

`victor-pi status` prints a suggestion; import the GGUF yourself with the Control Center or `victor-ctl model import`.

| Board | Suggestion | Size (Q4_K_M) |
|---|---|---|
| Pi 5, 8 GB+ | Llama 3.2 3B Instruct | ~2.0 GB |
| Pi 5 / Pi 4, 4 GB | Qwen2.5 1.5B Instruct | ~1.0 GB |
| Pi 4, 2 GB | Qwen2.5 0.5B Instruct (ctx 2048) | ~0.4 GB |

Expect a few tokens per second for 1-3B models on a Pi 5 and less on a Pi 4. **[Guessing]** — not measured.

## Wi-Fi and Bluetooth (onboard)

* **Wi-Fi**: `brcmfmac` with Raspberry Pi's `firmware-brcm80211` (43455 for Pi 5/4, 43430/43436 for Zero 2 W; the build
  fails if they are missing). Radios start soft-blocked (Raspberry Pi's `raspberrypi-sys-mods` default) and Wi-Fi is
  unblocked once a **Wi-Fi country** is set; the country is written to `cmdline.txt` (`cfg80211.ieee80211_regdom=`) and
  `/etc/modprobe.d`. Networks are managed by NetworkManager.
* **Bluetooth**: BlueZ + `bluez-firmware` + `pi-bluetooth` (UART attach helpers; `hciuart.service` is enabled if the
  package version still ships it). `bluetoothd` runs but the adapter is **not powered by default** (`AutoEnable=false`,
  rfkill blocked). Turn it on with `victor-pi bluetooth on` (or Blueman on the desktop).

## First-boot setup

There is no default password. The owner account is `victor` (the shared VictorLinux tools expect that login name, so the
setup screens set the **full name** but not a different login — a custom login name is a known limitation).

1. **Desktop:** a setup window (brand colors) asks for full name, password, Wi-Fi country, optional Wi-Fi network,
   optional SSH public keys and an "SSH on now (key-only)" checkbox.
2. **Headless with a screen:** the same questions on tty1 (`victor-pi-setup-tui`) before the login prompt.
3. **Headless without a screen (e.g. Zero 2 W):** copy `victor-firstboot.conf.example` on the FAT boot partition to
   `victor-firstboot.conf` and fill it in. Plain passwords are refused; use `PASSWORD_HASH` from `openssl passwd -6`.
   The file is applied once, then overwritten and deleted. If any line is invalid, nothing is applied and the reason is
   saved in `/var/lib/victorlinux/firstboot-errors.txt`.

Persistence: the root filesystem is a normal ext4 partition, so settings survive reboot. SSH keys are also copied to the
`VICTORDATA` data partition (`secrets/ssh_authorized_keys`) and restored at boot if the root is reflashed; the owner
password hash is kept there too (same mechanism as the Debian edition). SSH stays **off** unless you turn it on, and then
it is key-only (`victor-priv ssh-enable`).

## Optional hooks (all off by default)

`victor-pi hooks` lists them; `victor-pi hook NAME` runs one. Nothing starts at boot.

| Hook | State | Notes |
|---|---|---|
| `scrcpy` | **Reuses main** | `victor-phone` + the Debian edition's optional scrcpy hook (PR #9). scrcpy may be absent if trixie-backports lacks it for arm64 |
| `phone-keyboard-bt` | **TODO** | Victorious Keyboard as a Bluetooth keyboard/mouse. No VictorLinux pairing tool on main yet and the phone side is not built. Manual today: `victor-pi bluetooth on`, then `bluetoothctl` |
| `hotspot` | **TODO** | No hotspot tool on main yet; NetworkManager is in the image as the intended backend |
| `mesh` | **Reuses main** | `victor-mesh` ([Victorious Mesh](victorious-mesh.md)): join your Headscale, advertises `tag:victorlinux-pi`. tailscale comes from the optional build hook; off by default. Works on the Zero 2 W as a headless mesh node |

TODO hooks print `TODO(name)` and exit 3 without changing anything. When those tools land on main, the dispatcher
(`pi-edition/overlay/usr/local/lib/victorlinux/pi/hooks/run-hook`) should call them instead of duplicating them.

## Build

```bash
sudo apt install mmdebstrap e2fsprogs dosfstools mtools fdisk xz-utils gnupg   # + qemu-user-static binfmt-support on x86-64
sudo ./pi-edition/build.sh --profile pi-headless [--no-llama] [--workdir DIR] [--out DIR]
xz -dc out/pi/VictorLinux-pi-*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress   # check sdX first
```

* Sources (BUILD time only): Debian mirrors from `debian-edition/versions.env`; Raspberry Pi archive
  `https://archive.raspberrypi.com/debian trixie main`, signing key `keys/raspberrypi-archive.asc` (fingerprint
  `CF8A 1AF5 02A2 AA2D 763B AE7E 82B1 2992 7FA3 303E`, checked before use). An apt pin lets that archive supply only
  `raspi-firmware`, the `linux-image-rpi-*` kernels, `firmware-brcm80211`, `bluez-firmware`, `pi-bluetooth`,
  `raspi-utils`, `raspberrypi-sys-mods`, `raspi-config`, `rpi-eeprom` and their Pi-only dependencies.
* Image layout (MBR): p1 FAT32 `bootfs` 512 MiB (`/boot/firmware`), p2 ext4 `rootfs`, p3 ext4 `VICTORDATA` (256 MiB,
  grown to fill the card on first boot by `victor-pi-grow-data`). Built without loop devices (`mke2fs -d`, `mcopy`).
* Policy changes vs. Raspberry Pi OS: `sshswitch` (SSH via a file on the boot partition), `rpi-resize` and automatic
  `rpi-eeprom-update` are masked. Update the EEPROM yourself with `sudo rpi-eeprom-update -a` when you choose to.
* Kernels: `kernel_2712.img` (Pi 5, 16K pages) and `kernel8.img` (Pi 4/Zero 2 W) are both installed; the firmware picks.
  If a later `raspi-firmware` upgrade on the device rewrites `config.txt`/`cmdline.txt`, re-check the root `PARTUUID`
  and Wi-Fi country. **[Guessing]** whether it does.

### CI

[`.github/workflows/pi-image.yml`](../.github/workflows/pi-image.yml): static checks and tests on every relevant PR;
`pi-headless` and `pi-desktop` image builds on PRs touching the Pi edition, on GitHub's native arm64 runner (`ubuntu-24.04-arm`); any
profile via **Run workflow** (`--no-llama`, or an x86-64 runner with qemu-user-static, which is much slower).
Artifacts are CI outputs, not a release.

## Implemented vs built vs tested

* **Implemented:** everything above.
* **Unit-tested (locally and in CI):** board classification, model suggestion, Wi-Fi country cmdline editing, first-boot
  file parsing/rejection, NetworkManager keyfile, package lists and profiles, apt pin coverage, archive key fingerprint,
  shared-part reuse, unit/file references, no secrets/telemetry/runtime downloads, brand colors, sudoers rule, TODO hooks,
  the `llm-disabled` guard in the shared llama service, shellcheck, and image partition layout (when mtools/sfdisk exist).
* **Built:** only in CI (see the PR checks). Not built locally.
* **Never done:** booting on any Pi, Wi-Fi association, Bluetooth pairing, first-boot screens on a real display, data
  partition growth on a real card, llama.cpp speed on a Pi.

## Licenses

Raspberry Pi firmware, kernels and `pi-bluetooth` come from Raspberry Pi Ltd's archive under their own licenses
(including redistributable binary firmware); see each package's `/usr/share/doc/*/copyright` in the image.
