# Victorious Mesh on VictorLinux

Victorious Mesh is Don's tailscale-android fork ([Victorious93/victorious-mesh](https://github.com/Victorious93/victorious-mesh))
that talks to **your own Headscale** server. VictorLinux joins the same mesh with the **standard `tailscale`/`tailscaled`**,
so the app sees the PC (or Pi) as a normal peer. It works on the Arch GUI and headless builds, the Debian edition and the
Raspberry Pi edition.

**Everything is off by default.** `tailscaled` is installed but disabled. Nothing about your server is built in, and
nothing starts until you join.

Labels: **[Certain]** = in the code and covered by tests; **[Likely]** = reasoned, not tried on hardware;
**[Guessing]** = not confirmed.

## Join

| Where | How |
|---|---|
| Arch web Control Center | **Mesh** tab → Headscale URL, pre-auth key, optional device name → **Join mesh** |
| Debian / Pi desktop (Tk Control Center) | **Mesh** tab, same fields |
| Any build, headless, Pi Zero 2 W | `victor-mesh join --url https://headscale.example.com [--name my-pc]` (the key prompt is hidden) |

Make the key on your Headscale server:

```sh
headscale preauthkeys create --user don --expiration 1h --tags tag:victorlinux      # PCs
headscale preauthkeys create --user don --expiration 1h --tags tag:victorlinux-pi   # Raspberry Pi edition
```

What happens [Certain]:

- The URL must be `https://`. `http://` is only accepted for a private or loopback IP address or a `.local`, `.lan` or `.home.arpa` name.
- The key goes from the screen or prompt to the root helper (`victor-mesh-priv`) on **stdin**. It's written to a 0600 file in
  `/run/victor`, passed as `tailscale up --auth-key=file:…`, then zeroed and deleted. It never lands on the
  command line, in settings, logs or the Control Center status, and an error message that echoes it is masked.
- Flags used: `--login-server=<your URL> --advertise-tags=tag:victorlinux` (or `tag:victorlinux-pi` when
  `/etc/os-release` says `VARIANT_ID=pi`), `--operator=victor --accept-dns=false --accept-routes=false --ssh=false --reset`.
- The node identity (`tailscaled.state`) is kept on persistent storage: Arch `victorlinux/security/mesh/`, Debian/Pi
  `secrets/mesh/` on VICTORDATA. After a reboot, **Connect** (`victor-mesh up`) rejoins without a new key. In a
  temporary session it lives in RAM, so you must join again after power-off.
- `tailscaled` is started for this boot only (a drop-in in `/run/systemd`). It's never enabled at boot.

`victor-mesh status`, `up` (connect), `down` (disconnect, keeps identity) and `leave` (log out, delete identity and
settings; shared and received files stay). After **leave**, also delete the node in Headscale: `headscale nodes delete`.

## Options (each off by default)

| Option | What it does | Notes |
|---|---|---|
| Exit node | `--advertise-exit-node` plus IP forwarding | Approve it in Headscale (`headscale nodes approve-routes` / `routes enable`). [Likely] |
| Wake-on-LAN | `ethtool -s <wired nic> wol g` while connected; `victor-mesh wake MAC` sends a magic packet on this machine's LAN | A Pi left on can wake PCs on its LAN. Waking over the mesh itself isn't possible (the target is off). [Likely] |
| Taildrop receive | `tailscale file get --loop --conflict=rename` into `…/mesh/received` (runs as `victor`) | [Guessing] whether your Headscale version delivers Taildrop to a **tagged** node. Use the file share as the fallback. |
| SSH over the mesh | sshd drop-in `15-victor-mesh.conf`: `ListenAddress <mesh IP>`, key-only, `AllowUsers victor`; Debian/Pi also open 22 **only on `tailscale0`** | Needs a key first (SSH tab). Keys persist across reboots. |
| File share | Read-only HTTP on **the mesh IP only**, port **8090** | Compatible with the app's share, see below |

## File share (compatible with the Victorious Mesh app)

Same contract as the app's *Mesh file share* (`MeshShareModels.kt`) [Certain]:

- It listens on this machine's mesh address only, never on `0.0.0.0`. It refuses to start for an address outside `100.64.0.0/10` or `fd7a:115c:a1e0::/48`.
- Every request needs HTTP Basic auth. **Any user name** works, and the password is generated (24 characters). Only a salted
  scrypt hash is stored, so the password is shown once. **Make a new share password** replaces it. Ten wrong tries pause that
  address for a minute.
- `GET /` is a dark index page and `GET /f/<name>` downloads a file (`Content-Disposition: attachment`, Range supported).
  `POST`, `PUT` and `DELETE` get **405**. There are no folders and no hidden files, and symlinks are never followed.
- The share folder is capped at **2 GB**. `victor-mesh share add FILE` copies files in (originals untouched).
  `share remove NAME` and the Control Center's **Stop sharing** take them out.
- From the phone app: **Open another device's share**. From a computer: `curl -u mesh:PASSWORD -O http://100.64.0.9:8090/f/notes.txt`.
- The server runs as `victor` (`victor-mesh-share.service`, no `[Install]` section) with a read-only filesystem view.

## Proposed Headscale ACL

Don's phones may reach VictorLinux machines on SSH (22), the inference gateway (8080) and the file share (8090). The
machines themselves can't reach the phones unless you add that. Replace `don@` with your Headscale user.

```json
{
  "tagOwners": {
    "tag:victorlinux":    ["don@"],
    "tag:victorlinux-pi": ["don@"]
  },
  "acls": [
    { "action": "accept",
      "src": ["don@"],
      "dst": ["tag:victorlinux:22,8080,8090", "tag:victorlinux-pi:22,8080,8090"] }
  ]
}
```

- If your phones also carry a tag (for example `tag:phone`), use that tag as `src` instead of the user.
- The gateway on 8080 still needs its own API key and must be bound to the mesh address on purpose (Server & Access,
  `docs/remote-access.md`). The Control Center admin UI stays on `127.0.0.1` and is never reachable over the mesh.
- For an exit node, Headscale also needs the route approved.

## Security and privacy

- No telemetry. VictorLinux adds no reporting of its own. `tailscaled` talks only to the control server you typed in
  (and the DERP relays that server hands out).
- `--accept-dns=false` and `--accept-routes=false`, so joining doesn't change DNS or routes on this machine.
- Debian/Pi firewall: the `inet victor` table now has a `mesh_tcp` set that is accepted **only on `tailscale0`**. The helper adds
  22 or 8090 only while that option is on. Arch has no host firewall, so there the guard is that both services
  bind to the mesh IP only.
- Tailscale on Debian/Pi comes from Tailscale's apt repository at **build time**. The keyring is pinned by SHA-256 in
  `debian-edition/versions.env`, and the repository is removed from the image afterwards (no runtime updates). If the
  checksum doesn't match, the image builds without tailscale and the Mesh screen says so.

## Status

- [Certain] Implemented: CLI, root helper, both Control Center screens, units, firewall set, packaging, tests
  (`tests/test_victor_mesh.py`: URL/key validation, tag choice, `tailscale up` flags, key never in argv/settings,
  share server auth, 405 on writes, path traversal, cap, Range, mesh-only bind).
- [Certain] **Never booted and never joined to a real Headscale.** CI builds the images. No phone was tested against it.
- [Likely] Joining, exit node and SSH work as described with tailscale ≥ 1.60 (`--auth-key=file:` support).
- [Guessing] Taildrop to tagged nodes on Headscale.
