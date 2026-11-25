# VictorLinux security audit (VIC-6)

Audit date: 2026-11-24/25 (America/New_York). Base: `main` at `f5cf849`, then `17203da`. Scope: Arch edition (repo root), Debian edition
(`debian-edition/`), Pi edition (`pi-edition/`), the fleet coordinator (`fleet/`), `voip/`, build scripts and CI.
Method: reading the code at the revisions above, writing unit/integration tests for each fix, and reproducing the one bug that could be
reproduced without hardware. Nothing was audited on a booted image.

Labels: **[Certain]** = seen in the code and demonstrated (test or script) or trivially true from the code; **[Likely]** = follows from the
code but the end-to-end effect was not run; **[Guessing]** = plausible, not verified. "Status" says whether a fix is **merged**, in an **open PR**, or **open**
(no fix).

## What "done" means in this report

| Level | Meaning here |
|---|---|
| Implemented | the change is in a PR |
| Built | an ISO/image containing it was built (CI `iso`/`image`/`build` jobs, per PR) |
| Tested | unit/integration tests pass (`pytest tests`, fleet suite) |
| Booted | run on real hardware or in the QEMU boot test (CI `iso` job boots the Debian ISO in QEMU) |

I did **not** boot anything myself. "Built/booted" for a PR is whatever its CI shows when it merges; each PR body states what was and was not exercised.
Two failures appear in a local full run on this box (`test_missing_adb_message`, `test_headless_status_without_adb`): `adb` is installed here; CI is green.

## Summary

No confirmed unauthenticated remote code execution. The strongest findings are local privilege escalation through root helpers that follow symlinks
(F-01, F-03), command injection into the Metasploit console for any caller holding a security-backend credential (F-04), a one-packet-stream pre-auth
denial of service in three TLS services (F-02), and a browser-reachable admin API on loopback (F-05). Fixes for all of these are in small PRs (below).
The design-level items that I did **not** change are listed under "Open".

## Findings

