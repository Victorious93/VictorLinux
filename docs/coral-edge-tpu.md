# Coral Edge TPU (optional, off by default)

**A Coral Edge TPU cannot run LLMs (chat models).** It runs small, 8-bit TensorFlow Lite models that were compiled for it with `edgetpu_compiler` and fit in a few MB of on-chip memory. Chat models stay on the GPU or CPU. VictorLinux uses the Edge TPU for two things only:

* **Vision**: image classification with a model you supply (Control Center > Coral TPU > Try vision, `victor-ctl coral classify PICTURE`).
* **Wake word**: spotting a word in 16 kHz microphone audio (`victor-ctl coral wake`) for models that take a raw audio window (`[1, N]` samples). Models that need spectrogram features are refused with a clear message.

## Rules
* **Off by default. Nothing is downloaded or installed for you.** You install the Edge TPU runtime (`libedgetpu`, Google/Coral apt repository or your own build) and a TensorFlow Lite runtime (`tflite_runtime` or `ai_edge_litert`, plus numpy and Pillow for pictures) yourself, and copy your models (`*_edgetpu.tflite`, optional labels in a matching `.txt`) into `coral/` on the data partition.
* **No fallback.** If no Coral device is detected or the runtime/delegate cannot load, the feature stops with an explanation. It never silently runs on the CPU and never calls a cloud service. No telemetry.
* Models are only accepted if they are TFLite files containing the Edge TPU custom op, chosen from the coral folder. The admin UI stays on 127.0.0.1.
* Detection: USB Accelerator (ids 1a6e:089a before firmware load, 18d1:9302 after) and PCIe/M.2 modules (`/dev/apex_N`). A udev rule (`71-coral-edgetpu.rules`) gives the logged-in user access; it installs nothing.

## Editions
Arch (Coral tab + `/api/coral/*`), Debian (`victor-ctl coral` + Tk tab), and the Raspberry Pi edition (ships the Debian tools; a USB Accelerator is the usual setup there).

Nothing here has been run on a real Coral device: the tests use a fake TFLite runtime.
