# VictorLinux — Debian edition

A Debian 13 ("trixie", stable) **custom live distribution** with optional Kali tooling, derived from the ToriLinux idea
(upstream: [sasha0552/ToriLinux](https://github.com/sasha0552/ToriLinux), Arch-based; its renamed VictorLinux edition lives in the repository root).
Debian edition and fork modifications: **Victorious / Victorious Framework**. See `../NOTICE`.

Goal: a portable, offline AI workstation on a USB drive — Xfce desktop, llama.cpp with a Vulkan GPU backend (AMD RX 6800 XT
target, detected at runtime), persistent models/settings/work, Android + network tools, an authenticated OpenAI-compatible endpoint,
and an optional isolated Kali container.

> Variants: this release ships the **GUI** variant only. Headless variants for both the Debian and the Arch edition are planned — see [docs/ROADMAP.md](docs/ROADMAP.md).

> Storage: the Arch edition (repository root) can now use the PC's internal SSD and any external drive for models and files in addition to the boot stick, and has a GPT-capable boot-media tool for multi-terabyte drives ([docs/storage.md](../docs/storage.md)). The Debian edition keeps its `VICTORDATA` layout and does **not** have these features yet.

## What is in the image (runtime dependencies, no network needed)

| Area | Included |
|---|---|
| Base | Debian trixie, kernel `linux-image-amd64` (6.12 LTS series), `firmware-amd-graphics`, `-linux-free`, `-misc-nonfree`, `-realtek`, `-iwlwifi`, CPU microcode |
| Desktop | Xorg, Xfce 4.20, LightDM autologin to the unprivileged `victor` user, Firefox ESR; **always dark** (Adwaita-dark GTK, terminal, greeter, rounded Control Center tabs and chat browser). `victor-theme apply` enforces dark at login and ignores older light preferences; `victor-theme status` reports `dark` |
| Inference | llama.cpp (pinned commit, see `versions.env`): **Vulkan** (Mesa RADV) + **CPU** (all x86-64 variants, chosen at runtime), HTTPS support compiled in, built-in download disabled |
| Control | `victor-control` (Tk GUI), `victor-ctl` (CLI behind it, including `hw recommend` for estimated model-fit advice), `victor-priv` (single validated root helper), first-run wizard |
| Dev/diag | git, Python 3 (+venv, pipx, pip), gcc/make, ADB, Fastboot, Nmap, Wireshark, tcpdump |
| Containers | rootless Podman for the **optional** Kali environment (no Kali repos on the host) |

**Not included:** model weights (supply GGUF files yourself), ROCm/HIP (experimental, see [docs/GPU.md](docs/GPU.md)), image-generation apps
(ComfyUI/InvokeAI/SD.Next from the Arch edition are *not yet ported*, see [docs/PACKS.md](docs/PACKS.md)), Kali image (optional pack), Tailscale/Headscale client.

| Needs network | Everything else works offline |
|---|---|
| Building the ISO; fetching Ventoy; optional Kali download; optional model downloads you perform yourself; optional VPN client install | Boot, first-run, importing models from USB/disk, inference, local chat, API, Control Center, dev tools |

No telemetry, no cloud inference and no online fallback exist in the image; no service updates anything at boot (apt timers are masked).

## Quick start

```sh
sudo ./build.sh                       # see docs/BUILD.md (≈20 GiB disk, 8 GiB RAM recommended)
./scripts/fetch-ventoy.sh /opt/ventoy # verified download of the pinned Ventoy
sudo ./scripts/provision-usb.py list --iso ../out/debian/*.iso
sudo ./scripts/provision-usb.py plan --device /dev/sdX --iso ../out/debian/VictorLinux-*.iso   # shows computed layout
sudo ./scripts/provision-usb.py write --device /dev/sdX --iso ... --confirm 'ERASE <identity from plan>'
sudo ./scripts/provision-usb.py update --device /dev/sdX --iso NEW.iso --confirm 'UPDATE <identity>'  # add an ISO, data untouched
sudo ./scripts/provision-usb.py verify --device /dev/sdX                                             # read-only check
```

Choose the ISO in Ventoy. Before the desktop opens, the text console lets you set the owner password or approve credentials already saved on the data drive. Press Enter to skip and leave the owner account locked for this boot. The desktop then opens automatically.
Details: [docs/USB.md](docs/USB.md), [docs/PERSISTENCE.md](docs/PERSISTENCE.md).

## Documents
[BUILD](docs/BUILD.md) · [USB](docs/USB.md) · [PERSISTENCE](docs/PERSISTENCE.md) · [GPU](docs/GPU.md) · [ENDPOINT / DroidCommand-AI](docs/ENDPOINT.md) · [KALI](docs/KALI.md) · [SECURITY](docs/SECURITY.md) · [PACKS & parity with Arch](docs/PACKS.md) · [ROADMAP](docs/ROADMAP.md) · [CURRENT SOURCE TEST REPORT](../docs/2026-11-14-victorlinux-testing.md)
