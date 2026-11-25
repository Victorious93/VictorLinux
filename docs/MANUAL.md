# VictorLinux user manual

A plain-English guide to using VictorLinux on a PC (Arch or Debian edition) or a Raspberry Pi. It describes what is in the current tree (`main` at `75c9f32` and later). It does **not** claim that any of this has been booted or tried on real hardware.

For a first-boot walk-through with a results table, use the [real-hardware boot checklist](BOOT_CHECKLIST.md). For what was reviewed in the code, see the [security audit](SECURITY_AUDIT.md). This page points at the detailed feature docs rather than repeating them.

## What it is

VictorLinux is a portable, private AI workstation. You boot it from a USB stick (or from an ISO on a supported rooted phone). Inference runs on **this machine's** hardware.

- **No telemetry.** Nothing is reported back.
- **No automatic downloads.** Models, voice files, image-generation programs and Coral runtimes are files you already have. You copy them in yourself.
- **No cloud and no cloud fallback.** If a feature cannot run locally, it stops and says so. It does not call a network service instead.

The live operating system itself is fresh each boot. What survives is the data you keep on a persistent volume (normally a `VICTORDATA` partition on the stick, or a drive you choose). See [storage](storage.md) and, for Debian/Pi, [persistence](../debian-edition/docs/PERSISTENCE.md).

It is part of the Victorious Framework and works on its own. Phone and fleet tools are optional.

## Editions

| | [Arch edition](../README.md) (repository root) | [Debian edition](../debian-edition/README.md) | [Raspberry Pi edition](raspberry-pi.md) |
|---|---|---|---|
| Desktop | Openbox and a **web** Control Center | Xfce and a **Tk** Control Center (`victor-control`) | Same as Debian on `pi-desktop`; no desktop on `pi-headless` |
| Open it with | `victor-open` | `victor-control` | `victor-control` on desktop; `victor-ctl` / `victor-pi` when headless |
| Inference | Separate llama.cpp builds: CPU, Vulkan, optional CUDA, optional HIP | CPU + Vulkan (optional NVIDIA *driver* profile still uses Vulkan, not a CUDA llama.cpp build). No HIP in the image | CPU only (NEON). No Vulkan or GPU backend |
| Storage | Multi-drive destinations on disks you already own | A `VICTORDATA` / `TORIDATA` partition only (no multi-drive `victor-storage use` yet) | Same as Debian; the data partition is grown to fill the card on first boot |
| Typical command style | `victor-open`, `victor-storage`, `victor-hardware`, `victor-diag` | `victor-ctl …` (one CLI behind the Tk app) | Those Debian tools, plus `victor-pi` |

The owner account is always `victor`. There is no default or shared password. SSH is off until you turn it on (key-only). The Control Center admin UI never leaves this machine's loopback address.

The two PC editions can recognise the same data-partition *label*, but they keep **different folder layouts** and do not migrate each other's settings. Do not expect Arch chats or keys to appear in the Debian UI, or the other way around.

## Making the USB or phone boot, and first boot

There is **no published release ISO**. You build an image (or use a CI artifact, which is not a release) and write it yourself. Building needs internet; the finished image is meant to run offline. See [build](build.md), the Debian edition README, and [Raspberry Pi](raspberry-pi.md).

**Writing a stick erases that stick.** Runtime storage tools do not format disks. Confirm the device and the serial / `ERASE …` string before you write.

- **Arch stick:** `scripts/make-usb.sh plan` then `write --confirm-serial …`. Default layout: raw hybrid ISO plus ext4 `VICTORDATA`. Prefer this over Ventoy for Arch (Ventoy UEFI boot of this ISO is unresolved). Details: [boot media](usb.md).
- **Debian stick:** `provision-usb.py plan` then `write --confirm 'ERASE …'` (Ventoy + `VICTORDATA`). Details: [Debian USB](../debian-edition/docs/USB.md).
- **Pi image:** decompress the `.img.xz` and `dd` it to the card. First boot grows only the already-labelled `VICTORDATA` partition. Headless with no screen: copy `victor-firstboot.conf.example` to `victor-firstboot.conf` on the FAT boot partition and fill it in (use `PASSWORD_HASH` from `openssl passwd -6`, not a plain password).
- **Victorious Flash:** can write an ordinary stick, or a rooted phone can expose the ISO as a read-only boot gadget. Persistence then comes from a **separate writable drive**. Flash has not been updated in this repo for drives above 2 TiB. Flash's no-root HTTP/EFI path is not supported. See [Flash companion changes](flash-companion-changes.md) and the [boot checklist](BOOT_CHECKLIST.md).

