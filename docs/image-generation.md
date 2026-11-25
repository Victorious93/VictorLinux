# Image generation (optional, off by default)

Draw pictures from a text prompt on this PC with [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) (MIT).

* **Off by default. Nothing is bundled or downloaded.** You copy a stable-diffusion.cpp build (`sd-cli` or `sd`, found on `PATH` or given as a full path) onto the PC and a model file (`.gguf`, `.safetensors` or `.ckpt`, e.g. a quantised SD 1.5 / SDXL model you obtained yourself) into `images/models` on the data partition. Then turn it on: Control Center > Images (Arch) or `victor-ctl image set enabled=true model=FILE` (Debian, also an Images tab).
* **No cloud fallback, no telemetry.** Prompts and pictures stay on the PC. Pictures are saved to `images/out` (PNG).
* One picture at a time. Limits: 256-1024 px in steps of 64, 1-60 steps, guidance 1-20, prompt up to 1000 characters, 30-minute timeout. A running picture can be cancelled.
* The admin UI stays on 127.0.0.1; image generation is not exposed on the remote API.
* GPU use depends on how your stable-diffusion.cpp build was compiled (CPU, Vulkan, CUDA, ROCm). Running it while a large chat model is loaded can run out of VRAM; stop the chat model first if that happens.

Debian CLI:

    victor-ctl image status
    victor-ctl image set enabled=true model=sd-v1-5-Q8_0.gguf bin=/opt/sd/sd-cli
    victor-ctl image generate "a neon fox in the rain" --steps 20 --width 512 --height 512

Not tested with a real stable-diffusion.cpp build or model; the tests use a stand-in program.
