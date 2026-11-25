"""victor-ctl chats ...: encrypted persistent chats (same vault format as the Arch edition, see chatvault.py).

The passphrase is read from stdin (one line) or typed at a hidden prompt, never taken from argv.
"""
import json, os, sys
from ..common import VictorError, data_path
from .. import chatvault

NAME = "chats"


def register(sp):
    p = sp.add_parser(NAME, help="encrypted chat history on the data partition")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    s.add_parser("init")
    s.add_parser("list")
    s.add_parser("show").add_argument("id")
    snd = s.add_parser("send"); snd.add_argument("message"); snd.add_argument("--id", default="")
    ex = s.add_parser("export"); ex.add_argument("--format", choices=("md", "json"), default="md"); ex.add_argument("--out", required=True)
    cl = s.add_parser("clear"); cl.add_argument("--yes", action="store_true"); cl.add_argument("--forget-vault", action="store_true")
    st = s.add_parser("settings"); st.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE")
    s.add_parser("delete").add_argument("id")


def _passphrase(confirm=False):
    if sys.stdin.isatty():
        import getpass
        p = getpass.getpass("Chat passphrase: ")
        if confirm and getpass.getpass("Repeat passphrase: ") != p:
            raise VictorError("The passphrases do not match.")
        return p
    return sys.stdin.readline().rstrip("\n")


def _vault(unlock=True):
    v = chatvault.Vault(data_path("chat", "vault"))
    if unlock:
        v.unlock(_passphrase())
    return v


def _ask(messages, s):
    from .. import api, llama
    c = llama.load_config()
    key = open(llama.key_path()).read().strip() if os.path.exists(llama.key_path()) else None
    body = {"model": "local", "messages": messages, "temperature": s["temperature"], "max_tokens": s["max_tokens"]}
    try:
        with api._req(c, "/v1/chat/completions", key, body, timeout=600) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except (OSError, ValueError, KeyError, IndexError) as e:
        raise VictorError(f"The local model did not answer (is inference running?): {e}")


def handle(a):
    try:
        if a.cmd == "status":
            return _vault(unlock=False).state()
        if a.cmd == "init":
            return chatvault.Vault(data_path("chat", "vault")).create(_passphrase(confirm=True))
        if a.cmd == "clear":
            if not a.yes:
                raise VictorError("Add --yes to delete every saved chat.")
            return _vault(unlock=False).clear(forget_vault=a.forget_vault)
        v = _vault()
        if a.cmd == "list":
            return v.list()
        if a.cmd == "show":
            return v.get(a.id)
        if a.cmd == "delete":
            return v.delete(a.id)
        if a.cmd == "settings":
            new = {}
            for kv in a.set:
                k, _, val = kv.partition("=")
                new[k] = val
            return v.set_settings(new) if new else v.get_settings()
        if a.cmd == "send":
            cid = a.id or v.new()["id"]
            v.add_message(cid, "user", a.message)
            return v.add_message(cid, "assistant", _ask(v.history(cid), v.settings_cache or v.get_settings()))
        if a.cmd == "export":
            text = v.export(a.format)
            fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            return {"exported": a.out, "warning": "This export is NOT encrypted."}
    except chatvault.VaultError as e:
        raise VictorError(str(e))
    raise VictorError("unknown chats command")