**First boot, in short:**

1. In the firmware menu pick the stick or the phone (UEFI preferred; **Secure Boot is not supported** by this ISO — turn it off). A Pi has no PC-style menu; it boots from the card.
2. The `victor` account auto-logs in on the console. Arch starts Openbox; Debian/Pi desktop starts Xfce (always dark).
3. Debian asks for *your* owner password (first-run window). Arch has no password wizard. Pi desktop or a headless tty asks for name, password, Wi-Fi country, optional Wi-Fi and optional SSH keys. Radios start soft-blocked until a Wi-Fi country is set.
4. Confirm persistent storage mounted (not a silent RAM session). Then open the Control Center (`victor-open` or `victor-control`).

The [boot checklist](BOOT_CHECKLIST.md) is the page to print and tick off, including BIOS/UEFI notes and a results table.

## The Control Center

The admin UI is local only. On Arch it is a web app at `http://127.0.0.1:8787/` with a per-session token (`victor-open` puts the token in the URL and opens Firefox). On Debian and the Pi desktop it is the Tk app `victor-control`. Headless builds have no GUI; use the commands in the next section.

### Arch (web) tabs

Built-in: **Dashboard**, **Models**, **Documents**, **Storage**, **Server & Access**, **Fleet**, **Logs & Diagnostics**. Extra feature screens (from `web/ext`) appear in the same sidebar: **Chat**, **Coral TPU**, **Hotspot**, **Images**, **Mesh**, **Tune**, **Voice**. Model hot-swap is a card on **Models** (*Switch model*), not its own tab.

| Tab | What it is for |
|---|---|
| Dashboard | Live status (model, backend, API, storage, RAM/VRAM). Start / Stop the model, open chat, check this PC |
| Models | Pick a GGUF, backend (`auto` / Vulkan / HIP / CUDA / CPU), context and layers; model library; import; switch model without a reboot |
| Documents | Ask questions of folders you add, fully offline ([doc chat](doc-chat.md)) |
| Storage | Destinations on existing drives, health (read-only SMART), encrypted backups of settings and chats |
| Server & Access | Inference API bind address, API keys, phone pairing QR, microphone, Bluetooth and Wi-Fi keyboard, SSH |
| Mesh | Join your Headscale, share, mesh SSH and related options |
| Fleet | Optional coordinator for DroidCommand phones; off until you start it |
| Chat | Encrypted chat history on the data partition |
| Tune | One benchmark to pick fast settings for this PC and this model |
| Voice / Images / Coral TPU / Hotspot | Optional features; off until you turn them on and supply their files |
| Logs & Diagnostics | Check this PC, logs, `Collect` diagnostics, reboot / power off |

### Debian and Pi desktop (Tk) tabs

Built-in: **Status**, **Models**, **Remote access**, **SSH**, **Mesh**, **Microphone**, **Phone keyboard**, **Kali (optional)**, **Fleet**, **Storage**, **Documents**, **Diagnostics**. Extra tabs: **Chat**, **Coral TPU**, **Hotspot**, **Images**, **Switch model**, **Tune**, **Voice**.

Storage on this edition is the `VICTORDATA` layout only (mount, temporary session, repair). Kali is a Debian-only optional rootless container. Headless Pi has no Tk app.

## Commands

Run these as `victor` in a terminal on the booted system. Several need `sudo -n` only for the narrow `victor-priv` / `victor-mesh-priv` helpers; there is no passwordless full sudo.

### Arch edition

