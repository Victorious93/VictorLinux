# Access and security model

* **No shared password.** `victor` and `root` are locked. The *physical console* auto-logs in `victor` (anyone with the
  USB and the PC is the owner by assumption). Nothing can log in over the network.
* **SSH is off.** Enable it in the Control Center after pasting your **public key**; key-only, `AllowUsers victor`,
  `PermitRootLogin no`, listens only on the address you choose. Password SSH is not supported. Lines with
  `authorized_keys` options (`from="10.0.0.0/8",no-pty ssh-ed25519 ...`) are accepted on both editions. Your keys
  are copied to persistent storage (Arch: `victorlinux/security/ssh_authorized_keys`; Debian/Pi:
  `secrets/ssh_authorized_keys` on `VICTORDATA`) and restored after a reboot; SSH itself stays off until you enable
  it again. In a temporary session the keys are not saved. The Debian edition has an **SSH** tab in the Tk Control
  Center and `victor-ctl ssh status|enable|stop|revoke`.
* **Least privilege.** AI applications run as `victor`. sudo is limited to one validated helper,
  `/usr/local/lib/victorlinux/victor-priv` (mount storage by validated UUID, create a requested folder, SSH key/enable/reset, reboot/poweroff). It has no format, partition, relabel, fsck or recursive-chown command, and refuses folder names containing `..`, control characters or symbolic-link escapes. There is no `NOPASSWD: ALL`.
* **Network exposure.** Services bind to `127.0.0.1`; the inference gateway opens a LAN/VPN address only when you
  set it and have an API key. mDNS advertising is disabled.
* **Reset / revoke.** Control Center: *Revoke all remote access* (API key + bind), *Remove all keys & disable* (SSH; also deletes the saved copy).
  Rebooting the live image resets everything that is not on `VICTORDATA`; delete `VICTORDATA/victorlinux/config/secrets.json`
  to drop stored keys.
* **Local disks are not remote.** The inference gateway and the phone-companion API have no storage, file or volume route; only the loopback-only, token-protected Control Center can list drives ([storage.md](storage.md)).
* **Local UI.** The Control Center listens on loopback only, requires a per-session token, and rejects foreign
  `Host` headers (DNS rebinding).
* **Offline by design.** No telemetry, no auto-update, no cloud fallback. Downloading models is a manual act.

Known limits: a self-signed certificate protects against passive snooping only if the client pins it; the RAM cap
is estimate-based, not a kernel limit; Secure Boot is not supported by this ISO.