| ID | Severity | Finding | Where (at f5cf849 unless noted) | Label | Status |
|---|---|---|---|---|---|
| F-01 | High | Root helpers write/chown/chmod inside `/home/victor/.ssh` and folders on an untrusted data drive through plain `open`/`chown`/`os.replace` with a predictable temp name: a planted symlink makes root overwrite or chown any file (victor -> root) | `victor-priv:114,117,255,457,461` (Arch), `victorssh.py:105-111,138`, Debian `victor-priv:85,238`, `victor-pi-priv:33-35,113-114` | Certain (code + test of the fix) | **merged** #41 (`1afcd43`) |
| F-04 | High | `secbackend` builds Metasploit console lines from job parameters. A bare `\n` in an option value, `target_host` or `payload`, or a trailing `\n` in an option key (`re.match(...$)`), starts a second console command (`irb`/`ruby` = code execution). Needs a backend credential | `secprotocol.py:143` (only `\0`,`\r` blocked), `:137,179` (`$`), `secbackend.py:134,138`, `secmsf.py:184` | Certain by code + tests; not run against a real msfrpcd | **merged** #43 (`bdfc9df`) |
| F-02 | Medium | One idle TCP connection stalls a whole TLS service: `server.socket = ctx.wrap_socket(...)` makes `accept()` do the handshake in the single serve loop, no timeout. victor-input, gateway (TLS mode) and fleet coordinator; the VMIC1 mic receiver does its handshake in the accept loop too (bounded by its idle timeout, so repeated idle connections still serialise it) | `victorinput.py:714`, `gateway.py:454`, `fleet/victorfleet/server.py:70`, `victormic.py:743-748` | Certain (reproduced, Python 3.13) | PR #44 |
| F-03 | Medium | Mesh root helper `chown`/`makedirs`/writes in the mesh state folder, which the unprivileged user owns after `ensure_dirs` (settings, share auth, share/received dirs) | `victor-mesh-priv:40-47`, `victormesh.py:170-181` | Certain by code + tests of the fix | PR #48 |
| F-05 | Medium | victor-input admin API (127.0.0.1:8472) has no Host/Origin check and parses any POST body as JSON: a web page in the owner's browser can POST `/pair-start`, `/revoke` (no CORS preflight for `text/plain`); with DNS rebinding the QR/pairing URI is readable | `victorinput.py:754-760,847-873` | Likely (tests send the crafted requests; no real browser) | PR #44 |
| F-09 | Medium | Build downloads not pinned: llama UI bundle checked against a `.sha256` from the same server; `nvidia-pstated` (runs as a root service on CUDA images) downloaded with no check | `scripts/prepare-sources.sh:31-47` | Certain | **merged** #42 (`17203da`) |
| F-10 | Medium | Debian/Pi: a lone ext4 partition labelled `VICTORDATA`/`TORIDATA` is auto-mounted, and `victor-access` (enabled at boot) installs `secrets/owner.shadow` from it as the owner's password hash and adds `victor` to `sudo`. A hostile or forged drive gives the attacker a known sudo password on the next boot | `victorlib/storage.py:27-55`, `victor-priv:302-311`, `victor-access.service` | Certain mechanism; exploit path Likely | **Open** |
| F-13 | Medium | While the owner account is still locked (first run, or no data drive) `victor-priv password` needs no authentication, and `victor` has NOPASSWD sudo to `victor-priv`: any process running as `victor` (browser, a model server, anything the owner runs) can set a password and become root in two commands. The password gate only protects later changes | `victor-priv:169-198`, `etc/sudoers.d/victor-priv:3` | Certain (design) | **Open** |
| F-06 | Low | `int(Content-Length)` then `rfile.read(n)`: a negative value blocks the handler thread until the client closes, garbage raises. victor-input, gateway, secbackend, fleet | `victorinput.py:755`, `gateway.py:147,217,366`, `secbackend.py:194`, `fleet/.../server.py:207` | Certain | PR #44 |
| F-07 | Low | victor-voip writes `pjsip.conf` (plaintext SIP/trunk passwords) with the default umask in 0755 dirs; the trunk password is not validated (a newline adds config lines) | `voip/victor_voip.py:140,185` | Certain | **merged** #45 (`78c43fc`) |
| F-08 | Low | `hmac.compare_digest(str, str)` raises `TypeError` on non-ASCII input: VMIC1 hello MAC, fleet terminal binding/attach token, content hash, Control Center token header (connection dropped instead of 401) | `victormic.py:472`, `fleet/.../terminal.py:186,239`, `messaging.py:104`, `control_center.py` `_auth` | Certain (test) | PR #44 |
| F-11 | Low | Debian/Pi hotspot (NetworkManager) lacks the AP isolation the Arch hotspot has (`ap_isolate=1`), so joined phones can reach each other | `hotspot.py:104-113` | Certain from code; not tested on hardware | PR #46 |
| F-12 | Low | `tailscaled.state` lives in the mesh folder owned by `victor`; root `tailscaled` reads/writes it there, so `victor` can swap or race the file. Moving it needs a root-owned state dir that still persists on the data drive | `victormesh.py:153,211`, `victor-mesh-priv:43` | Likely | **Open** |
| F-14 | Low | Arch edition has no host firewall (the Debian/Pi editions ship nftables, default drop). Protection is "services are off and bind where told"; Arch services that listen are opt-in | `airootfs/` (no nftables/iptables config) | Certain; documented design | **Open** |
| F-15 | Low | Fleet coordinator HTTPS has no per-request read timeout (slowloris on an authenticated-or-not connection). Not changed because terminal relay sockets are long-lived | `fleet/victorfleet/server.py` handlers | Likely | **Open** |
| F-16 | Low | Optional packs: `npm install --global sillytavern` is unpinned; ComfyUI is pinned by tag (mutable), not commit. Build-time only, marked experimental | `airootfs/root/customize_airootfs/scripts/1000-sillytavern.sh.jinja2:5`, `.ci/options.ini` `comfyui` | Certain | **Open** |
| F-17 | Info | `victor-input` binds `0.0.0.0` by default **when the owner enables it** (TLS + per-device bearer + single-use pairing token are required; off by default) | `victorinput.py:642` | Certain | Documented |
| F-18 | Info | Chat vault: no unlock throttling (local UI only; scrypt n=2^15 makes guessing slow), and a vault.json with a non-integer KDF value raises instead of "wrong passphrase" | `chatvault.py:117-131` | Certain | Open (cosmetic) |

