# Persistence — what survives a reboot

**Only application data on the VICTORDATA partition persists.** The operating system itself is a fresh live image every boot (no live-boot overlay persistence, no Ventoy persistence plugin).

| Survives | Where (`/run/victor/root` → data partition) |
|---|---|
| Models | `models/` |
| Inference settings, endpoint config, capability test records | `settings/llama.json`, `settings/capabilities.json` |
| Chat history | `chat/firefox-profile` (dedicated browser profile used by *Local AI Chat*) |
| Image workflows/outputs (when an image pack is used) | `images/` |
| Kali user work | `kali/` (mounted as `/root` in the container) |
| User documents | `documents/` |
| Owner password hash, API key, TLS key/cert | `secrets/` (0700; password hash 0600) |
| Bounded logs | `logs/` (≤ 10 snapshots × ≤ 1 MB); the journal itself is RAM-only (64 MB cap) |

**Does not survive:** installed packages, files outside the data partition (including `/home/victor` — keep work in `~/VictorData`/`documents/`), Wi-Fi passwords, desktop customisation, running services.

## Discovery (by UUID, never `/dev/sdX`)
At boot `victor-storage.service` runs `victor-priv storage-auto`: partitions with ext4 + label `VICTORDATA` are candidates (found via lsblk, with a direct `blkid -p` probe as fallback).
* exactly one → mounted (`nodev,nosuid,noexec,noatime`) at `/run/victor/data`;
* several → **nothing is mounted**; the Control Center → Storage tab requires an explicit choice;
* none → state `missing`; the user may start an explicit **temporary session** (tmpfs, RAM-only, banner says data is lost).

States and behaviour: `mounted-rw`; `mounted-ro` (dirty filesystem or read-only media: mounted read-only, writes refused, repair offered via `storage-repair` = `e2fsck -p`); `full` (< 256 MiB free: imports blocked,
message says what to delete); `damaged` (mount failed); `missing`; `needs-selection`; `session`.

## Credentials
The image ships the `victor` account **locked**. First run asks for the owner's own password (works offline). Its SHA-512 hash is stored on the data partition and re-applied at the next boot by
`victor-access.service` **only if this machine sealed it** (an HMAC over the drive UUID, file name and contents, made with a root-only key that never sits on the drive). A drive this machine has not approved is plain storage: it never sets the owner's password, `sudo` or SSH keys. On the Debian live USB the key is new every boot, so after each boot the console (tty1, before the login screen) asks you to type your owner password to approve the drive; Enter skips and nothing is applied. The Raspberry Pi keeps the key on its SD card, so an approved drive restores silently (after reflashing the card, approve it again). With no data partition the prompt reappears each boot. Changing or resetting the password needs the current password. A forgotten password: delete `secrets/owner.shadow`
and `secrets/.victor-seal.json` on the data partition from another machine (the next boot treats the drive as unapproved and the account stays locked until you set a new password).

## Flash wear
journald is volatile; logs are snapshots with rotation; the data mount uses `noatime`; no swap; models are written once at import.

## Sharing one drive with the Arch edition

The Debian edition accepts an ext4 partition labelled **`VICTORDATA`** (what `provision-usb.py` writes) **or `TORIDATA`** (what the Arch edition's
`scripts/make-usb.sh` writes), so one data partition can serve either OS. Rules: the label match is exact and case-sensitive; if more than one candidate is
attached (any mix of the two labels) nothing is mounted until you choose one explicitly. Each edition keeps its own directory layout on the partition and
neither migrates or deletes the other's files; secrets (API key, TLS key) are per-edition. Tested by unit tests only (label policy); a real drive shared
between both editions has NOT been tested.