| Command | Purpose |
|---|---|
| `victor-open` | Start the Control Center if needed and open it in Firefox on `127.0.0.1:8787` |
| `victor-control-center` | The web UI server itself (usually started by `victor-open`) |
| `victor-storage` | `list`, `status`, `use ROLE DRIVE --folder DIR`, `state DRIVE`, `eject` — multi-drive storage |
| `victor-hardware` | JSON: measured hardware and estimated model advice |
| `victor-diag` | Human-readable diagnostics (GPU, Vulkan, backend, storage, log tails). Safe-ish to paste; key-like tokens are redacted |
| `victor-check` | Same checks as *Full PC check*, plus a short speed test if a model is already loaded |
| `victor-model-import FILE.gguf` | Copy a GGUF (or a split set) into storage; `--to-storage` for a chosen models drive |
| `victor-bench [backend] model.gguf` | llama-bench on that file |
| `victor-capcheck` | Real requests against the loaded model; fills `/victor/capabilities` |
| `victor-tls-cert HOST` | Self-signed certificate for the gateway |
| `victor-mesh` | Join / status / up / down / leave, share, options — same as the Mesh tab |
| `victor-input` | Wi-Fi keyboard/mouse service (off by default) |
| `victor-mic` | Phone microphone / push-to-talk receiver |
| `victor-bt-pair` | Bluetooth pairing helper |
| `victor-phone`, `victor-phone-gui`, `victor-phone-headless`, `victor-pc` | Phone mirroring and related tools |
| `victor-secbackend` | Optional SET/Metasploit security backend (see [security-backend](security-backend.md)); off unless you start it |

### Debian and Pi

| Command | Purpose |
|---|---|
| `victor-control` | Tk Control Center (desktop only) |
| `victor-ctl` | The CLI behind it. Groups include `storage`, `backup`, `hw`, `model`, `llama`, `api`, `bench`, `docs`, `diag`, `ssh`, `input`, `bluetooth`, plus feature groups `chats`, `coral`, `hotspot`, `image`, `switch`, `tune`, `voice` when those modules load. Try `victor-ctl --help` |
| `victor-mesh` | Same mesh CLI as Arch |
| `victor-firstrun` | First-run password window (normally starts by itself) |
| `victor-theme` | Dark theme; `status` should report `dark` |
| `victor-chat` | Helper around local chat (the Chat tab / `victor-ctl chats` is the encrypted vault) |
| `victor-input`, `victor-mic`, `victor-bt-pair`, `victor-phone*` , `victor-pc` | Same roles as on Arch |

Useful `victor-ctl` examples: `storage status`, `hw snapshot`, `hw backend`, `hw recommend`, `model import FILE`, `llama start`, `api connection`, `diag show`, `diag check`, `tune run`, `switch FILE.gguf`.

### Raspberry Pi only

| Command | Purpose |
|---|---|
| `victor-pi status` | Board, whether LLM serving is enabled, a model *suggestion* (nothing is downloaded) |
| `victor-pi wifi-country CC` | Required before onboard Wi-Fi is unblocked |
| `victor-pi wifi-connect SSID` | Passphrase on stdin |
| `victor-pi bluetooth on\|off` | Off by default |
| `victor-pi ssh-keys` | Read public keys from stdin; SSH stays off until enabled |
| `victor-pi hooks` / `hook NAME` | Optional hooks. `mesh` and `scrcpy` reuse main; `hotspot` and `phone-keyboard-bt` still print `TODO` and change nothing |

On a Pi Zero 2 W (or any board under about 1.8 GiB RAM), LLM serving is disabled for that boot and `victor-ctl llama start` must refuse with a reason. The same card in a Pi 5 can serve models again. Details: [raspberry-pi.md](raspberry-pi.md).

## Your models, and CPU / Vulkan / CUDA / HIP

Nothing is bundled. Point VictorLinux at GGUF files you already have (any drive; read-only is fine). On Arch, `victor-storage use models DRIVE --folder DIR` uses them in place. Import copies a file or a complete split set (`victor-model-import FIRST-00001-of-0000N.gguf` copies every shard or none). See [model library](model-library.md) and [hardware and models](hardware-and-models.md).

**Backend** is chosen in the Models tab (`auto` by default) or with `victor-ctl hw backend` / llama settings.

