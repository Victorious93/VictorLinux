"""Voice chat: the browser records 16 kHz WAV, whisper.cpp turns it into text, the local model answers, Piper speaks.

Off by default. Only user-supplied programs and model files are used. Audio exists only in a temp folder for one
request (tmpfs under RUN_DIR when available) and is never saved. The admin API stays on 127.0.0.1.
"""
import base64
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

import victorcommon as tc
import voice

RUN = subprocess.run          # tests inject a fake
WAKE = {"seq": 0}
_LOCK = threading.Lock()


def cfg_path():
    return tc.data_path("config", "voice.json")


def voice_dir():
    return tc.data_path("voice")


def _cfg():
    p = cfg_path()
    return voice.load(p) if p else dict(voice.DEFAULTS)


def _files():
    return voice.model_files([voice_dir()])


def _tmp():
    return tc.RUN_DIR if os.path.isdir(tc.RUN_DIR) and os.access(tc.RUN_DIR, os.W_OK) else None


def state(app, q):
    c = _cfg()
    st = voice.status(c, _files())
    return 200, {"config": c, "status": st, "voice_dir": voice_dir(), "llm_running": app.llm.running(),
                 "storage": bool(cfg_path()), "wake_seq": WAKE["seq"]}


def config(app, b):
    if not cfg_path():
        raise voice.VoiceError("Set up storage first (Storage tab) so the voice settings can be saved.")
    n = voice.update(_cfg(), b, _files())
    voice.save(cfg_path(), n)
    return 200, {"ok": True, "config": n, "message": "Voice settings saved."}


def ask_model(app, messages):
    import control_center as cc
    body = {"model": "victor-local", "messages": messages, "max_tokens": 400}
    req = urllib.request.Request(f"http://127.0.0.1:{cc.UPSTREAM_PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + app.ensure_upstream_key()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # loopback only, never a proxy
    try:
        with opener.open(req, timeout=600) as r:
            return json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError) as e:
        raise voice.VoiceError(f"The local model did not answer: {e}")


def _history(b):
    out = []
    for m in (b.get("history") or [])[-12:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            out.append({"role": m["role"], "content": m["content"][:4000]})
    return out


def ask(app, b):
    c = _cfg()
    if not c["enabled"]:
        raise voice.VoiceError("Voice chat is off. Turn it on in the Voice tab first.")
    try:
        wav = base64.b64decode(str(b.get("audio") or ""), validate=True)
    except ValueError:
        raise voice.VoiceError("Audio data is not valid base64.")
    with _LOCK:                                   # one transcription at a time keeps RAM predictable
        text = voice.transcribe(c, wav, threads=max(1, (os.cpu_count() or 2) // 2), run=RUN, tmpdir=_tmp())
    if not text:
        raise voice.VoiceError("Didn't catch that. Try again a bit closer to the microphone.")
    res = {"text": text}
    if b.get("transcribe_only"):
        return 200, res
    if not app.llm.running():
        raise voice.VoiceError("No model is running. Start one on the Dashboard first.")
    res["reply"] = ask_model(app, _history(b) + [{"role": "user", "content": text}])
    if c["speak_replies"] and voice.status(c, _files())["tts_ready"]:
        try:
            res["audio"] = base64.b64encode(voice.speak(c, res["reply"], run=RUN, tmpdir=_tmp())).decode()
        except voice.VoiceError as e:
            res["tts_error"] = str(e)
    return 200, res


def say(app, b):
    c = _cfg()
    return 200, {"audio": base64.b64encode(voice.speak(c, b.get("text") or "Voice output is working.", run=RUN, tmpdir=_tmp())).decode()}


def wake(app, b):
    """Wake-word hook: a local detector (e.g. the optional Coral side task) calls this; an open Voice tab with
    hands-free on starts listening. Admin API, 127.0.0.1 only."""
    WAKE["seq"] += 1
    return 200, {"ok": True, "wake_seq": WAKE["seq"]}


def _wrap(fn):
    def w(app, data):
        try:
            return fn(app, data)
        except voice.VoiceError as e:
            return 400, {"error": str(e)}
    return w


ROUTES = {("GET", "/api/voice/state"): state, ("POST", "/api/voice/config"): _wrap(config), ("POST", "/api/voice/ask"): _wrap(ask),
          ("POST", "/api/voice/say"): _wrap(say), ("POST", "/api/voice/wake"): wake}
