"""Offline voice chat helpers shared by both editions: speech-to-text with whisper.cpp, text-to-speech with Piper.

Nothing is bundled or downloaded. The user puts a whisper.cpp program and a ggml model (ggml-*.bin), and optionally
Piper with a voice (*.onnx + *.onnx.json), on the machine and picks them in the Control Center. Voice is OFF by default.
Recorded audio only lives in a temporary folder for the duration of one request.
"""
import json
import os
import shutil
import struct
import subprocess
import tempfile

STT_NAMES = ("whisper-cli", "whisper-cpp", "whisper.cpp")
TTS_NAMES = ("piper", "piper-tts")
DEFAULTS = {"enabled": False, "stt_bin": "", "stt_model": "", "tts_bin": "", "tts_model": "", "language": "en",
            "speak_replies": True, "max_seconds": 20}
NOISE = {"[BLANK_AUDIO]", "[ Silence ]", "[MUSIC]", "(silence)", "[NO_SPEECH]"}


class VoiceError(Exception):
    pass


def load(path):
    c = dict(DEFAULTS)
    try:
        with open(path) as f:
            c.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    return c


def save(path, c):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: c[k] for k in DEFAULTS}, f, indent=1)
    os.replace(tmp, path)


def find_bin(configured, names):
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def model_files(dirs):
    """User-supplied voice model files: whisper ggml (*.bin / *.gguf with 'whisper' or 'ggml' in the name), piper (*.onnx)."""
    stt, tts = [], []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for root, _dn, files in os.walk(d):
            for fn in sorted(files):
                p, low = os.path.join(root, fn), fn.lower()
                if low.endswith(".onnx") and os.path.isfile(p + ".json"):
                    tts.append(p)
                elif low.endswith(".bin") and ("ggml" in low or "whisper" in low):
                    stt.append(p)
    return {"stt": stt, "tts": tts}


def update(c, b, files):
    """Apply a settings change; model paths must be one of the listed files, programs must be executable."""
    n = dict(c)
    for k in ("enabled", "speak_replies"):
        if k in b:
            if not isinstance(b[k], bool):
                raise VoiceError(f"{k} must be true or false.")
            n[k] = b[k]
    for k, kind in (("stt_model", "stt"), ("tts_model", "tts")):
        if k in b:
            v = str(b[k] or "")
            if v and v not in files[kind]:
                raise VoiceError("Pick a model file from the voice folder.")
            n[k] = v
    for k in ("stt_bin", "tts_bin"):
        if k in b:
            v = str(b[k] or "").strip()
            if v and not (os.path.isabs(v) and os.path.isfile(v) and os.access(v, os.X_OK)):
                raise VoiceError(f"{v} is not an executable program (use a full path).")
            n[k] = v
    if "language" in b:
        v = str(b["language"] or "").strip().lower()
        if not (v == "auto" or (2 <= len(v) <= 3 and v.isalpha())):
            raise VoiceError("Language must be a code like en, de or auto.")
        n["language"] = v
    if "max_seconds" in b:
        v = b["max_seconds"]
        if not isinstance(v, int) or isinstance(v, bool) or not 3 <= v <= 24:
            raise VoiceError("Max recording length must be 3 to 24 seconds (the request size limit).")
        n["max_seconds"] = v
    return n


def status(c, files):
    stt_bin, tts_bin = find_bin(c["stt_bin"], STT_NAMES), find_bin(c["tts_bin"], TTS_NAMES)
    stt_ok = bool(stt_bin and c["stt_model"] and os.path.isfile(c["stt_model"]))
    tts_ok = bool(tts_bin and c["tts_model"] and os.path.isfile(c["tts_model"]))
    missing = []
    if not stt_bin:
        missing.append("whisper.cpp program (whisper-cli) not found")
    if not c["stt_model"]:
        missing.append("no speech-to-text model selected")
    if not tts_bin:
        missing.append("Piper not found (replies are shown as text only)")
    return {"enabled": c["enabled"], "stt_ready": stt_ok, "tts_ready": tts_ok, "stt_bin": stt_bin, "tts_bin": tts_bin,
            "ready": c["enabled"] and stt_ok, "missing": missing, "files": files}


