"""Encrypted chat history and chat settings on the persistent data partition (shared by both editions).

* One passphrase per vault, typed by the owner and never written anywhere. Only a scrypt-derived key is held in
  memory while unlocked; it is dropped on lock, after the idle timeout and when the Control Center exits.
* Each chat is its own file <id>.vlc: AES-256-GCM, random 96-bit nonce, file name bound as associated data.
  Chat settings live in settings.vlc the same way. vault.json holds only KDF parameters, salt and a check value.
* Export is plain text on purpose (the owner asks for it); the UI warns first.
* No network access. Without python-cryptography the vault refuses to start; it never falls back to plain text.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time

VERSION = 1
MIN_PASS = 10
ID_RE = re.compile(r"^[a-z0-9]{12}$")
DEFAULT_SETTINGS = {"system_prompt": "", "temperature": 0.7, "max_tokens": 512, "auto_lock_minutes": 30}
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
CHECK = b"victorlinux-chat-vault-v1"
MAX_MESSAGES = 2000
MAX_TEXT = 200_000


class VaultError(Exception):
    pass


def available():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except ImportError:
        return False


def _aead(key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise VaultError("The encryption library (python-cryptography) is missing, so chats cannot be saved encrypted. "
                         "Nothing is stored in plain text instead.")
    return AESGCM(key)


def _kdf(passphrase, salt, params):
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                          maxmem=128 * 1024 * 1024, dklen=32)


def check_passphrase(p):
    if not isinstance(p, str) or len(p) < MIN_PASS:
        raise VaultError(f"Use a passphrase of at least {MIN_PASS} characters. Without it the chats cannot be opened again.")
    if len(set(p)) < 4:
        raise VaultError("That passphrase is too repetitive. Use a few unrelated words.")


def _write(path, data, mode=0o600):
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Vault:
    def __init__(self, directory, clock=time.time):
        self.dir, self.clock = directory, clock
        self.key, self.last_used, self.settings_cache = None, 0.0, None

    @property
    def meta_path(self):
        return os.path.join(self.dir, "vault.json")

    def exists(self):
        return os.path.isfile(self.meta_path)

    def unlocked(self):
        if self.key is not None:
            limit = int((self.settings_cache or DEFAULT_SETTINGS).get("auto_lock_minutes") or 0)
            if limit and self.clock() - self.last_used > limit * 60:
                self.lock()
        return self.key is not None

    def state(self):
        return {"available": available(), "exists": self.exists(), "unlocked": self.unlocked()}

    def _touch(self):
        if not self.unlocked():
            raise VaultError("Chats are locked. Enter the chat passphrase to unlock them.")
        self.last_used = self.clock()

    def create(self, passphrase):
        check_passphrase(passphrase)
        if self.exists():
            raise VaultError("A chat vault already exists here. Unlock it, or remove it first.")
        _aead(b"\0" * 32)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        key = _kdf(passphrase, salt, SCRYPT)
        check = _aead(key).encrypt(nonce, CHECK, b"check")
        meta = {"v": VERSION, "kdf": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode(),
                "check": base64.b64encode(nonce + check).decode()}
        _write(self.meta_path, json.dumps(meta).encode())
        self.key, self.last_used, self.settings_cache = key, self.clock(), dict(DEFAULT_SETTINGS)
        return self.state()

    def unlock(self, passphrase):
        if not self.exists():
            raise VaultError("No chat vault yet. Choose a passphrase to create one.")
        with open(self.meta_path) as f:
            m = json.load(f)
        if m.get("v") != VERSION or m.get("kdf") != "scrypt":
            raise VaultError("This chat vault was made by a newer VictorLinux.")
        key = _kdf(passphrase or "", base64.b64decode(m["salt"]), {k: int(m[k]) for k in ("n", "r", "p")})
        blob = base64.b64decode(m["check"])
        aead = _aead(key)
        try:
            ok = aead.decrypt(blob[:12], blob[12:], b"check") == CHECK
        except Exception:  # noqa: BLE001 - InvalidTag
            ok = False
        if not ok:
            raise VaultError("Wrong chat passphrase.")
        self.key, self.last_used, self.settings_cache = key, self.clock(), None
        self.settings_cache = self.get_settings()
        return self.state()

    def lock(self):
        self.key, self.settings_cache = None, None

    def _seal(self, name, obj):
        self._touch()
        nonce = secrets.token_bytes(12)
        _write(os.path.join(self.dir, name), b"VLC1" + nonce + _aead(self.key).encrypt(nonce, json.dumps(obj).encode(), name.encode()))

    def _open(self, name):
        self._touch()
        with open(os.path.join(self.dir, name), "rb") as f:
            raw = f.read()
        if raw[:4] != b"VLC1":
            raise VaultError(f"{name} is not a VictorLinux chat file.")
        try:
            return json.loads(_aead(self.key).decrypt(raw[4:16], raw[16:], name.encode()))
        except Exception:  # noqa: BLE001
            raise VaultError(f"{name} is damaged or was not written by this vault.")

    # ------------------------------------------------------------- chats --
    def _ids(self):
        try:
            return sorted(f[:-4] for f in os.listdir(self.dir) if f.endswith(".vlc") and ID_RE.match(f[:-4]))
        except OSError:
            return []

    def list(self):
        self._touch()
        out, damaged = [], 0
        for i in self._ids():
            try:
                c = self._open(i + ".vlc")
            except VaultError:
                damaged += 1
                continue
            out.append({"id": i, "title": c.get("title") or "Untitled", "updated": c.get("updated"), "messages": len(c.get("messages", []))})
        out.sort(key=lambda c: -(c["updated"] or 0))
        return {"chats": out, "damaged": damaged}

    def get(self, chat_id):
        if not ID_RE.match(chat_id or "") or not os.path.exists(os.path.join(self.dir, chat_id + ".vlc")):
            raise VaultError("Unknown chat.")
        return self._open(chat_id + ".vlc")

    def new(self, title=""):
        now = int(self.clock())
        c = {"id": "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12)),
             "title": (title or "New chat")[:120], "created": now, "updated": now, "messages": []}
        return self.save(c)

    def save(self, chat):
        if not ID_RE.match(chat.get("id") or ""):
            raise VaultError("Unknown chat.")
        msgs = chat.get("messages", [])
        if len(msgs) > MAX_MESSAGES or any(m.get("role") not in ("user", "assistant") or len(str(m.get("content", ""))) > MAX_TEXT for m in msgs):
            raise VaultError("The chat is too long or has an invalid message.")
        chat["updated"] = int(self.clock())
        self._seal(chat["id"] + ".vlc", chat)
        return chat

    def add_message(self, chat_id, role, content):
        c = self.get(chat_id)
        c["messages"].append({"role": role, "content": str(content), "t": int(self.clock())})
        if role == "user" and c.get("title") in (None, "", "New chat"):
            c["title"] = " ".join(str(content).split())[:60] or "New chat"
        return self.save(c)

    def delete(self, chat_id):
        self.get(chat_id)
        os.unlink(os.path.join(self.dir, chat_id + ".vlc"))
        return {"deleted": chat_id}

    def clear(self, forget_vault=False):
        """Delete every chat; with forget_vault also the settings and the vault. Works while locked, because
        forgetting must not depend on remembering the passphrase."""
        n = 0
        for i in self._ids():
            os.unlink(os.path.join(self.dir, i + ".vlc"))
            n += 1
        if forget_vault:
            for f in ("settings.vlc", "vault.json"):
                if os.path.exists(os.path.join(self.dir, f)):
                    os.unlink(os.path.join(self.dir, f))
            self.lock()
        return {"deleted_chats": n, "vault_removed": bool(forget_vault),
                "note": "Files deleted. A flash drive can keep old blocks until they are overwritten; those blocks were encrypted."}

    # ---------------------------------------------------------- settings --
    def get_settings(self):
        self._touch()
        s = dict(DEFAULT_SETTINGS)
        if os.path.exists(os.path.join(self.dir, "settings.vlc")):
            s.update(self._open("settings.vlc"))
        return s

    def set_settings(self, new):
        s = self.get_settings()
        for k, v in (new or {}).items():
            if k not in DEFAULT_SETTINGS:
                raise VaultError(f"Unknown chat setting: {k}")
            s[k] = v
        try:
            s["system_prompt"] = str(s["system_prompt"])[:8000]
            s["temperature"] = float(s["temperature"])
            s["max_tokens"] = int(s["max_tokens"])
            s["auto_lock_minutes"] = int(s["auto_lock_minutes"])
        except (TypeError, ValueError):
            raise VaultError("Chat settings must be numbers where numbers are expected.")
        if not 0 <= s["temperature"] <= 2 or not 16 <= s["max_tokens"] <= 32768 or not 0 <= s["auto_lock_minutes"] <= 1440:
            raise VaultError("Temperature 0-2, max tokens 16-32768, auto-lock 0-1440 minutes (0 = never).")
        self._seal("settings.vlc", s)
        self.settings_cache = s
        return s

    # ------------------------------------------------------------ export --
    def export(self, fmt="md", chat_id=None):
        self._touch()
        chats = [self.get(i) for i in ([chat_id] if chat_id else self._ids())]
        if fmt == "json":
            return json.dumps({"exported": int(self.clock()), "chats": chats}, indent=2)
        if fmt != "md":
            raise VaultError("Export format must be md or json.")
        out = []
        for c in chats:
            out.append(f"# {c.get('title') or 'Untitled'}\n")
            out += [f"**{m['role'].title()}:** {m['content']}\n" for m in c.get("messages", [])]
        return "\n".join(out)

    def history(self, chat_id):
        """OpenAI-style messages for the next request: system prompt + saved turns."""
        c = self.get(chat_id)
        s = self.settings_cache or self.get_settings()
        msgs = [{"role": "system", "content": s["system_prompt"]}] if s.get("system_prompt") else []
        return msgs + [{"role": m["role"], "content": m["content"]} for m in c["messages"]]
