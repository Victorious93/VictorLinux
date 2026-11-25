# Offline voice chat (optional, off by default)

Speak to the local model and hear the answer. Everything runs on this PC:

| Step | Program | Where it comes from |
|---|---|---|
| Microphone → text | whisper.cpp (`whisper-cli`) + a ggml model (`ggml-*.bin`) | **you provide both** |
| Text → answer | the running llama-server (127.0.0.1) | already part of VictorLinux |
| Answer → speech | Piper + a voice (`*.onnx` and `*.onnx.json`) | **you provide both** (optional; text-only replies otherwise) |

Nothing is downloaded and no program or model is bundled. Neither whisper.cpp nor Piper is in the Arch or Debian package lists this image is built from. Put the programs on `PATH` or enter their full path. Copy the model files into the `voice` folder on the data partition (the path is shown in the Voice tab).

Privacy: recordings are sent only to the Control Center on 127.0.0.1. They are written to a temporary folder (tmpfs under `/run/victor` when available) for one request and deleted. The transcript and reply are not saved. Persistent history is a separate feature (encrypted chats).

## Arch (Control Center → Voice)
Turn it on, pick the models, then **Start talking**. The page records 16 kHz mono WAV itself (Web Audio), with a 3–24 s limit because of the 1 MiB request limit. **Hands-free** starts recording when a local wake-word detector calls `POST /api/voice/wake`. The optional Coral feature provides one; nothing listens all the time otherwise.

## Debian
`victor-ctl voice set enabled=true stt_model=ggml-base.en.bin tts_model=en_US-amy-medium.onnx`, then:
- `victor-ctl voice listen --seconds 6 --speak` (needs `arecord`, which is added via `alsa-utils`)
- `victor-ctl voice ask question.wav`, or the **Voice** tab in the Tk Control Center.

Packages added for sound: Arch GUI images get `alsa-utils pipewire pipewire-alsa pipewire-pulse wireplumber`. Debian gets `alsa-utils pipewire-audio`.

## Reuse with phone push-to-talk

The existing push-to-talk pipeline (`victor-mic serve --ptt`, see [phone-microphone.md](phone-microphone.md)) can use the same whisper.cpp setup instead of Vosk:

    victor-mic serve --ptt --ptt-backend whisper [--voice-config /path/to/voice.json]

It reads the program and ggml model you picked here (default: `config/voice.json` on the data partition). Vosk stays the default. Audio is held in memory while the button is down and goes to a private temporary folder only for the one whisper.cpp call.
