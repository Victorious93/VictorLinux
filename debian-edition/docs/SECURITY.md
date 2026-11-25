# Access defaults

* No shared password: user `victor` is created locked in the image (live-config's default `live` password and `NOPASSWD: ALL` sudoers are bypassed by pre-creating the user and booting with `noroot`; hook `9999` fails the build if either appears).
* AI apps run as `victor`. Root-level actions go through **one** helper (`victor-priv`) with fixed verbs: mount/unmount VICTORDATA (label+type checked, UUID validated), temp session, firewall element, set/reset password
  (current password required once set), SSH enable/revoke. `victor` is not in group `sudo` until first-run sets a password; then `sudo` requires that password. Only `victor-priv` is passwordless.
* SSH: disabled; enable only from Control Center with your **public key**; key-only, `AllowUsers victor`, no root, bound to the address you pick. Host keys are generated at that moment (unique per drive/boot).
* Firewall: nftables default-drop inbound (loopback, established, ICMP, DHCP allowed).
* No keys/passwords in logs or diagnostics; the ISO contains no machine-id, SSH host keys, API keys or certificates (verified by `scripts/verify-iso.sh`).
* Data-drive credentials (F-10): `owner.shadow` and the saved SSH keys on VICTORDATA/TORIDATA are applied at boot only when sealed by this machine (HMAC with a root-only key, bound to the drive UUID). An unapproved drive is plain storage. Approval is at the physical console (`victor-owner-setup`, root only, not in sudoers): type the password the drive claims to hold. Details: `docs/SECURITY_AUDIT.md`.
* Revocation: Control Center → Remote access → *Revoke ALL* (SSH off, keys removed, firewall closed), *Rotate key* (API), `reset-owner` (needs current password).
* Known limitation: any process already running as `victor` can use `victor-priv`'s non-destructive verbs (e.g. mount VICTORDATA, open the firewall port for a configured address). That is the intended single-owner trust model; it cannot set/reset the password or reach other root functionality.
