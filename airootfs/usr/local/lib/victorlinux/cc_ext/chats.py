"""Chat tab: encrypted, persistent chat history and chat settings (chatvault) + a chat box for the local model.

The model is reached on 127.0.0.1 only (the private llama-server port behind the gateway). Nothing else is contacted.
"""
import json
import urllib.error
import urllib.request

import chatvault
import victorcommon as tc

_VAULT = {"v": None, "dir": None}


def vault():
    d = tc.data_path("chats", "vault")
    if d is None:
        raise chatvault.VaultError("Choose storage first (Storage tab). Chats are saved on the data partition.")
    if _VAULT["dir"] != d:                      # storage changed: forget the key for the old location
        _VAULT["v"], _VAULT["dir"] = chatvault.Vault(d), d
    return _VAULT["v"]


def _wrap(fn):
    def inner(app, data):
        try:
            return 200, fn(app, data)
        except chatvault.VaultError as e:
            return 400, {"error": str(e)}
    return inner


def state(app, q):
    mode = tc.persist_state().get("mode")
    st = {"available": chatvault.available(), "exists": False, "unlocked": False, "storage": mode,
          "persistent": mode == "persistent", "model_running": app.llm.running()}
    if tc.data_path("chats") is not None:
        st.update(vault().state())
    if mode != "persistent":
        st["warning"] = "Temporary session: chats are encrypted but kept in RAM and lost at power-off. Choose a VICTORDATA partition to keep them."
    if st["unlocked"]:
        st["settings"] = vault().get_settings()
    return st


def setup(app, b):
    if b.get("passphrase") != b.get("confirm"):
        raise chatvault.VaultError("The two passphrases do not match.")
    return vault().create(b.get("passphrase"))


def unlock(app, b):
    return vault().unlock(b.get("passphrase") or "")


def lock(app, b):
    vault().lock()
    return {"ok": True, "message": "Chats locked. The key was removed from memory."}


def chat_get(app, q):
    return vault().get((q.get("id") or [""])[0])


def chat_new(app, b):
    return vault().new(str(b.get("title") or ""))


def chat_delete(app, b):
    return vault().delete(str(b.get("id") or ""))


def clear(app, b):
    if b.get("confirm") is not True:
        raise chatvault.VaultError("Tick the confirmation first. This deletes every saved chat.")
    return vault().clear(forget_vault=bool(b.get("forget_vault")))


def settings(app, b):
    return vault().set_settings(b.get("settings") or {})


def export(app, q):
    fmt = (q.get("fmt") or ["md"])[0]
    cid = (q.get("id") or [None])[0]
    return {"format": fmt, "filename": f"victorlinux-chats.{'json' if fmt == 'json' else 'md'}",
            "text": vault().export(fmt, cid), "warning": "This export is NOT encrypted. Store it somewhere safe."}


def ask_model(app, messages, s):
    """One non-streaming completion from the local llama-server (loopback, private upstream key)."""
    import control_center as cc
    body = {"model": "victor-local", "messages": messages, "temperature": s["temperature"], "max_tokens": s["max_tokens"]}
    req = urllib.request.Request(f"http://127.0.0.1:{cc.UPSTREAM_PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + app.ensure_upstream_key()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy
    try:
        with opener.open(req, timeout=600) as r:
            return json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError) as e:
        raise chatvault.VaultError(f"The local model did not answer: {e}")


def send(app, b):
    v = vault()
    text = str(b.get("message") or "").strip()
    if not text:
        raise chatvault.VaultError("Type a message first.")
    if not app.llm.running():
        raise chatvault.VaultError("No model is running. Start one on the Dashboard first.")
    cid = str(b.get("id") or "") or v.new()["id"]
    v.add_message(cid, "user", text)
    reply = ask_model(app, v.history(cid), v.settings_cache or v.get_settings())
    return v.add_message(cid, "assistant", reply)


def shutdown(app):
    if _VAULT["v"] is not None:
        _VAULT["v"].lock()


ROUTES = {
    ("GET", "/api/chats/state"): _wrap(state),
    ("GET", "/api/chats/list"): _wrap(lambda app, q: vault().list()),
    ("GET", "/api/chats/get"): _wrap(chat_get),
    ("GET", "/api/chats/export"): _wrap(export),
    ("POST", "/api/chats/setup"): _wrap(setup),
    ("POST", "/api/chats/unlock"): _wrap(unlock),
    ("POST", "/api/chats/lock"): _wrap(lock),
    ("POST", "/api/chats/new"): _wrap(chat_new),
    ("POST", "/api/chats/send"): _wrap(send),
    ("POST", "/api/chats/delete"): _wrap(chat_delete),
    ("POST", "/api/chats/clear"): _wrap(clear),
    ("POST", "/api/chats/settings"): _wrap(settings),
}