PR links (all https://github.com/Victorious93/VictorLinux/pull/N): #41, #42, #43, #45 merged; #44 (TLS/admin/Content-Length), #46 (hotspot), #48 (mesh helper) open at the time of writing; #47 is this report.

## Verified positives (checked, no change needed)

- Web Control Center: binds 127.0.0.1, per-session token header compared in constant time, Host allow-list, CSP with a per-response nonce, 1 MiB body cap (`control_center.py`, `_host_ok`, `_auth`, `_body`, `do_GET`).
- Gateway: authentication required unless bound to loopback (`gateway.py:129`), API keys stored only as SHA-256 and compared with `compare_digest` (`apikeys.py:30,91-98`), usage log 0600, no request bodies or keys in logs, pairing refuses plain HTTP off loopback (`gateway.py:146`), pairing tokens single-use and attempt-limited (`apikeys.py:255`).
- Mesh: share server uses scrypt-hashed Basic auth with constant-time compare (`victormesh.py:233-253`), only reachable over `tailscale0` (nftables `mesh_tcp` set on Debian/Pi), pre-auth key is passed to `tailscale up` by a 0600 file under the root-owned run dir that is zeroed and deleted, and masked in errors (`victor-mesh-priv:76-94`), mesh sshd drop-in forces key-only login and `PermitRootLogin no` (`victormesh.py:216`).
- Debian/Pi: owner account created and locked (`passwd -l`), no live-user password, no `NOPASSWD: ALL` (build verification `verify-iso.sh:17-18`, QEMU test asserts it), sudoers allows only the two validated helpers, nftables `policy drop` on input with explicit sets, hotspot interfaces dropped on forward (`nftables.conf:11-27`).
- Ext loader: modules are imported only from the root-owned code directory inside the image, never from the data drive; chats are AES-256-GCM with scrypt, file name bound as AAD, no plaintext fallback (`chatvault.py`).
- Ext loader modules: `cc_ext/__init__.py:22-27` imports only from its own (image) directory. `imagegen` runs its external program with an argument list (no shell) and requires an executable absolute path set through the authenticated Control Center (`imagegen.py:65-79`). voice/tune/coral were only skimmed (they also use `subprocess.run` with lists, no `shell=True`).
- Build: llama.cpp pinned to an exact commit and re-verified (`prepare-sources.sh:18-23`, Debian/Pi hooks), Tailscale apt keyring pinned by SHA-256 (`0260-optional-tailscale.hook.chroot:42`), Raspberry Pi archive key fingerprint-checked (`pi-edition/build.sh:48`).
- Repo: no private keys, tokens or credentials found by pattern scan (the only match is the Pi test that performs this same scan); workflows use `permissions: contents: read` or narrower, no `pull_request_target`, no repository secrets used.
- Network services are off by default: no `victor-input` or mesh share unit is enabled at boot in any edition, and the remote gateway setting defaults to disabled on 127.0.0.1 (`control_center.py:927`). Only storage/access/NetworkManager/nftables (and Pi board/grow/key units) are enabled (`0200-victor-config.hook.chroot:49`, `10-pi-config.sh:62-65`). The fleet coordinator was not checked for a boot-enabled unit.

## Not audited, or only lightly

- Tk Control Center and `victorlib/api.py` (Debian): not reviewed in this pass.
- Pi nftables rules and `victor-pi-board`: not reviewed beyond the hooks above.
- Bluetooth pairing, phone-as-keyboard input injection beyond the WSS auth, SET (`secset.py`) end-to-end behaviour, Asterisk's own hardening: code read only where stated above.
- Everything about the running images: no boot, no hardware, no Wi-Fi/NetworkManager, no real Metasploit/Asterisk/Headscale.

## Recommended next steps (not done)

1. F-13/F-10: gate first-run password setup behind a GUI-only polkit/pkexec prompt, and only auto-restore `owner.shadow` from a drive the owner paired explicitly (record its UUID), not from any labelled partition.
2. F-12: keep `tailscaled.state` in a root-owned directory and copy it to/from the data drive with the `safefs` helpers.
3. F-14: add a small nftables default-drop profile to the Arch edition, with the same sets as Debian.
4. F-15/F-16: pin the optional packs by commit and add request read timeouts to the fleet server.
