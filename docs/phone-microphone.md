# Phone microphone, PC detect and three-phone control (owner guide)

Work by Victorious. Status words below are literal: **Implemented**, **Built**, **JVM/host-tested**, **Emulator-tested**, **Physically verified**. Nothing here has been run on a physical phone.

## Choose a microphone
`victor-mic list | status | use default | use pc <stable-id> | use phone <DEVICE> --provider droidcommand|keyboard | prefer-phone on --device D --provider P | virtual on [--set-default]`.
Settings live in `~/.config/victorlinux/mic.json` (0600) and are keyed by stable device identity and provider. A manual PC or phone choice is **never** replaced silently by a hot-plugged headset; only the explicit "use my preferred phone when no PC mic is available" fallback can substitute, and only with live, unexpired consent. A stored preference can never start capture: consent exists only in memory on the PC and expires (default 1 h, max 8 h) or on reboot.

## Pair a phone as a microphone
1. On the PC: `victor-mic serve --bind <LAN-or-VPN-address> --offer Nord:nord1:keyboard` (or `:droidcommand`). It prints one `vmic1;…` offer line (contains a one-time secret; show it only to the phone owner). Never bind a public address.
2. Phone: **Victorious Keyboard → PC microphone** (toolbar tile or Settings → Tools) or **DroidCommand-AI → My PC → Use this phone as microphone**. Paste the line, allow the microphone on that screen, start. A notification names the PC and has Stop and Mute.
3. Creating a new offer for the same handset releases the other app (explicit handover on one phone).
4. Optional: `--virtual-mic` publishes the phone as a user-session source `victor_phone_mic` for any Linux recorder (PulseAudio/pipewire-pulse). It is **not** made the system default unless you also pass `--set-default`; stopping removes it and restores the old default.
Stop/revoke: Stop on the phone, or end `serve`; expiry also ends it.

## What is and is not covered
| Item | Implemented | Built | JVM/host-tested | Emulator | Physical |
|---|---|---|---|---|---|
| Mic settings/resolution, hot-plug rules, consent expiry, handover (Python) | yes | n/a | yes (25 tests) | – | – |
| VMIC1 stream: pinned TLS + HMAC bound to PC/phone/app, bounded buffer, replay drop | yes | n/a | yes, incl. Kotlin client ↔ real Python receiver | – | – |
| Virtual mic module handling | yes | n/a | with a fake `pactl` only | – | – |
| Voice grammar, echo guard, tile model, input routing/epochs, coordinate mapping | yes | n/a | yes | – | – |
| Detect PC / read-aloud policy (privacy filters) | yes | n/a | yes with a fake accessibility backend | – | – |
| AT-SPI backend | yes | n/a | **no** (no desktop session here) | – | – |
| DroidCommand / Keyboard Android shells | yes | see handoff | – | – | – |
| `victor-pc detect` / `victor-pc read --enable [--speak]` | yes | n/a | CLI gating tested; AT-SPI read **not run** | – | – |
| scrcpy tile placement (`scrcpy_tile_argvs`): one window per phone via --serial, view-only unless granted | yes | n/a | argv tested only | – | – |
| Tk mic settings window `victormicgui.py` | written, py_compile only (no Tk here) | – | no | – | – |
| Web (Arch) card + `/api/mic` tested; Debian Tk tab and `victormicgui` window written, syntax-checked only | yes | n/a | API tested; Tk **not rendered** | – | – |
| Dispatcher `victordispatch` (voice and manual share one gate; fixed adb key argv per named serial) | yes | n/a | yes | – | – |
| Microphone… button in the Phone GUI | written, syntax-checked only | – | no (no Tk) | – | – |
| Push-to-talk pipeline `victorptt` (focus bound at press, echo guard, cancel on disconnect, 15 s cap) + `serve --ptt` | yes | n/a | yes with a fake recogniser; Vosk backend **not run** (no model) | – | – |
| (still missing:) single-window embedded tiles, PTT button/shortcut + speech model, rustdeskV phone-to-phone, read-aloud on the phone | **not implemented** | – | – | – | – |

See the handoff message for the shortest remaining checks.
