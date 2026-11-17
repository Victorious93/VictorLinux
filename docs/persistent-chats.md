# Encrypted persistent chats

Chat history and chat settings (system prompt, temperature, max tokens, auto-lock) are saved **encrypted** on the
data partition, so they survive a reboot and move with the stick.

| | Arch edition | Debian edition |
|---|---|---|
| Where | Control Center → **Chat** tab | Control Center (Tk) → **Chat** tab, or `victor-ctl chats ...` |
| Folder | `<data>/chats/vault/` | `<data>/chat/vault/` |

## How it is protected
- You choose a passphrase (10+ characters). It is never written to disk. A key is derived with scrypt
  (N=2^15, r=8, p=1) and held in memory only while the chats are unlocked.
- Each chat is one file `<id>.vlc`: AES-256-GCM with a random nonce. The file name is authenticated, so files cannot be
  swapped or renamed without detection. `vault.json` holds only the KDF parameters, salt and an encrypted check value.
- Arch: the key is dropped on **Lock**, after the auto-lock time (default 30 minutes idle) and when the Control Center stops.
- Debian: `victor-ctl` reads the passphrase from stdin or a hidden prompt, never from the command line. The Tk tab keeps
  it in that window's memory until you press **Lock** or close the window.
- Without `python-cryptography` / `python3-cryptography` the feature refuses to save. It never falls back to plain text.
- Temporary-session mode works, but the banner warns that chats are lost at power-off.

## Export and clear
- **Export Markdown / JSON** writes plain text on purpose. The UI and CLI say so.
- **Clear chats** deletes every chat file; it works while locked. *Also remove the vault* deletes settings and the
  passphrase check too. Flash drives can keep old blocks until overwritten; those blocks were encrypted.

## Talking to the model
The Chat tab sends messages to the local llama-server on 127.0.0.1 (private upstream port on Arch; the configured
local API on Debian). No other address is contacted and no proxy is used.

## CLI (Debian)
```
victor-ctl chats init                 # create (asks twice)
victor-ctl chats list | show ID | delete ID
victor-ctl chats send "question" [--id ID]
victor-ctl chats settings --set temperature=0.3 system_prompt="Be brief"
victor-ctl chats export --format md --out ~/chats.md
victor-ctl chats clear --yes [--forget-vault]
```
