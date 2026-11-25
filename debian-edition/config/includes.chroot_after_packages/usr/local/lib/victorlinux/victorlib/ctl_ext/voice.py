"""victor-ctl voice ...: offline voice chat with user-supplied whisper.cpp + Piper (same rules as the Arch edition).

Off by default. `listen` records from the default microphone with arecord (alsa-utils) into a temp folder that is
deleted afterwards; `ask FILE.wav` works without a microphone. Nothing is downloaded.
"""
import json, os, shutil, subprocess, tempfile
from ..common import VictorError, data_path
from .. import voice

NAME = "voice"
RUN = subprocess.run


def register(sp):
    p = sp.add_parser(NAME, help="offline voice chat (whisper.cpp + Piper you provide); off by default")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    st = s.add_parser("set"); st.add_argument("assign", nargs="+", metavar="KEY=VALUE")
    s.add_parser("transcribe").add_argument("wav")
    sy = s.add_parser("say"); sy.add_argument("text"); sy.add_argument("--out", required=True)
    ak = s.add_parser("ask"); ak.add_argument("wav"); ak.add_argument("--speak", action="store_true", help="play the reply with aplay")
    ls = s.add_parser("listen"); ls.add_argument("--seconds", type=int, default=6); ls.add_argument("--speak", action="store_true")


def _cfg():
    return voice.load(data_path("config", "voice.json"))


def _files():
    return voice.model_files([data_path("voice")])


def _parse(assign, files):
    b = {}
    for x in assign:
        k, sep, v = x.partition("=")
        if not sep:
            raise VictorError(f"Use KEY=VALUE, not {x}")
        if k in ("enabled", "speak_replies"):
            b[k] = v.lower() in ("1", "true", "yes", "on")
        elif k == "max_seconds":
            b[k] = int(v) if v.isdigit() else -1
        elif k in ("stt_model", "tts_model") and v and not os.path.isabs(v):
            b[k] = os.path.join(data_path("voice"), v)
        else:
            b[k] = v
    return b


def _ask_model(text):
    from .. import api, llama
    c = llama.load_config()
    key = open(llama.key_path()).read().strip() if os.path.exists(llama.key_path()) else None
    body = {"model": "local", "messages": [{"role": "user", "content": text}], "max_tokens": 400}
    try:
        with api._req(c, "/v1/chat/completions", key, body, timeout=600) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except (OSError, ValueError, KeyError, IndexError) as e:
        raise VictorError(f"The local model did not answer (is inference running?): {e}")


def _converse(c, wav_bytes, speak):
    text = voice.transcribe(c, wav_bytes, threads=max(1, (os.cpu_count() or 2) // 2), run=RUN)
    if not text:
        raise VictorError("Didn't catch that.")
    out = {"text": text, "reply": _ask_model(text)}
    if speak:
        audio = voice.speak(c, out["reply"], run=RUN)
        player = shutil.which("aplay") or shutil.which("pw-play") or shutil.which("paplay")
        if not player:
            out["note"] = "No audio player (aplay/pw-play/paplay) found; reply not played."
        else:
            with tempfile.TemporaryDirectory(prefix="victor-voice-") as d:
                p = os.path.join(d, "reply.wav")
                with open(p, "wb") as f:
                    f.write(audio)
                RUN([player, p], capture_output=True, timeout=300)
    return out


def handle(a):
    try:
        c = _cfg()
        if a.cmd == "status":
            return voice.status(c, _files()) | {"config": c, "voice_dir": data_path("voice")}
        if a.cmd == "set":
            n = voice.update(c, _parse(a.assign, _files()), _files())
            voice.save(data_path("config", "voice.json"), n)
            return n
        if not c["enabled"]:
            raise VictorError("Voice chat is off. Turn it on with: victor-ctl voice set enabled=true")
        if a.cmd == "transcribe":
            with open(a.wav, "rb") as f:
                return {"text": voice.transcribe(c, f.read(), run=RUN)}
        if a.cmd == "say":
            data = voice.speak(c, a.text, run=RUN)
            with open(a.out, "wb") as f:
                f.write(data)
            return {"written": a.out, "bytes": len(data)}
        if a.cmd == "ask":
            with open(a.wav, "rb") as f:
                return _converse(c, f.read(), a.speak)
        if a.cmd == "listen":
            if not shutil.which("arecord"):
                raise VictorError("arecord (alsa-utils) is not installed; record a 16 kHz mono WAV and use `voice ask FILE`.")
            secs = max(2, min(a.seconds, c["max_seconds"]))
            with tempfile.TemporaryDirectory(prefix="victor-voice-") as d:
                p = os.path.join(d, "in.wav")
                r = RUN(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(secs), p], capture_output=True, text=True, timeout=secs + 15)
                if r.returncode != 0:
                    raise VictorError("Recording failed: " + (r.stderr or "").strip()[:200])
                with open(p, "rb") as f:
                    data = f.read()
            return _converse(c, data, a.speak)
    except voice.VoiceError as e:
        raise VictorError(str(e))
    except OSError as e:
        raise VictorError(str(e))
