# Phone dashboard, microphone and voice (owner manual and status)

Open it from the menu (**VictorLinux Phone Dashboard**), from the Control Center **Fleet** section (Arch web: *Open phone
dashboard*; Debian Tk: *Phone dashboard (3 screens)*), or run `victor-phone-dash` as your normal user. It refuses to run as
root or without a graphical session. The single-phone `victor-phone-gui` and `victor-phone-headless` are unchanged;
`victor-phone-headless dash --dry-run [--json]` prints what the dashboard would start.

## What it does (and how)

* Up to **three phones**. Each tile is one `scrcpy` process started with `--serial=<exact adb serial>`, positioned over its
  tile (X11; on Wayland the windows cannot be placed and open separately). The tile frame shows name, transport
  (usb/wifi), state, and **view-only / CONTROL**; the focused tile has a cyan border.
* Tiles start **view-only** (`scrcpy --no-control`). **Control** asks you, then restarts that one phone's stream with input;
  **Release control** (or **Esc**, or the *Release input* button) restarts it view-only, so input stops immediately.
  Back/Home/Recents/scroll go through `adb -s <that serial> shell input ...` and only while that tile is controlled.
* USB and wireless appearances of one phone are merged only when `ro.serialno` + `ro.product.device` match; unreadable or
  placeholder values are never merged.
* A fourth phone asks **which visible tile to replace**; nothing is replaced silently. A dropped phone pauses only its own
  tile, clears focus if it had it, and comes back **view-only with a new session**.
* Quality: *balanced* (1024 px/30 fps), *low-load* (720 px/15 fps, for an 8 GB workstation), *sharp*.
* Wireless: **Pair wireless phone...** runs `adb pair` (code on stdin, never on a command line) then `adb connect`.
* If scrcpy/adb is missing or too old the dashboard shows the exact fix (Arch `pacman -S scrcpy android-tools android-udev`;
  Debian 13 `apt-get install -t trixie-backports scrcpy`, adb is in the image).

**Limit you should know:** mouse and keyboard go straight to the scrcpy window under the pointer, so there is no
dashboard-level coordinate mapping for those; letterbox/rotation mapping (`victordash.widget_to_device`) exists and is tested
but is used only for dashboard-originated gestures. To get the keyboard back from a scrcpy window use Alt+Tab or click the
dashboard; **Esc** works while the dashboard window has focus.

## PC microphone

Pick **Follow system default** or **Use selected mic**, and tick **Use this mic when connected**. Sources come from
`pactl` (PipeWire's pipewire-pulse or PulseAudio); playback monitors are never offered. Identity is stable across reboots
(USB vendor:product:serial, Bluetooth MAC, PCI path + port, so the headset jack and the internal mic differ). Plugging a mic
in while the dashboard is open updates the list (`pactl subscribe`, 2 s polling fallback). **Test mic** shows the level;
**Mute / unmute** uses `pactl set-source-mute`. If the mic disappears mid-command the utterance is cancelled and the
status says so; when the preferred mic returns it is reattached.

## Voice (offline push-to-talk)

Hold **Hold to talk** or **Ctrl+Space**. Needs the Python `vosk` package and a model folder in
`~/.local/share/victorlinux/stt/` (the dashboard prints the exact steps; nothing is downloaded automatically). Without it the
button is disabled and everything else works.

Phrases (closed grammar): "show all three phones", "show two phones", "focus my Nord", "go back on phone two", "home",
"recent apps", "scroll down [on phone three]", "disconnect phone two", "release input", "full screen", "read the selected PC
text", "stop reading". Named phones resolve by stable identity; duplicate names, low confidence, or a focus change during
speech make the dashboard **ask which phone**. Anything else ("run ...", "open a terminal") is *not a dashboard command* and
does nothing. Voice cannot run shell commands or send messages.

**Read selected PC text** speaks your current selection with `espeak-ng` (text via stdin). Recognition is refused while
speaking. An explicit window read (accessibility tree) drops password, hidden and sensitive fields; this path is implemented
and tested on a synthetic tree but has no AT-SPI adapter yet.

## Detect PC

Shows computer name, session type, displays (xrandr), audio inputs with status, and what a phone could do to this PC under
RustDesk: X11 = view+control; Wayland = view only unless `/dev/uinput` is writable (reported accurately).

## Messages from this PC to a phone

```sh
printf 'See you at 6' | victor-fleet message draft --device dev-... --kind sms --to +15551234567 --body-file -
victor-fleet message status dr-...      # queued | draft_received | declined | submitted | failed | unknown
victor-fleet capabilities               # messaging / screen_view / manual_control / terminal, each with a fix
```
The phone shows recipients, sending identity and contents; only the owner's **Send** submits. A draft is never retried;
bodies are removed from the coordinator store when it ends and never logged.

## Status: implemented / built / tested / verified on hardware

| Item | Implemented | Run here | Hardware |
|---|---|---|---|
| Dashboard model, identity merge, tiles, grants, 4th-phone choice | yes | 73+8 unit/integration tests with fake adb/scrcpy processes | no phone |
| Tk window | yes | rendered under Xvfb with 3 fake streams (screenshot reviewed); GUI interactions not scripted | no |
| scrcpy video inside tiles, rotation, taps to one phone | yes (argv, pinning) | argv/serial only | **not run** |
| Mic selection / hot-plug logic | yes | fixture-driven tests | **no physical mic or headset jack** |
| Voice grammar, target resolution, PTT lifecycle | yes | tests with fake capture/recognizer | **no vosk model, no real speech** |
| Detect PC, read selection | yes | fakes | not on a real desktop |
| message.draft coordinator side + fixtures | yes | 6 tests + fixture sync | phone side: see VictorSMS docs/MAIL.md |
| Phone -> PC and phone -> phone viewing (rustdeskV) | **no** | - | - |
| 3-stream CPU/memory/fps measurement | **no** | - | - |
