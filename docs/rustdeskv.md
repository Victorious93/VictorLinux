# RustDeskV built into VictorLinux

RustDeskV ([Victorious93/rustdeskV](https://github.com/Victorious93/rustdeskV), Don's RustDesk fork) is preinstalled in
the Arch **headless** and **gui** images and in the Debian edition (which is GUI-only today). It is an included but
removable package: the image builds with or without it, and nothing else in VictorLinux depends on it.

Labels: **[Certain]** = in the code and covered by tests; **[Likely]** = reasoned from RustDesk's source, not run;
**[Guessing]** = not confirmed. No image with this change has been built or booted yet.

## What you get

| Edition | RustDeskV client window | Host service (unattended access) |
|---|---|---|
| Arch **gui** | yes: menu entries *RustDeskV* and *RustDeskV host setup* (Openbox), desktop file, optional autostart | yes, after you set a password |
| Arch **headless** | no desktop, no window | yes, after you set a password (`victor-rustdeskv setup` on the console or over SSH) |
| Debian (GUI) | yes: Xfce menu entries and optional autostart | yes, after you set a password |
| Arch **empty** | not included | not included |

The Raspberry Pi edition is not covered (arm64 needs a different RustDesk package).

## Unattended access needs YOUR password (no default exists)

`victor-rustdeskv.service` is **enabled**, but it only starts when `/var/lib/victorlinux/rustdeskv/enabled` exists, and only
`victor-rustdeskv setup` creates that file. Until then the host is not running and nothing can connect [Certain].

```sh
victor-rustdeskv setup      # hidden prompt, 10+ characters, asked twice
victor-rustdeskv status     # ON/OFF, your RustDeskV ID, which servers it uses (no secrets)
victor-rustdeskv disable    # stop and stay off until the next setup
victor-rustdeskv reset      # disable and delete this PC's ID and password
```

What setup does [Certain for the logic, Likely for RustDesk's side]:

1. Checks the password (length, printable ASCII, not a trivial one). The password goes to the root helper on **stdin**, never in
   a file, setting, log or the Control Center.
2. Starts the service, which applies the policy `verification-method=use-permanent-password` and `approve-mode=password`
   on every start: no one-time passwords, no click-to-accept. If RustDeskV does not confirm the policy within 45 s the unit
   stops (fail closed).
3. Sets the permanent password with `rustdesk --password`. RustDesk's CLI takes it as an argument, so it is visible in the
   process list for a moment on this single-user live system. The helper masks it in every error.
4. If it fails, the marker is removed and the service stopped: nothing stays half-configured.

Persistence follows the rest of VictorLinux: on persistent storage the RustDeskV config (ID, keys, password hash) is kept in
`security/rustdeskv/` (Arch) or `secrets/rustdeskv/` on VICTORDATA (Debian) and bind-mounted over `/root/.config/rustdesk`; in a
temporary session it lives in RAM and you run `setup` again after power-off. The image contains no ID, key or password.

No inbound port is opened: RustDesk connects out to a rendezvous/relay server. The Debian nftables policy and the Arch image
are left unchanged. Direct IP access stays at RustDesk's default (off) [Likely].

## Servers: public RustDesk or your own

`/etc/victorlinux/rustdeskv-relay.conf` is a placeholder, **empty by default** = the public RustDesk servers. To self-host
(hbbs/hbbr), fill in `rendezvous_server`, `relay_server`, optional `api_server`, and `key` (your server's **public** key) as root, then
`systemctl restart victor-rustdeskv`. Values are validated; a bad value stops the host from starting instead of falling back
quietly. Never put a password in that file.

## GUI: autostart and desktop entry

`victor-rustdeskv autostart on` writes `~/.config/autostart/victor-rustdeskv.desktop` (Xfce reads it; the Openbox autostart
script checks for it). `off` removes it. Autostart opens the client window only; it never changes the host policy. GUI builds are
X11; RustDesk's unattended host does not support Wayland [Likely].

## Build and the toggle

| Edition | Default | Leave it out |
|---|---|---|
| Arch | `scripts/build.sh amd gui` and `amd headless` include it | `scripts/build.sh amd gui --no-rustdeskv` or `VICTOR_RUSTDESKV=0` (also for `prepare-sources.sh`) |
| Debian | `sudo debian-edition/build.sh` includes it | `sudo debian-edition/build.sh --no-rustdeskv` or `VICTOR_RUSTDESKV=0` |

Source of the binary (**interim**): `Victorious93/rustdeskV` publishes no release artifacts yet, so both editions pin the upstream
RustDesk **1.5.3** release (the version the fork is based on). It does **not** contain the fork's VictorSuite terminal hand-off.

* Arch: `rustdesk-1.5.3-0-x86_64.pkg.tar.zst`, fetched by `scripts/prepare-sources.sh`, compared with `[checksums] rustdeskv_arch`
  in `.ci/options.ini`, installed offline with `pacman -U --noscriptlet` (the package's own post-install would start its stock
  service). Library dependencies come from the package list.
* Debian: `rustdesk-1.5.3-x86_64.deb`, checked against `RUSTDESKV_DEB_SHA256` in `debian-edition/versions.env` by the
  `0270-rustdeskv` hook. With RustDeskV on, a download or checksum failure **stops** the build.

To switch to the fork's own build, publish a Linux `.pkg.tar.zst` and `.deb` from rustdeskV and change the version, file name and
the two SHA-256 pins; nothing else changes.

## Licence

RustDesk is AGPL-3.0. VictorLinux ships the unmodified upstream release and names its source in `NOTICE`.