def check_wav(data, max_seconds):
    """Accept only 16 kHz, mono, 16-bit PCM WAV (what whisper.cpp wants). Returns duration in seconds."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise VoiceError("Audio must be a WAV file.")
    pos, fmt, size = 12, None, None
    while pos + 8 <= len(data):
        cid, ln = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", data[pos + 8:pos + 24])
        elif cid == b"data":
            size = min(ln, len(data) - pos - 8)
            break
        pos += 8 + ln + (ln & 1)
    if not fmt or size is None:
        raise VoiceError("Audio file is incomplete.")
    if fmt[0] != 1 or fmt[1] != 1 or fmt[2] != 16000 or fmt[5] != 16:
        raise VoiceError("Audio must be 16 kHz mono 16-bit PCM.")
    secs = size / 32000
    if secs < 0.3:
        raise VoiceError("The recording is too short.")
    if secs > max_seconds + 1:
        raise VoiceError(f"The recording is longer than {max_seconds} seconds.")
    return secs


def stt_command(binary, model, wav, language, threads):
    return [binary, "-m", model, "-f", wav, "-l", language, "-t", str(threads), "-nt", "-np"]


def parse_stt(out):
    lines = [ln.strip() for ln in out.splitlines()]
    return " ".join(ln for ln in lines if ln and ln not in NOISE).strip()


def tts_command(binary, model, out_wav):
    return [binary, "--model", model, "--output_file", out_wav]


def transcribe(c, wav_bytes, threads=4, run=subprocess.run, tmpdir=None):
    b = find_bin(c["stt_bin"], STT_NAMES)
    if not b or not c["stt_model"]:
        raise VoiceError("Speech-to-text is not set up (whisper.cpp program and model).")
    check_wav(wav_bytes, c["max_seconds"])
    with tempfile.TemporaryDirectory(prefix="victor-voice-", dir=tmpdir) as d:
        p = os.path.join(d, "in.wav")
        with open(p, "wb") as f:
            f.write(wav_bytes)
        r = run(stt_command(b, c["stt_model"], p, c["language"], threads), capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise VoiceError("whisper.cpp failed: " + ((r.stderr or "").strip().splitlines() or ["exit " + str(r.returncode)])[-1][:200])
    return parse_stt(r.stdout)


def speak(c, text, run=subprocess.run, tmpdir=None):
    b = find_bin(c["tts_bin"], TTS_NAMES)
    if not b or not c["tts_model"]:
        raise VoiceError("Text-to-speech is not set up (Piper program and voice).")
    text = " ".join(str(text).split())[:2000]
    if not text:
        raise VoiceError("Nothing to say.")
    with tempfile.TemporaryDirectory(prefix="victor-voice-", dir=tmpdir) as d:
        out = os.path.join(d, "out.wav")
        r = run(tts_command(b, c["tts_model"], out), input=text, capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not os.path.isfile(out):
            raise VoiceError("Piper failed: " + ((r.stderr or "").strip().splitlines() or ["no audio written"])[-1][:200])
        with open(out, "rb") as f:
            return f.read()


def pcm_to_wav(pcm, rate=16000):
    """Wrap raw s16le mono PCM (what victor-mic / parec deliver) in a WAV header for whisper.cpp."""
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


class WhisperRecognizer:
    """whisper.cpp as a backend for the existing push-to-talk pipeline (victorptt.Recognizer: accept / finish / reset).

    Audio stays in memory while the button is held and only touches a private temporary folder for the one
    whisper.cpp call. whisper.cpp reports no word confidence, so a non-empty transcript counts as confident and the
    grammar in victorvoice still has to match it."""

    def __init__(self, cfg, threads=4, run=subprocess.run):
        b = find_bin(cfg.get("stt_bin", ""), STT_NAMES)
        if not b or not cfg.get("stt_model") or not os.path.isfile(cfg["stt_model"]):
            raise VoiceError("whisper.cpp is not set up: pick the program and a ggml model in Control Center > Voice "
                             "(nothing is downloaded automatically).")
        self.cfg, self.threads, self.run = dict(cfg), threads, run
        self.reset()

    def reset(self):
        self._buf = bytearray()

    def accept(self, pcm):
        self._buf += pcm

    def finish(self):
        pcm, self._buf = bytes(self._buf), bytearray()
        if len(pcm) < 16000 * 2 * 0.3:
            return "", 0.0
        text = transcribe(self.cfg, pcm_to_wav(pcm), threads=self.threads, run=self.run)
        return text, (0.9 if text else 0.0)
