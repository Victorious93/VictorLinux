# Real-hardware boot checklist

A walk-through for Don when a VictorLinux image is first started on real hardware. Follow it step by step, write the result in the [table at the end](#results-table), and keep the diagnostics it asks you to save.

Base: `main` at `973fd69` (2026-11-25, America/New_York). Every claim below comes from the current repository code or docs. Labels:

- **[Certain]**: written in the code or a current document.
- **[Likely]**: follows from that, but has not been tried on real hardware.
- **[Guessing]**: plausible, not confirmed.

This revision has **no published release ISO**, and the docs say **no ISO of this revision has been booted on real hardware**. QEMU covered a generic GRUB test image (Arch planner) and, when CI ran it, a Debian ISO boot-test. Firmware, USB controllers, the RX 6800 XT, phones, Pi boards and Victorious Flash are all untested here. A tick in the table is *your* result, not a claim that it already works.

## Before you start

1. **Build or fetch an image** (there is nothing to download from GitHub Releases). See the README, [docs/build.md](build.md), [debian-edition/README.md](../debian-edition/README.md) and [docs/raspberry-pi.md](raspberry-pi.md).
   - Arch: `scripts/build.sh amd gui` → `out/victorlinux-*.iso` (Vulkan + CPU). Optional CUDA image: `scripts/build.sh universal gui` (or `cuda`). Optional HIP pack is separate (`--packs hip`) and experimental.
   - Debian: `debian-edition/build.sh` → `out/debian/VictorLinux-*.iso`. Vulkan + CPU. There is no HIP in the Debian image. An NVIDIA profile uses Vulkan on the NVIDIA driver; it is not a CUDA llama.cpp build.
   - Pi: `pi-edition/build.sh --profile pi-desktop` or `pi-headless` → `out/pi/VictorLinux-pi-*.img.xz`. CPU only (no Vulkan/GPU backend on the Pi).
2. **Keep the `.sha256` file next to the ISO or image** and check it before you write anything (`sha256sum -c`).
3. **Back up anything already on the stick or SD card.** Preparing boot media **erases the whole target**. Using a drive later, from inside VictorLinux, must not.
4. Have at least one GGUF model on a USB or internal drive if you want to test inference (nothing is bundled or downloaded).
5. For mesh: your Headscale URL and a short-lived pre-auth key. For voice, image generation or Coral: the programs and models are not in the image; you bring them (see those sections). For Coral: a USB Accelerator or M.2 module, plus the runtime you install yourself.
6. Don's PC (Radeon RX 6800 XT) is the AMD Vulkan target. Use an **Arch `amd` or Debian default** image for that machine, not a CUDA-only expectation.

## Preparing boot media

These tools are destructive on purpose. They refuse the system disk and need an explicit confirmation. Runtime storage tools are a different path and have no format command.

### Arch ISO → USB stick (`scripts/make-usb.sh`)

Default layout: hybrid ISO written raw, plus an ext4 `VICTORDATA` partition. UEFI and legacy BIOS are the intended firmware modes. **[Certain]** in the script and [docs/usb.md](usb.md); the VictorLinux ISO itself has not been booted on a real stick.

```sh
sudo scripts/make-usb.sh list
sudo scripts/make-usb.sh plan  --iso out/EXACT-victorlinux-image.iso --device /dev/sdX
# Read the plan. Back up the target. Then:
sudo scripts/make-usb.sh write --iso out/EXACT-victorlinux-image.iso --device /dev/sdX --confirm-serial ACTUAL_SERIAL
```

Success: the plan's sizes match the stick; `write` re-reads the ISO payload and reports a matching checksum; the stick shows a `VICTORDATA` partition. A 4Kn stick is refused and left untouched. Sticks larger than 2 TiB get a GPT that keeps the image's own boot structures. Minimum size is image + 2 GiB data + 1 GiB reserve (about 4.8 GiB for a ~1.8 GiB ISO); `plan` prints the exact figure.

Do **not** rely on `make-usb.sh --layout ventoy` for this checklist: an earlier Ventoy + UEFI boot of this ISO failed in QEMU and is still unresolved. Prefer the default raw layout.

### Debian ISO → USB stick (`debian-edition/scripts/provision-usb.py`)

Debian uses Ventoy plus a `VICTORDATA` partition. Ventoy is meant to boot the ISO in legacy BIOS and UEFI. **[Certain]** in [debian-edition/docs/USB.md](../debian-edition/docs/USB.md); real hardware is untested.

```sh
./debian-edition/scripts/fetch-ventoy.sh /opt/ventoy    # pinned, checksum verified
sudo ./debian-edition/scripts/provision-usb.py list --iso out/debian/VictorLinux-*.iso
sudo ./debian-edition/scripts/provision-usb.py plan  --device /dev/sdX --iso out/debian/VictorLinux-*.iso
sudo ./debian-edition/scripts/provision-usb.py write --device /dev/sdX --iso ... --confirm 'ERASE <identity from plan>'
```

Success: `plan` prints a confirmation string of the form `ERASE model:serial:bytes`; `write` accepts only that exact string; the ISO's `.sha256` is checked before and after the copy; Ventoy's menu later lists the ISO.

### Raspberry Pi image → SD card or USB

The image is a full disk (FAT `bootfs`, ext4 `rootfs`, ext4 `VICTORDATA`). Write the decompressed image, not the ISO tools above.

```sh
xz -dc out/pi/VictorLinux-pi-*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
```

Check `sdX` first. Success: the card has those three partitions, p3 labelled `VICTORDATA`. On first boot, `victor-pi-grow-data` expands **only partition 3** (and only if it is still labelled `VICTORDATA`) to fill the rest of the card, then resizes the filesystem. It does not touch partitions 1 or 2, and it runs once. **[Certain]** in the grow script; not tried on a real card.

For a headless Pi with no screen, copy `victor-firstboot.conf.example` on the FAT boot partition to `victor-firstboot.conf` and fill it in **before** first boot. Plain passwords are refused; use `PASSWORD_HASH` from `openssl passwd -6`. Invalid values apply nothing and write the reason to `/var/lib/victorlinux/firstboot-errors.txt`. The file is overwritten and deleted after a successful apply.

### Victorious Flash: writing a stick, or booting from a phone

[Victorious Flash](https://github.com/Victorious93/victorious-flash) is a separate repository. Two different jobs:

| Job | What to expect | Success |
|---|---|---|
| Flash writes an ordinary USB stick (desktop, or the phone as a USB *host* / OTG) | The README still describes Flash as writing the ISO, then adding a 16-byte MBR slot-3 `VICTORDATA` entry. That path is **not** updated for drives above 2 TiB ([docs/flash-companion-changes.md](flash-companion-changes.md)). Use a stick of 2 TiB or less, or use `make-usb.sh` instead. | The PC boots the stick the same way as a `make-usb.sh` stick, and finds `VICTORDATA`. If Flash reports a checksum mismatch, stop; do not boot it. |
| The phone is a **read-only** boot medium (rooted Android, USB gadget: the phone is the USB *device*) | The ISO is exposed to the PC; persistence comes from a **separate writable drive**, not from the phone. Any ext4/btrfs/xfs volume can be the state drive (`victor-storage state DRIVE`). Flash's no-root HTTP/EFI path is Ubuntu/casper only and is **not supported** for VictorLinux. | The PC's firmware sees the ISO, the kernel starts, and storage is missing until you attach and select the extra drive. Keep the phone connected until the PC has shut down. Gadget mode and OTG cannot share one USB port. |

Neither Flash path has been re-verified against current Flash source in this repository. Treat firmware and gadget quirks as **[Guessing]** until you try them.

## BIOS, UEFI and Secure Boot

- **UEFI is recommended** for the Arch raw stick ([docs/usb.md](usb.md)). Legacy BIOS is also an intended mode for that layout and for Debian's Ventoy stick.
- Open the firmware boot menu on the PC (the key is vendor-specific: often Esc, F10, F12 or a similar key — **[Guessing]** for any given machine) and pick the USB stick or the phone gadget, not the internal disk.
- If the firmware lists both a UEFI and a legacy entry for the same stick, try UEFI first, then legacy, and record which one actually started the OS.
- **Secure Boot is not supported by this ISO** ([docs/security.md](security.md)). Turn Secure Boot off for this checklist. Debian's Ventoy notes that Secure Boot would need Ventoy's own key enrolled (see Ventoy's docs); that has not been tried here. If you leave Secure Boot on and the firmware refuses to start the image, that is expected, not a surprise fail — note it anyway.
- On a Pi, there is no PC-style firmware menu. The bootloader on the FAT partition picks `kernel_2712.img` (Pi 5) or `kernel8.img` (Pi 4 / Zero 2 W).

Success: you reach a VictorLinux login or desktop, not a "Security Boot Fail", "MOK management", or a blank Ventoy/UEFI hang. If Ventoy on Arch hangs or says the ISO is unsupported, stop and use the raw layout instead (Ventoy UEFI for this ISO is unresolved). Debian Ventoy: if "normal" mode hangs, try Ventoy's "GRUB2 mode" and record which worked.

## What "it booted" looks like, by edition

Do these checks on every boot before you go further. The owner account is always `victor`. There is no default or shared password. The physical console auto-logs that account in.

| Edition | How the session starts **[Certain]** | Success looks like | First setup |
|---|---|---|---|
| Arch GUI | tty1 auto-logs in `victor`, `~/.zprofile` runs `startx`, Openbox starts | Dark Openbox desktop, no password prompt, a way to open a terminal and `victor-open` | No first-run password wizard. SSH stays off. |
| Debian GUI | LightDM auto-logs in `victor` to Xfce (always-dark theme) | Xfce desktop, brand colours, first-run window asking for *your* password | Set a password (works offline). Hash is stored on `VICTORDATA` and restored next boot. With no data partition the prompt comes back. SSH stays off. |
| Pi 5/4 desktop (`pi-desktop`) | Same Xfce + LightDM path as Debian, plus a Pi setup window | Setup asks for full name, password, Wi-Fi country, optional Wi-Fi, optional SSH public keys, and "SSH on now (key-only)" | Country is required before Wi-Fi is unblocked (radios start soft-blocked). |
| Pi headless (`pi-headless`, including Zero 2 W) | No desktop. Console setup on tty1 (`victor-pi-setup-tui`), or `victor-firstboot.conf` on the boot partition if there is no screen | You get a login or a shell after setup; `victor-ctl` and `victor-mesh` are on `PATH` | Same questions as the desktop setup. On a Zero 2 W, also check the LLM-disabled behaviour below. |

On every edition, also confirm:

- The image identity is present: Arch `/etc/victor-release`; Debian/Pi `/usr/share/victorlinux/image-facts`.
- Persistent storage: Arch `victor-storage status`; Debian/Pi look at the Storage tab or `victor-ctl` storage status. A `VICTORDATA`/`TORIDATA` volume should mount on its own if it is the only candidate. If several labelled partitions are attached, **nothing is mounted** until you choose one. If none, you may start an explicit temporary session (RAM only, lost at power-off) — the UI must say so, never silently.
- You can open a terminal. `whoami` is `victor`. `sudo -n true` fails (there is no `NOPASSWD: ALL`). SSH is off unless you just enabled it.

## Hardware detection

On the Arch desktop: **Logs & Diagnostics → Check this PC** (or *Full PC check*), plus these commands:

```sh
victor-hardware          # JSON: measured RAM/GPUs, chosen backend, estimated model advice
victor-diag              # human-readable report (safe-ish to paste; API-looking tokens are redacted)
victor-check             # the same checks as the UI, plus a short speed test if a model is already loaded
```

On Debian / Pi desktop: *Check this PC* on the Tk Diagnostics tab, or:

```sh
victor-ctl diag show
victor-ctl diag check
victor-ctl hw snapshot
victor-ctl hw recommend
```

Success:

- RAM total looks like the machine you booted (not a tiny VM figure, unless you did boot a VM).
- The GPU is listed. On Don's RX 6800 XT, sysfs should show vendor `0x1002`, device `0x73bf`. The code names that PCI id `Radeon RX 6800 / 6800 XT / 6900 XT (Navi 21)` **[Certain]**. Driver should be `amdgpu`, not `radeon`, `vfio-pci` or nothing.
- `/dev/dri/card*` and `renderD*` are readable and writable by `victor` (`render`/`video` groups). `victor-diag` says `read/write=yes`.
- `vulkaninfo` lists a hardware device whose name is the AMD card, driver RADV / `amdgpu`. A device named llvmpipe, lavapipe or similar is **not** acceleration and must be treated as a CPU fallback.
- Chosen backend on that PC, auto mode, Arch `amd` or Debian default image: **`vulkan`**, reason along the lines of "Hardware Vulkan device detected: …". CPU is a fail on this machine unless you are deliberately testing the fallback.
- HIP is **not** chosen automatically, even if an Arch HIP pack is in the image. CUDA is only auto-chosen when an NVIDIA GPU *and* a CUDA `llama-server` are present. Neither applies to the 6800 XT.
- Pi: no GPU backend. `victor-pi status` should print a model suggestion. On a Zero 2 W, or any board with under about 1.8 GiB RAM, `/run/victor/llm-disabled` is written and `victor-ctl llama start` must refuse with a reason **[Certain]** in code, **[Likely]** on a real board. The same card moved to a Pi 5 should serve models again.

Then start a modest GGUF (something that should fit in 16 GB VRAM; you supply the file). After it is running, read the llama-server log (it is at the end of `victor-diag`, or the Debian journal tail). Success is a line that offloaded layers to the GPU (`offloaded N/M layers to GPU` with N > 0), not "0 layers". A one-token test chat in the Control Center should succeed. `victor-bench vulkan model.gguf` (Arch) or `victor-ctl bench run` (Debian, on the loaded model) is the speed measurement; record tokens/s. None of this has been run on a real 6800 XT in development **[Likely]** if the log shows offload.

If the backend falls back to CPU on the 6800 XT, check: boot without `nomodeset`, `linux-firmware` / `firmware-amd-graphics` present, user in `render` and `video`, no VFIO passthrough of the card.

## Storage on drives you already own

This is the non-destructive path. Preparing a boot stick (above) is the only workflow that erases a disk, and only after you type the serial or `ERASE …` confirmation.

**Arch** can use the internal SSD and any external drive without a special label or format. Picking a folder only creates that folder; it must never format, repartition, relabel, re-own or erase the drive **[Certain]** (`docs/storage.md`, and `victor-priv` has no format/partition command).

```sh
victor-storage list
victor-storage use models    "The drive's name or UUID" --folder "LLM library"
victor-storage use documents <UUID-of-internal-SSD> --folder "Documents"
victor-storage status
```

Success:

- `list` shows internal, USB and SD volumes with names and UUIDs, including drives that already have files on them.
- After `use`, the named folder exists and the original files on that drive are still there.
- Unplugging the drive makes a later save **fail with an explanation**, not silently write to RAM or another disk.
- Replug in a different port or order: the choice comes back (remembered by filesystem UUID, never `/dev/sdX`).
- The Storage tab in the Control Center shows the same state (same code).
- If the UI ever offers to format, repartition or wipe a data drive, that is a **fail** — stop and write it down.

**Debian and Pi** do not have this multi-drive destination feature yet. They auto-mount a single ext4 `VICTORDATA` or `TORIDATA` partition and keep app data there. You can still *read* models from a USB you mount yourself, but there is no `victor-storage use` equivalent. Do not format an existing disk to make a data partition "so it will work"; use the data partition already on the boot media, or (Arch) pick an existing folder.

The running live system must never treat the boot stick as a format target. `make-usb.sh` refuses the disk the system booted from (`/run/archiso/bootmnt`, `/run/live/medium`, and similar).

## Control Center (admin UI stays on 127.0.0.1)

| Edition | How to open it | Where it listens **[Certain]** |
|---|---|---|
| Arch GUI | `victor-open` (starts it if needed, opens Firefox with the session token) | `http://127.0.0.1:8787/` only, token in `~/.cache/victorlinux/cc.token` (0600). A foreign `Host` header is rejected. |
| Debian / Pi desktop | `victor-control` (Tk app) | Local desktop app; it is not a web server on the LAN. |
| Headless | no GUI; use `victor-ctl` / `victor-mesh` | — |

Success:

- The URL or window opens on this machine without a password other than the session token (Arch) or the local app (Debian).
- From another machine, `http://<lan-ip>:8787/` does not serve the admin UI. (The inference API is a different listener, next section.)
- Status shows model/backend, storage, and RAM/VRAM once hardware is detected.
- Closing and reopening `victor-open` still works (token file present).

## Inference API

The Control Center starts `llama-server` on a private loopback port and fronts it with the gateway.

- Default: `http://127.0.0.1:8080/` — local only, no API key **[Certain]** ([docs/remote-access.md](remote-access.md)).
- Remote: in *Server & Access* (Debian: Remote Access / `victor-ctl api`), set a LAN, VPN or mesh bind address, create a key. The gateway then requires `Authorization: Bearer <key>` on everything except `GET /victor/health`.
- The key is shown once and stored as a SHA-256 hash. "Revoke all remote access" deletes it and returns to loopback.

Success, local:

```sh
curl -sS http://127.0.0.1:8080/victor/health
# {"status":"ok"} when a model is running, or 503 model_not_running
```

A chat completion against `/v1/chat/completions` works from the same PC once a model is up. Success, remote (only after you opt in): a request without a key is rejected; a request with the key works from another device; binding stays on the address you chose (or the mesh IP), not accidentally on all interfaces unless you chose `0.0.0.0`. Prefer the mesh or HTTPS; plain HTTP on a LAN is readable by anyone on that network.

The admin Control Center must still be on 127.0.0.1 after you enable the remote API.

## Mesh join

Off by default. `tailscaled` is installed (Debian/Pi: from a build-time hook; if that hook skipped tailscale, the Mesh screen says so) and disabled until you join. **Never tested against a real Headscale.**

1. On Headscale, make a short-lived key:
   - PCs: `headscale preauthkeys create --user don --expiration 1h --tags tag:victorlinux`
   - Pi: same with `tag:victorlinux-pi`
2. Arch: Control Center → **Mesh** → URL, key, optional name → **Join mesh**. Debian/Pi desktop: the Mesh tab. Headless: `victor-mesh join --url https://headscale.example.com [--name my-pc]` (the key is prompted, hidden).
3. URL must be `https://` unless it is a private/loopback IP or a `.local` / `.lan` / `.home.arpa` name.

Success:

- `victor-mesh status` (or the tab) shows a mesh IPv4 in `100.64.0.0/10`.
- The pre-auth key is **not** in `settings`, logs, or `ps` output.
- After reboot, **Connect** / `victor-mesh up` comes back without a new key if persistent storage was mounted. In a temporary session you must join again.
- Optional file share (off by default): listen address is the mesh IP only, port 8090, Basic auth, read-only. Optional mesh SSH: key-only, `victor` only, and (Debian/Pi) port 22 open only on `tailscale0`. Do not turn these on unless you want to test them; record that they were off if you skip them.

Then `victor-mesh down` (keeps identity) or `leave` (deletes identity; also delete the node in Headscale).

## Wi-Fi hotspot

Optional, off by default, gone after reboot. Arch: Control Center → **Hotspot** → Start (hostapd + DHCP-only dnsmasq). Debian: the same tab, or `victor-ctl hotspot start` (NetworkManager AP profile + firewall). Isolated: `10.77.0.0/24`, no internet sharing, no forwarding; Arch uses `ap_isolate=1`, Debian/Pi's NetworkManager profile sets `802-11-wireless.ap-isolation yes`. The admin UI stays on 127.0.0.1; the hotspot is only useful if the remote API is on and bound to `0.0.0.0` (or the hotspot address). **Nothing here has been tried on real Wi-Fi hardware.**

Success: the SSID appears; a phone joins with the WPA2 password (shown only via Show / Join QR, never typed on the Debian CLI); the phone gets a `10.77.0.x` address; it can reach the API (with a key) and cannot reach the internet through the PC or talk to another joined phone. Stop the hotspot when you are done.

**Pi:** `victor-pi hook hotspot` is still a documented TODO and exits without changing anything. The Debian hotspot tool is copied into the Pi image with the rest of the Debian tools, so `victor-ctl hotspot` may be present; whether a Pi's onboard Wi-Fi can actually run AP mode has not been tried **[Guessing]**. Do not treat a Pi hotspot as a required pass.

## Voice, image generation, Coral

All three are **off by default**, bundle nothing, download nothing, and stay on the local admin UI. Skip any you do not have files or hardware for, and mark the row "skipped".

| Feature | What you must provide | How to run it | Success |
|---|---|---|---|
| Voice | whisper.cpp (`whisper-cli`) + a `ggml-*.bin`; optional Piper + `*.onnx` / `*.onnx.json`. Put models in the `voice` folder shown in the tab. | Arch: Control Center → **Voice** → enable, pick models, Start talking (3–24 s). Debian: Voice tab or `victor-ctl voice listen --seconds 6 --speak`. | A spoken prompt becomes text, the local model answers, and (if Piper is set) you hear it. Recordings are temporary and deleted. No network calls. |
| Image generation | A `sd-cli` or `sd` binary (stable-diffusion.cpp) and a model (`.gguf` / `.safetensors` / `.ckpt`) in `images/models`. | Arch: **Images**. Debian: Images tab or `victor-ctl image generate "…"`. | One PNG appears in `images/out`. Cancel works. Not exposed on the remote API. If VRAM runs out, stop the chat model first. Not tested with a real `sd` build. |
| Coral Edge TPU | `libedgetpu` + a TFLite runtime you install, and `*_edgetpu.tflite` models in `coral/`. A Coral cannot run LLMs. | Arch: **Coral TPU** → Try vision. Debian/Pi: `victor-ctl coral classify PICTURE` / `coral wake`. | The USB Accelerator enumerates (`1a6e:089a` then `18d1:9302`) or `/dev/apex_N` exists; a classification or wake-word result comes back. If the device or delegate is missing, the feature must **stop with an explanation**, never silently run on CPU or call a cloud. Not tested on a real Coral. |

## Diagnostics to collect

After the checks you care about (at least: boot, storage, hardware/backend, and one successful generation if you have a model), save:

**Arch**

```sh
victor-hardware > /tmp/victor-hardware.json
victor-diag     > /tmp/victor-diag.txt
# optional, if a model is loaded:
victor-check    > /tmp/victor-check.txt
```

`victor-diag` prints: image id, kernel, RAM, each GPU (name, PCI id, driver, VRAM), `/dev/dri` access, user groups, Vulkan devices, backends present in the image, memory-pool budget, selected backend and why, storage JSON, generation check if one was done, acceleration parsed from the llama-server log, and a tail of the llama-server and gateway logs. Tokens that look like `victor-…` keys are redacted.

**Debian / Pi**

```sh
victor-ctl diag show   > /tmp/victor-diag.json
victor-ctl diag check  > /tmp/victor-check.txt
victor-ctl hw snapshot > /tmp/victor-hw.json
# optional: victor-ctl diag save-log   # bounded llama journal snapshot on the data partition
```

`diag show` is described as copy-pasteable and is not supposed to include the API key, TLS key or password hashes. Skim the file before you share it anyway.

**Do not include** in anything you paste or attach: owner passwords or `PASSWORD_HASH`, mesh pre-auth keys, API keys (even if you think they are hashed), hotspot or share passwords, `secrets/` files, SSH private keys, or the Control Center token.

Also note, in the table: which image filename and SHA-256 you booted, UEFI vs BIOS, Secure Boot on/off, machine (for the 6800 XT: that it was recognised), and whether storage was persistent or a temporary session.

## Results table

Copy this and fill each cell with **pass**, **fail**, **skipped**, or **n/a**, plus a short note. Use one column per boot you actually did. Extra columns are fine if you try both a USB stick and a phone-hosted ISO.

| Check | Arch USB (raw) | Arch via Flash ISO (phone) | Debian USB (Ventoy) | Pi 5 or 4 desktop | Pi 5 or 4 headless | Pi Zero 2 W headless | Notes |
|---|---|---|---|---|---|---|---|
| Image SHA-256 verified before write | | | | | | | filename + hash |
| Write tool refused a wrong disk / required confirmation | | | | | | | |
| Firmware: UEFI boot | | | | n/a | n/a | n/a | key used, Secure Boot off? |
| Firmware: legacy BIOS boot | | | | n/a | n/a | n/a | |
| Autologin / desktop or headless prompt | | | | | | | |
| First-run / first-boot setup | n/a (Arch has no password wizard) | | | | | | Wi-Fi country, hash file, errors file |
| Persistent storage mounted (not a silent RAM session) | | | | | | | label, UUID |
| Existing data drive listed; folder created; nothing formatted | | n/a if no extra drive | n/a (Debian has no multi-drive `use`) | n/a | n/a | n/a | |
| Control Center on 127.0.0.1 only | | | | | n/a | n/a | |
| `victor-hardware` / `hw snapshot` looks like this machine | | | | | | | |
| RX 6800 XT: `amdgpu`, Vulkan RADV, backend `vulkan`, layers offloaded | | | | n/a | n/a | n/a | tokens/s if measured |
| CPU fallback behaves (Pi, or forced) | | | | | | | Zero 2 W: LLM refused |
| Local API `GET /victor/health` | | | | | | | |
| Remote API (opt-in): no key rejected, key works | | | | | | | bind address |
| Mesh join, status shows a 100.64.x address, key not in logs | | | | | | | Headscale version |
| Mesh still up after reboot (persistent storage) | | | | | | | |
| Data drive approval after reboot (Debian/Pi): the tty1 console asks for the owner password before login; a wrong password or Enter leaves the account locked and the drive as plain storage | n/a (Arch restores no password) | | | | | | Note whether the prompt appeared, and whether a wrong password was refused |
| Hotspot: phone joins, isolated, no WAN through the PC | | | | skipped unless you try | skipped | skipped | |
| Voice (if you brought models) | | | | | | | |
| Image gen (if you brought `sd` + a model) | | | | | | | |
| Coral (if you have a device + runtime) | | | | | | | |
| `victor-diag` / `diag show` saved (secrets checked) | | | | | | | path to the file |

## What this checklist cannot prove

A full row of passes on one PC is still one PC. It does not make a release, does not replace CI, and does not mean Ventoy UEFI, Secure Boot, drives above 2 TiB, Flash gadget mode, or a Pi board are generally fine. If something in the running system disagrees with this document, trust what you saw, mark **fail**, and keep the diag output.