| Choice | When it is used |
|---|---|
| `auto` | NVIDIA GPU + a CUDA build in the image → CUDA. Otherwise a **hardware** Vulkan device → Vulkan. Otherwise CPU. A software renderer (llvmpipe / lavapipe) is not treated as acceleration. **HIP is never chosen automatically.** |
| `vulkan` | Needs a Vulkan `llama-server` in the image and a real device. This is the path for a Radeon RX 6800 XT (Mesa RADV, PCI `1002:73bf`). See [AMD GPU](amd-gpu.md). |
| `cuda` | Only if you booted an Arch image built with the CUDA / `universal` profile, and you have a supported NVIDIA GPU (Turing or newer on the current snapshot). |
| `hip` | Only if you built Arch with the optional HIP pack (`gfx1030` for the 6800 XT). Experimental; you must select it yourself. Debian does not ship HIP. |
| `cpu` | Always available. Expected on the Pi. On a 6800 XT, treat it as a problem unless you chose it on purpose. |

After Start, believe the llama-server log: layers should be offloaded to the GPU when you picked a GPU backend (`offloaded N/M` with N > 0). The UI warns if a GPU backend was selected but 0 layers moved. Estimates in `victor-hardware` are estimates, not a promise that a model fits. *Check this PC* / `victor-check` / `victor-ctl diag check` explains a CPU fallback ([check this PC](check-this-pc.md)).

## The API, gateway keys, and phone pairing

The Control Center starts `llama-server` on a private loopback port. Clients talk to the **gateway** (Arch) or the configured API (Debian).

- Default: `http://127.0.0.1:8080/` — this machine only, no key.
- To listen on a LAN, VPN or mesh address: *Server & Access* / *Remote access*. Create an API key. Then every path except `GET /victor/health` needs `Authorization: Bearer <key>`.
- The key is shown once and stored as a hash. Revoke or "Revoke all remote access" drops it and returns to loopback.
- Prefer the [mesh](victorious-mesh.md) or HTTPS (`victor-tls-cert`, or the button in the UI). Plain HTTP on a LAN is readable by anyone on that network.
- Clients may use OpenAI (`/v1/chat/completions`, …), a small Ollama-compatible subset, or llama.cpp native paths. Tool execution is not granted to remote callers.

Details: [remote access](remote-access.md), [API keys and pairing](api-keys-and-pairing.md), [clients](clients.md).

**Pair phone** (opt-in, Arch *Server & Access*; the same idea is in the pairing docs for clients such as Victorious Browser): a QR is shown for a short window. It carries the gateway address, a TLS fingerprint and a **single-use token, never an API key**. The phone trades the token for its own named key over TLS. Wrong tokens close the window. There is no LAN discovery.

## Optional features (all off by default)

None of these start at boot. Several need programs or models you supply. Skip any you do not have.

| Feature | Where | What to know | Doc |
|---|---|---|---|
| Encrypted chats | **Chat** tab; Debian also `victor-ctl chats` | Passphrase (10+ characters) is never stored. scrypt + AES-256-GCM on the data partition. No plaintext fallback. Export to Markdown/JSON is deliberately plain text and warns first | [persistent-chats](persistent-chats.md) |
| Bench and auto-tune | **Tune** tab; `victor-bench` (Arch); `victor-ctl tune` / `bench run` (Debian) | One short `llama-bench` on *this* PC and model; the winning settings are saved for this machine only | [bench-autotune](bench-autotune.md) |
| Hot-swap | Arch: Models → *Switch model*; Debian: **Switch model** tab or `victor-ctl switch` | Loads another already-imported GGUF without rebooting. Arch keeps the gateway up and holds requests briefly; Debian serves llama-server directly, so the API is down while the new model loads. Rollback if the new one fails | [model-hot-swap](model-hot-swap.md) |
| Voice | **Voice** tab; `victor-ctl voice` | You provide whisper.cpp and a ggml model; Piper is optional. Recordings are temporary and deleted. No network | [voice-chat](voice-chat.md) |
| Image generation | **Images** tab; `victor-ctl image` | You provide a stable-diffusion.cpp binary and a model in `images/models`. Pictures stay in `images/out`. Not on the remote API. Not tested with a real `sd` build | [image-generation](image-generation.md) |
| Coral Edge TPU | **Coral TPU** tab; `victor-ctl coral` | **Vision and wake-word only — it cannot run chat models.** You install the Edge TPU runtime and copy `*_edgetpu.tflite` files into `coral/`. If the device is missing, it must stop with an explanation, never fall back to CPU or a cloud. Not tested on a real Coral | [coral-edge-tpu](coral-edge-tpu.md) |
| Wi-Fi hotspot | **Hotspot** tab; `victor-ctl hotspot` | Isolated AP (`10.77.0.0/24`), no internet sharing. Useful only if the remote API is bound where phones can reach it. Password via Show / Join QR. Not tried on real Wi-Fi. The Pi hook `victor-pi hook hotspot` is still a TODO | [wifi-hotspot](wifi-hotspot.md) |
| Mesh | **Mesh** tab; `victor-mesh` | Joins **your** Headscale with a pre-auth key (PCs: `tag:victorlinux`, Pi: `tag:victorlinux-pi`). Off until you join. Key is never stored in settings. File share, mesh SSH, exit node and so on are each off until enabled. Not tried against a real Headscale | [victorious-mesh](victorious-mesh.md) |

