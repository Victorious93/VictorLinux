# Building VictorLinux

## Network: build time vs. run time

| Phase | Internet needed? | What is fetched |
|---|---|---|
| `scripts/prepare-sources.sh` | yes | llama.cpp at a pinned tag (verified against a pinned commit SHA) |
| `mkarchiso` (image build) | yes | Arch packages from an **Arch Linux Archive snapshot** (`arch_snapshot` in `.ci/options.ini`) |
| Optional packs | yes | pip/npm/git (PyTorch wheels, ComfyUI, ...) |
| **Booting and running the image** | **no** | Nothing. No package updates, downloads, telemetry or cloud calls are configured |

## Local build (documented, no secrets, no special runner)

```sh
scripts/build.sh [amd|cuda|rocm] [headless|gui|empty] [--packs a,b,c]
```

* With Docker/Podman it runs `archlinux:latest` privileged and calls `scripts/build-inner.sh`.
* On Arch as root it runs `scripts/build-inner.sh` directly.
* Disk: allow ~20 GB for the lean image, much more for CUDA/HIP/packs (tens of GB).
* Outputs in `out/`: `victorlinux-*.iso`, `*.iso.sha256`, `manifest-*.json`, `packages-*.txt`, `build-*.log`.

`scripts/check-packages.sh` (run in Arch) verifies every package exists in the pinned snapshot before a long build.

### Profiles

* **platform** `amd` (Vulkan + CPU), `cuda` (CUDA + Vulkan + CPU, NVIDIA), `rocm` (= `amd` + experimental `hip` pack)
* **type** `headless`, `gui` (Openbox, autostarts the Control Center), `empty` (OS + drivers only)
* **packs** `hip comfyui automatic invokeai sillytavern vllm` - all off by default

## Pinning and reproducibility

`.ci/options.ini` pins: llama.cpp tag **and** commit, ComfyUI, SD.Next, nvidia-pstated, and the Arch snapshot date.
The manifest records the git commit (and whether the tree was dirty), llama.cpp commit, every installed package
and version (`packages-*.txt`), GPU-stack package versions (kernel, linux-firmware, mesa, vulkan-radeon, ...), and the
build environment. `.ci/update.py` is a *manual* helper to look up newer upstream releases; nothing updates
automatically, and nothing updates at boot.

Python environments are isolated per application (venv per project, pipx for InvokeAI/vllm); llama.cpp has no
Python dependency at runtime. The Control Center uses only the Python standard library.

## GitHub Actions

* `.github/workflows/validate.yml` - renders every profile, shellcheck, python tests (no secrets).
* `.github/workflows/build-iso.yml` builds Arch AMD GUI on relevant pull requests. A manual run can build a selected profile. CUDA, HIP, and optional packs may need a runner with more storage.
* `.github/workflows/debian-iso.yml` builds the Debian live image. `.github/workflows/pi-image.yml` builds Pi Desktop and Headless on relevant pull requests.
* Image jobs for a `release/` pull request check out its exact head commit. Compare the image manifests before putting artifacts together in a release draft.
* The original maintainer's SSH secrets and self-hosted runner are **not** used anywhere.

## Verification gates

The Arch AMD GUI workflow boots its own image in QEMU, imports a pinned test GGUF, runs CPU inference, and checks that the update tool preserves the model, settings, saved chat, and a document on a file-backed USB image. The Debian full suite uses the VM keyboard to set the first owner password and to approve saved credentials at the next boot. Both suites fetch the same checksum-pinned test model on the build runner and run inference in a guest with restricted networking.

Use the QEMU reports for the exact commit of a release candidate. Include the matching SHA-256 files and manifests with the images. Keep the firmware, physical storage, and GPU results in the [boot checklist](BOOT_CHECKLIST.md).