Phone extras (keyboard, microphone, mirroring) have their own pages: [phone as keyboard](phone-as-keyboard.md), [phone microphone](phone-microphone.md), [phone mirroring](phone-mirroring.md).

## Multi-drive storage and safety rules

This is about **using** drives after you have already booted. Preparing a stick (above) is the only workflow that erases a disk, and only after an explicit confirmation.

**Arch** can keep the OS on the stick, documents on the internal SSD, and models on an existing external drive, all at once. The data drives do not need a VictorLinux label or a special format. Choosing a folder only creates that folder. It must never format, repartition, relabel, re-own or erase the drive. If the UI ever offers to wipe a data disk, stop.

```sh
victor-storage list
victor-storage use models    "Seagate 2TB" --folder "LLM library"
victor-storage use documents <UUID> --folder "Documents"
victor-storage status
```

Drives are remembered by filesystem UUID, not `/dev/sdX`. If a chosen drive is unplugged, a later save **fails with an explanation** — nothing is silently written to RAM or another disk. Privileged work goes through `victor-priv`, which has no format or partition command. Full behaviour, filesystems and limits: [storage](storage.md). Health and encrypted backups: [storage health and backups](storage-health-backups.md).

**Debian and Pi** auto-mount a single ext4 `VICTORDATA` or `TORIDATA` partition. Several such partitions: nothing is mounted until you choose. None: you may start an explicit temporary session (RAM only, lost at power-off). `victor-ctl storage init` only creates the folder layout on an already-mounted data volume; it does not format a disk.

The running system must not treat the boot stick as a format target. `make-usb.sh` refuses the disk the OS booted from.

## Troubleshooting

Start with the tables in the [README](../README.md#troubleshooting) and the [boot checklist](BOOT_CHECKLIST.md). Short version:

| Symptom | What to try |
|---|---|
| No persistent storage | `victor-storage list` or `victor-ctl storage status`. Choose a volume, or start a temporary session deliberately |
| Destination missing / ambiguous | Reconnect; duplicate UUIDs need one drive unplugged or relabelled |
| Drive mounted read-only | Often Windows Fast Startup / an unclean NTFS volume. VictorLinux does not force a repair on NTFS |
| File refused on FAT32 | Files ≥ 4 GiB cannot exist there |
| Stick will not boot under Ventoy/UEFI | Use the Arch raw layout; Ventoy UEFI for this ISO is unresolved |
| CPU fallback on a GPU PC | *Check this PC* / `victor-diag`. Look at the driver, `/dev/dri` permissions, and whether Vulkan is only llvmpipe |
| GPU backend but 0 layers offloaded | The log is the proof; see [AMD GPU](amd-gpu.md) |
| Admin UI from another machine | That is expected to fail. The Control Center stays on `127.0.0.1` |

Save `victor-diag` (Arch) or `victor-ctl diag show` (Debian/Pi) when something is wrong. Do not paste owner passwords, mesh pre-auth keys, API keys, hotspot or share passwords, or files from `secrets/`.

## Security notes

The intended model is in [security](security.md): locked accounts, no default password, sudo limited to validated helpers, services off or on loopback until you opt in, no telemetry.

The [security audit](SECURITY_AUDIT.md) is a code review of this tree, with findings, what was fixed, and what was left open. Read it if you care about residual issues (for example, first-run password setup and auto-restoring a password hash from a labelled data drive). It is not a penetration-test report, and nothing in it was exercised on a booted machine here.

For a first run on real hardware, follow the [boot checklist](BOOT_CHECKLIST.md) and fill in its results table. This manual does not replace that pass.
