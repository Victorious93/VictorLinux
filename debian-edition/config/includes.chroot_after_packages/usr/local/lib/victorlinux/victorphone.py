#!/usr/bin/env python3
"""Shared helpers for VictorLinux Phone (Tk GUI) and victor-phone-headless.

Mirror and control a USB-connected Android phone with scrcpy (Genymobile/scrcpy,
Apache-2.0), or use adb alone for device listing, wait, and screenshots.

Nothing is downloaded at runtime: not scrcpy, not the scrcpy server, and not
any other file. English-only UI. Always-dark brand palette: background #0B0F1A,
primary accent cyan #00E5FF, secondary accent magenta #FF2BD6.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_TITLE = "VictorLinux Phone"
LIBDIR = os.path.dirname(os.path.realpath(__file__))

# Same palette as the Debian Tk Control Center and the Arch web Control Center.
# Every pair is chosen for WCAG AA on this dark background. No light/white theme.
PALETTE = {
    "bg": "#0B0F1A",
    "panel": "#161E31",
    "card": "#111727",
    "fg": "#E8EEF9",
    "muted": "#9BA8C3",
    "accent": "#00E5FF",
    "on_accent": "#05121A",
    "accent2": "#FF2BD6",
    "entry": "#0B0F1A",
    "sel": "#1D2740",
    "border": "#33406A",
    "ok": "#3EE6A8",
    "warn": "#FFC857",
    "bad": "#FF6B88",
}

AUDIO_SOURCES = ("output", "mic")


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    extras: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def model(self) -> str:
        for part in self.extras.split():
            if part.startswith("model:"):
                return part.split(":", 1)[1].replace("_", " ")
        return ""

    def label(self) -> str:
        name = self.model or self.serial
        if self.model:
            name = f"{self.model} ({self.serial})"
        if self.ready:
            return f"{name}  ready"
        return f"{name}  {self.state}"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ready"] = self.ready
        d["model"] = self.model
        return d


def which_tools() -> dict[str, str | None]:
    return {"adb": shutil.which("adb"), "scrcpy": shutil.which("scrcpy")}


def missing_help(missing: str) -> str:
    return (
        f"victor-phone cannot start: {missing} is not installed.\n"
        "\n"
        "Phone mirroring is optional. The rest of VictorLinux does not depend on it.\n"
        "\n"
        "  Arch edition: install the scrcpy, android-tools and android-udev packages.\n"
        "  Debian edition: adb is part of the image. scrcpy is not in Debian 13\n"
        "  (trixie); the image build installs it only when the suite or\n"
        "  trixie-backports provides it, and skips it otherwise. You can install\n"
        "  it yourself later with:\n"
        "    apt-get install -y --no-install-recommends scrcpy\n"
        "  or, if your administrator has enabled backports:\n"
        "    apt-get install -y --no-install-recommends -t trixie-backports scrcpy\n"
        "\n"
        "Nothing is downloaded automatically. Enable USB debugging on the phone,\n"
        "authorize this PC, and see docs/phone-mirroring.md for udev and group notes.\n"
    )


def parse_adb_devices(text: str) -> list[Device]:
    """Parse `adb devices` / `adb devices -l` output. Header and daemon lines are ignored."""
    devices: list[Device] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        # "serial<TAB>state [extras]" — also accept runs of spaces from odd adb builds.
        if "\t" in line:
            serial, rest = line.split("\t", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            serial, rest = parts
        # adb uses a two-word state for "no permissions".
        if rest == "no permissions" or rest.startswith("no permissions "):
            state = "no permissions"
            extras = rest[len("no permissions"):].strip()
        else:
            rest_parts = rest.split(None, 1)
            state = rest_parts[0]
            extras = rest_parts[1] if len(rest_parts) > 1 else ""
        if not serial or not state:
            continue
        devices.append(Device(serial=serial, state=state, extras=extras))
    return devices


def device_hint(devices: list[Device]) -> str | None:
    """Human message when no authorized phone is ready. None if at least one is ready."""
    if any(d.ready for d in devices):
        return None
    states = {d.state for d in devices}
    if "no permissions" in states:
        return (
            "adb cannot access the phone (no permissions). Add this user to the "
            "adbusers or plugdev group, install the Android udev rules, then unplug "
            "and replug the phone. See docs/phone-mirroring.md."
        )
    if "unauthorized" in states:
        return (
            "The phone is connected but not authorized. Unlock it and accept the "
            "USB debugging prompt, then try again."
        )
    return (
        "No authorized phone was found. Connect it with a USB cable, enable USB "
        "debugging, and authorize this PC. See docs/phone-mirroring.md."
    )


def scrcpy_argv(
    *,
    window_title: str = DEFAULT_TITLE,
    no_audio: bool = False,
    audio_source: str = "",
    record_file: str = "",
    serial: str = "",
    stay_awake: bool = False,
    turn_screen_off: bool = False,
    no_window: bool = False,
    extra: list[str] | None = None,
) -> list[str]:
    if no_audio and audio_source:
        raise ValueError("use either --no-audio or --audio-source, not both")
    if audio_source and audio_source not in AUDIO_SOURCES:
        raise ValueError(f"audio source must be one of: {', '.join(AUDIO_SOURCES)}")
    cmd = ["scrcpy", f"--window-title={window_title}"]
    if no_audio:
        cmd.append("--no-audio")
    if audio_source:
        cmd.append(f"--audio-source={audio_source}")
    if record_file:
        cmd.append(f"--record={record_file}")
    if serial:
        cmd.append(f"--serial={serial}")
    if stay_awake:
        cmd.append("--stay-awake")
    if turn_screen_off:
        cmd.append("--turn-screen-off")
    if no_window:
        cmd.append("--no-window")
    if extra:
        cmd.extend(extra)
    return cmd


def adb_argv(*args: str, serial: str = "") -> list[str]:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return cmd


def run_cmd(argv: list[str], *, timeout: float | None = 30, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=check)


def list_devices() -> tuple[list[Device], str]:
    """Return (devices, raw_output). Raises FileNotFoundError if adb is missing."""
    if not shutil.which("adb"):
        raise FileNotFoundError("adb")
    proc = run_cmd(adb_argv("devices", "-l"), timeout=20)
    text = (proc.stdout or "") + (proc.stderr or "")
    return parse_adb_devices(proc.stdout or ""), text


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------------------
# Headless CLI
# ---------------------------------------------------------------------------

HEADLESS_USAGE = f"""\
victor-phone-headless — adb and optional scrcpy for headless (and GUI) builds

List, wait for, and screenshot a USB-connected Android phone with adb.
Record or mirror the screen with scrcpy when that package is installed.
Nothing is downloaded. Audio is left on scrcpy's default unless you pass
--no-audio or --audio-source.

Usage: victor-phone-headless [options] <command> [command options]

  status                 Show whether adb/scrcpy are installed, and list phones
  devices                List phones (adb devices -l)
  wait                   Wait until a phone is connected (adb wait-for-device)
  screencap FILE         Save a PNG screenshot with adb (no scrcpy needed)
  record FILE            Record with scrcpy, no window (needs scrcpy)
  mirror                 Start scrcpy (needs a display unless you pass --no-window)
  screens                Up to three phones side by side, one scrcpy window each
                         (--serials A,B,C [--profile normal|low] [--control SERIAL])

Global options:
  -h, --help             Show this help
      --serial SERIAL    Select a device when more than one is connected
      --json             Machine-readable output for status and devices

record / mirror options:
      --dry-run          Print the scrcpy arguments (one per line) and exit
      --no-audio         Disable audio forwarding
      --audio-source S   Pass --audio-source=S to scrcpy (output or mic)
      --window-title T   Window title (default: {DEFAULT_TITLE}; mirror only)
      --no-window        Do not open a scrcpy window (mirror; implied by record)
      --stay-awake       Keep the phone awake
      --turn-screen-off  Turn the phone screen off while mirroring
      -- extra scrcpy arguments

If scrcpy or adb is not installed, this command explains what is missing
and exits. The rest of VictorLinux keeps working.

USB debugging must be enabled on the phone, and you must authorize this
PC the first time it is connected. If adb reports "no permissions" or
the device does not appear, the logged-in user needs the udev rules and
group membership from android-udev (Arch: android-udev, group adbusers)
or android-sdk-platform-tools-common (Debian: group plugdev, and
adbusers when that group exists). Unplug and replug the phone after
group changes, or log out and back in.

See docs/phone-mirroring.md.
"""


class HeadlessError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _die_missing(tool: str) -> int:
    sys.stderr.write(missing_help(tool))
    return 1


def _parse_headless(argv: list[str]) -> argparse.Namespace:
    # Split extra scrcpy args ourselves so argparse does not swallow "--".
    if "--" in argv:
        idx = argv.index("--")
        argv, extra = argv[:idx], argv[idx + 1 :]
    else:
        extra = []
    parser = argparse.ArgumentParser(prog="victor-phone-headless", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--serial", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--audio-source", default="")
    parser.add_argument("--window-title", default=DEFAULT_TITLE)
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--stay-awake", action="store_true")
    parser.add_argument("--turn-screen-off", action="store_true")
    parser.add_argument("--record", default="")
    parser.add_argument("--serials", default="")
    parser.add_argument("--profile", default="normal")
    parser.add_argument("--control", default="")
    parser.add_argument("--width", type=int, default=1800)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("command", nargs="?", default="")
    parser.add_argument("target", nargs="?", default="")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        raise HeadlessError(
            f"victor-phone-headless: unexpected argument: {unknown[0]} (try --help)",
            2,
        )
    args.extra = extra
    return args


def _scrcpy_from_cli(args: argparse.Namespace, *, record_file: str = "", no_window: bool = False) -> list[str]:
    try:
        return scrcpy_argv(
            window_title=args.window_title,
            no_audio=args.no_audio,
            audio_source=args.audio_source,
            record_file=record_file or args.record,
            serial=args.serial,
            stay_awake=args.stay_awake,
            turn_screen_off=args.turn_screen_off,
            no_window=no_window or args.no_window,
            extra=args.extra,
        )
    except ValueError as exc:
        raise HeadlessError(f"victor-phone-headless: {exc}", 2) from exc


def _print_devices(devices: list[Device], as_json: bool) -> None:
    if as_json:
        print(json.dumps([d.as_dict() for d in devices], indent=2))
        return
    if not devices:
        print("No phones listed by adb.")
        return
    for d in devices:
        print(d.label())


def cmd_status(args: argparse.Namespace) -> int:
    tools = which_tools()
    devices: list[Device] = []
    hint = None
    if tools["adb"]:
        devices, _raw = list_devices()
        hint = device_hint(devices)
    payload = {
        "adb": tools["adb"],
        "scrcpy": tools["scrcpy"],
        "display": has_display(),
        "devices": [d.as_dict() for d in devices],
        "hint": hint,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"adb:    {tools['adb'] or 'not installed'}")
        print(f"scrcpy: {tools['scrcpy'] or 'not installed'}")
        print(f"display: {'yes' if payload['display'] else 'no'}")
        if devices:
            print("phones:")
            for d in devices:
                print(f"  {d.label()}")
        else:
            print("phones: none")
        if hint:
            print(hint)
    if not tools["adb"]:
        return _die_missing("adb")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    if not shutil.which("adb"):
        return _die_missing("adb")
    devices, _raw = list_devices()
    _print_devices(devices, args.json)
    if not args.json:
        hint = device_hint(devices)
        if hint:
            print(hint, file=sys.stderr)
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    if not shutil.which("adb"):
        return _die_missing("adb")
    proc = run_cmd(adb_argv("wait-for-device", serial=args.serial), timeout=None)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "adb wait-for-device failed\n")
        return proc.returncode or 1
    devices, _raw = list_devices()
    if args.serial:
        devices = [d for d in devices if d.serial == args.serial]
    hint = device_hint(devices)
    if hint and not any(d.ready for d in devices):
        # Connected but maybe unauthorized — still report it.
        print(hint, file=sys.stderr)
    else:
        print("Phone connected.")
    return 0


def cmd_screencap(args: argparse.Namespace) -> int:
    if not args.target:
        raise HeadlessError("victor-phone-headless: screencap needs a file path", 2)
    if not shutil.which("adb"):
        return _die_missing("adb")
    # Binary mode: a PNG is not valid UTF-8, so do not use text=True.
    raw = subprocess.run(
        adb_argv("exec-out", "screencap", "-p", serial=args.serial),
        capture_output=True,
        timeout=60,
    )
    if raw.returncode != 0:
        err = raw.stderr.decode("utf-8", "replace") if raw.stderr else "adb screencap failed\n"
        sys.stderr.write(err)
        return raw.returncode or 1
    path = os.path.expanduser(args.target)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(raw.stdout)
    print(path)
    return 0


def cmd_scrcpy(args: argparse.Namespace, *, record_file: str, no_window: bool) -> int:
    if record_file == "" and args.command == "record":
        raise HeadlessError("victor-phone-headless: record needs a file path", 2)
    try:
        cmd = _scrcpy_from_cli(args, record_file=record_file, no_window=no_window)
    except HeadlessError:
        raise
    if args.dry_run:
        print("\n".join(cmd))
        return 0
    if not shutil.which("scrcpy"):
        return _die_missing("scrcpy")
    if not shutil.which("adb"):
        return _die_missing("adb")
    os.execvp(cmd[0], cmd)
    return 1


def cmd_screens(args: argparse.Namespace) -> int:
    """Headless/CLI counterpart of the dashboard: separate tiled scrcpy windows, view-only except --control."""
    import victorphonedash as pd
    import victorscreens as vs
    serials = [x for x in (args.serials or args.serial).split(",") if x]
    if not serials:
        raise HeadlessError("victor-phone-headless: screens needs --serials A[,B[,C]]", 2)
    if len(serials) > vs.MAX_TILES:
        raise HeadlessError("victor-phone-headless: at most three phones; choose which to show", 2)
    if args.profile not in pd.PROFILES:
        raise HeadlessError(f"victor-phone-headless: profile must be one of {', '.join(pd.PROFILES)}", 2)
    mgr = vs.TileManager()
    for s in serials:
        t = mgr.add(s, s)
        t.state = "live"
        if s == args.control:
            t.permissions = "control"
    if args.control:
        mgr.grant_input(args.control)
    layout = mgr.layout(args.width, args.height)
    cmds = []
    for t in mgr.tiles:
        x, y, w, h = layout[t.device_id]
        place = [f"--window-x={x}", f"--window-y={y}", f"--window-width={w}", f"--window-height={h}"]
        cmds.append(pd.tile_argv(t, mgr.input_held == t.device_id, args.profile, place))
    if args.dry_run:
        for c in cmds:
            print(" ".join(c))
        return 0
    if not shutil.which("scrcpy"):
        return _die_missing("scrcpy")
    procs = [subprocess.Popen(c) for c in cmds]
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    return 0


def main_headless(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parse_headless(argv)
    except HeadlessError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    if args.help or args.command in ("", "help"):
        print(HEADLESS_USAGE, end="" if HEADLESS_USAGE.endswith("\n") else "\n")
        return 0
    command = args.command
    try:
        if command == "status":
            if args.target:
                raise HeadlessError("victor-phone-headless: status takes no file argument", 2)
            return cmd_status(args)
        if command == "devices":
            return cmd_devices(args)
        if command == "wait":
            return cmd_wait(args)
        if command == "screencap":
            return cmd_screencap(args)
        if command == "record":
            return cmd_scrcpy(args, record_file=args.target, no_window=True)
        if command == "mirror":
            return cmd_scrcpy(args, record_file=args.record or args.target, no_window=args.no_window)
        if command == "screens":
            return cmd_screens(args)
        raise HeadlessError(f"victor-phone-headless: unknown command: {command} (try --help)", 2)
    except HeadlessError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    except FileNotFoundError as exc:
        return _die_missing(str(exc) or "adb")
    except subprocess.TimeoutExpired:
        print("victor-phone-headless: command timed out", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Tk GUI (imported lazily so the headless CLI does not need tkinter)
# ---------------------------------------------------------------------------

def apply_brand_theme(root: Any) -> None:
    """Apply the always-dark VictorLinux palette to a Tk root. No white backgrounds."""
    import tkinter as tk
    from tkinter import ttk

    P = PALETTE
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        ".",
        background=P["bg"],
        foreground=P["fg"],
        fieldbackground=P["entry"],
        bordercolor=P["panel"],
        lightcolor=P["panel"],
        darkcolor=P["panel"],
    )
    style.configure("TFrame", background=P["bg"])
    style.configure("TLabel", background=P["bg"], foreground=P["fg"])
    style.configure("TLabelframe", background=P["bg"], foreground=P["fg"])
    style.configure("TLabelframe.Label", background=P["bg"], foreground=P["accent"])
    style.configure(
        "TButton",
        background=P["panel"],
        foreground=P["fg"],
        padding=(14, 9),
        bordercolor=P["border"],
        focuscolor=P["accent"],
    )
    style.map(
        "TButton",
        background=[("disabled", P["card"]), ("active", P["sel"])],
        foreground=[("disabled", P["muted"])],
        bordercolor=[("focus", P["accent"]), ("active", P["accent"])],
    )
    style.configure(
        "Accent.TButton",
        background=P["accent"],
        foreground=P["on_accent"],
        bordercolor=P["accent"],
        font=("Sans", 10, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", P["sel"]), ("active", "#5CF0FF")],
        foreground=[("disabled", P["muted"])],
    )
    style.configure(
        "Danger.TButton",
        background="#2A1222",
        foreground="#FF9DB1",
        bordercolor="#6E2C46",
    )
    style.map("Danger.TButton", background=[("active", "#3A1830")])
    style.configure("Card.TFrame", background=P["card"], bordercolor=P["border"], relief="solid", borderwidth=1)
    style.configure("Brand.TLabel", background=P["bg"], foreground=P["fg"], font=("Sans", 22, "bold"))
    style.configure("Eyebrow.TLabel", background=P["bg"], foreground=P["accent2"], font=("Sans", 9, "bold"))
    style.configure("Muted.TLabel", background=P["bg"], foreground=P["muted"])
    style.configure("Status.TLabel", background=P["card"], foreground=P["muted"], padding=(10, 4))
    style.configure("StatusOk.TLabel", background=P["card"], foreground=P["ok"], padding=(10, 4))
    style.configure("StatusWarn.TLabel", background=P["card"], foreground=P["warn"], padding=(10, 4))
    style.configure("StatusBad.TLabel", background=P["card"], foreground=P["bad"], padding=(10, 4))
    style.configure("TCheckbutton", background=P["bg"], foreground=P["fg"])
    style.map("TCheckbutton", background=[("active", P["bg"])], foreground=[("active", P["fg"])])
    style.configure(
        "TEntry",
        fieldbackground=P["entry"],
        foreground=P["fg"],
        insertcolor=P["accent"],
        bordercolor=P["border"],
    )
    style.map("TEntry", bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])])
    style.configure("TCombobox", fieldbackground=P["entry"], foreground=P["fg"], arrowcolor=P["accent"])
    style.map("TCombobox", fieldbackground=[("readonly", P["entry"])], foreground=[("readonly", P["fg"])])
    style.configure("TScrollbar", background=P["panel"], troughcolor=P["bg"], arrowcolor=P["fg"])
    root.configure(bg=P["bg"])
    root.option_add("*Listbox.background", P["entry"])
    root.option_add("*Listbox.foreground", P["fg"])
    root.option_add("*Listbox.selectBackground", P["accent"])
    root.option_add("*Listbox.selectForeground", P["on_accent"])
    root.option_add("*Text.background", P["entry"])
    root.option_add("*Text.foreground", P["fg"])
    root.option_add("*highlightColor", P["accent"])
    root.option_add("*highlightBackground", P["bg"])
    root.option_add("*background", P["bg"])
    root.option_add("*foreground", P["fg"])
    root.option_add("*selectBackground", P["accent"])
    root.option_add("*selectForeground", P["on_accent"])
    root.option_add("*insertBackground", P["accent"])
    root.option_add("*activeBackground", P["sel"])
    root.option_add("*activeForeground", P["fg"])
    root.option_add("*Dialog.background", P["bg"])
    root.option_add("*Dialog.foreground", P["fg"])


def build_gui_argv(
    *,
    serial: str = "",
    audio: bool = True,
    audio_source: str = "",
    record_file: str = "",
    stay_awake: bool = False,
    turn_screen_off: bool = False,
) -> list[str]:
    source = audio_source if audio and audio_source in AUDIO_SOURCES else ""
    return scrcpy_argv(
        no_audio=not audio,
        audio_source=source,
        record_file=record_file,
        serial=serial,
        stay_awake=stay_awake,
        turn_screen_off=turn_screen_off,
    )


def main_gui(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--screens" in argv:
        import victorphonedashgui
        return victorphonedashgui.main(argv)
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except ImportError:
        print(
            "VictorLinux Phone cannot start: Tk is not installed.\n"
            "\n"
            "  Arch edition (GUI profile): install the tk package.\n"
            "  Debian edition: install python3-tk.\n"
            "\n"
            "The headless tool still works: victor-phone-headless --help\n"
            "Nothing is downloaded automatically.\n",
            file=sys.stderr,
        )
        return 1

    class PhoneApp(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(DEFAULT_TITLE)
            self.geometry("540x700")
            self.minsize(480, 620)
            apply_brand_theme(self)
            self.proc: subprocess.Popen[str] | None = None
            self.devices: list[Device] = []
            self._build()
            self.after(100, self.refresh_devices)
            self.protocol("WM_DELETE_WINDOW", self.on_close)

        def open_mic(self) -> None:
            # Shortcut to Settings -> Audio -> Microphone (same settings as `victor-mic`). Unverified: no display here.
            try:
                import victormicgui
                victormicgui.open_window(self)
            except Exception as exc:  # never break the mirroring window over a settings dialog
                self.set_status(f"Microphone settings unavailable: {exc}") if hasattr(self, "set_status") else None

        def open_screens(self) -> None:
            # One window with up to three live phones and push-to-talk (victorphonedashgui).
            try:
                import victorphonedashgui
                victorphonedashgui.open_dashboard(self)
            except Exception as exc:
                self.set_status(f"Screens unavailable: {exc}") if hasattr(self, "set_status") else None

        def _build(self) -> None:
            header = ttk.Frame(self, padding=(22, 16, 22, 6))
            header.pack(fill="x")
            ttk.Label(header, text="VICTORLINUX  ·  PHONE", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(header, text="VictorLinux Phone", style="Brand.TLabel").pack(anchor="w")
            ttk.Label(
                header,
                text="Mirror and control a USB-connected Android phone.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(4, 0))
            tk.Frame(self, height=2, bg=PALETTE["accent"]).pack(fill="x", padx=22)

            body = ttk.Frame(self, padding=(22, 16, 22, 8))
            body.pack(fill="both", expand=True)

            row = ttk.Frame(body)
            row.pack(fill="x")
            ttk.Label(row, text="Devices").pack(side="left")
            ttk.Button(row, text="Refresh", command=self.refresh_devices).pack(side="right")
            ttk.Button(row, text="Microphone…", command=self.open_mic).pack(side="right", padx=(0, 6))
            ttk.Button(row, text="Screens & talk…", command=self.open_screens).pack(side="right", padx=(0, 6))

            self.listbox = tk.Listbox(body, height=6, exportselection=False, activestyle="none")
            self.listbox.pack(fill="x", pady=(6, 12))
            self.listbox.configure(
                bg=PALETTE["entry"],
                fg=PALETTE["fg"],
                selectbackground=PALETTE["accent"],
                selectforeground=PALETTE["on_accent"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                highlightcolor=PALETTE["accent"],
                relief="flat",
            )

            opts = ttk.Labelframe(body, text="Options", padding=(12, 8))
            opts.pack(fill="x")

            self.audio_var = tk.BooleanVar(value=True)
            self.source_var = tk.StringVar(value="default")
            self.record_var = tk.BooleanVar(value=False)
            self.record_path = tk.StringVar(value="")
            self.awake_var = tk.BooleanVar(value=False)
            self.screen_off_var = tk.BooleanVar(value=False)

            ttk.Checkbutton(opts, text="Audio", variable=self.audio_var, command=self._toggle_audio).grid(
                row=0, column=0, sticky="w", pady=2
            )
            ttk.Label(opts, text="Audio source").grid(row=0, column=1, sticky="e", padx=(16, 6))
            self.source_box = ttk.Combobox(
                opts,
                textvariable=self.source_var,
                values=("default", "output", "mic"),
                state="readonly",
                width=12,
            )
            self.source_box.grid(row=0, column=2, sticky="w")

            ttk.Checkbutton(opts, text="Record to file", variable=self.record_var, command=self._toggle_record).grid(
                row=1, column=0, sticky="w", pady=2
            )
            self.record_entry = ttk.Entry(opts, textvariable=self.record_path, width=28)
            self.record_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(16, 6), pady=2)
            self.browse_btn = ttk.Button(opts, text="Browse", command=self.browse_record)
            self.browse_btn.grid(row=1, column=3, sticky="w")
            ttk.Checkbutton(opts, text="Stay awake", variable=self.awake_var).grid(row=2, column=0, sticky="w", pady=2)
            ttk.Checkbutton(opts, text="Turn screen off", variable=self.screen_off_var).grid(
                row=2, column=1, columnspan=2, sticky="w", pady=2
            )
            opts.columnconfigure(1, weight=1)
            self._toggle_record()

            actions = ttk.Frame(body)
            actions.pack(fill="x", pady=(16, 8))
            self.start_btn = ttk.Button(actions, text="Start mirroring", style="Accent.TButton", command=self.start)
            self.start_btn.pack(side="left")
            self.stop_btn = ttk.Button(actions, text="Stop", style="Danger.TButton", command=self.stop, state="disabled")
            self.stop_btn.pack(side="left", padx=(8, 0))

            ttk.Label(body, text="Log", style="Muted.TLabel").pack(anchor="w")
            self.log = tk.Text(body, height=8, wrap="word", relief="flat", borderwidth=0)
            self.log.pack(fill="both", expand=True, pady=(4, 0))
            self.log.configure(
                bg=PALETTE["entry"],
                fg=PALETTE["fg"],
                insertbackground=PALETTE["accent"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                highlightcolor=PALETTE["accent"],
            )
            self.log.insert("end", "Connect a phone with USB debugging enabled, then press Refresh.\n")
            self.log.configure(state="disabled")

            self.status_var = tk.StringVar(value="Starting…")
            self.status = ttk.Label(self, textvariable=self.status_var, anchor="w", style="Status.TLabel")
            self.status.pack(fill="x", side="bottom")

        def _toggle_audio(self) -> None:
            self.source_box.configure(state="readonly" if self.audio_var.get() else "disabled")

        def _toggle_record(self) -> None:
            state = "normal" if self.record_var.get() else "disabled"
            self.record_entry.configure(state=state)
            self.browse_btn.configure(state=state)

        def browse_record(self) -> None:
            path = filedialog.asksaveasfilename(
                parent=self,
                title="Record session",
                defaultextension=".mp4",
                filetypes=[("MP4 video", "*.mp4"), ("MKV video", "*.mkv"), ("All files", "*")],
            )
            if path:
                self.record_path.set(path)
                self.record_var.set(True)
                self._toggle_record()

        def append_log(self, text: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def set_status(self, text: str, kind: str = "") -> None:
            style = {"ok": "StatusOk.TLabel", "warn": "StatusWarn.TLabel", "bad": "StatusBad.TLabel"}.get(
                kind, "Status.TLabel"
            )
            self.status.configure(style=style)
            self.status_var.set(text)

        def selected_serial(self) -> str:
            try:
                idx = self.listbox.curselection()[0]
            except IndexError:
                return self.devices[0].serial if len(self.devices) == 1 and self.devices[0].ready else ""
            if 0 <= idx < len(self.devices):
                return self.devices[idx].serial
            return ""

        def refresh_devices(self) -> None:
            tools = which_tools()
            self.listbox.delete(0, "end")
            self.devices = []
            if not tools["adb"]:
                self.set_status("adb is not installed", "bad")
                self.append_log(missing_help("adb"))
                self.start_btn.configure(state="disabled")
                return
            if not tools["scrcpy"]:
                self.set_status("scrcpy is not installed — mirroring unavailable", "warn")
                self.append_log(missing_help("scrcpy"))
            try:
                devices, _raw = list_devices()
            except Exception as exc:  # noqa: BLE001 — show any adb failure in the UI
                self.set_status(f"adb failed: {exc}", "bad")
                return
            self.devices = devices
            if not devices:
                self.listbox.insert("end", "No phone listed")
            for d in devices:
                self.listbox.insert("end", d.label())
            ready = [d for d in devices if d.ready]
            if ready:
                self.listbox.selection_set(devices.index(ready[0]))
                kind = "ok" if tools["scrcpy"] else "warn"
                self.set_status(f"{len(ready)} phone ready" if len(ready) == 1 else f"{len(ready)} phones ready", kind)
                self.start_btn.configure(state="normal" if tools["scrcpy"] and self.proc is None else "disabled")
            else:
                hint = device_hint(devices) or "No authorized phone was found."
                self.set_status(hint, "warn")
                self.start_btn.configure(state="disabled")

        def start(self) -> None:
            if self.proc is not None:
                return
            tools = which_tools()
            if not tools["scrcpy"]:
                self.set_status("scrcpy is not installed", "bad")
                self.append_log(missing_help("scrcpy"))
                return
            if not tools["adb"]:
                self.set_status("adb is not installed", "bad")
                self.append_log(missing_help("adb"))
                return
            serial = self.selected_serial()
            if not serial:
                self.set_status("Select a ready phone first.", "warn")
                return
            record = self.record_path.get().strip() if self.record_var.get() else ""
            if self.record_var.get() and not record:
                self.set_status("Choose a record file, or turn recording off.", "warn")
                return
            source = self.source_var.get()
            try:
                cmd = build_gui_argv(
                    serial=serial,
                    audio=self.audio_var.get(),
                    audio_source="" if source == "default" else source,
                    record_file=record,
                    stay_awake=self.awake_var.get(),
                    turn_screen_off=self.screen_off_var.get(),
                )
            except ValueError as exc:
                self.set_status(str(exc), "bad")
                return
            self.append_log("Starting: " + " ".join(cmd))
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except FileNotFoundError:
                self.proc = None
                self.set_status("scrcpy is not installed", "bad")
                self.append_log(missing_help("scrcpy"))
                return
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.set_status("Mirroring…", "ok")
            threading.Thread(target=self._watch, daemon=True).start()

        def _watch(self) -> None:
            proc = self.proc
            if proc is None or proc.stdout is None:
                return
            for line in proc.stdout:
                text = line.rstrip()
                if text:
                    self.after(0, self.append_log, text)
            proc.wait()
            self.after(0, self._ended, proc.returncode)

        def _ended(self, code: int | None) -> None:
            self.proc = None
            self.stop_btn.configure(state="disabled")
            tools = which_tools()
            self.start_btn.configure(state="normal" if tools["scrcpy"] else "disabled")
            if code:
                self.set_status(f"scrcpy exited with status {code}", "warn")
            else:
                self.set_status("Mirroring stopped.", "ok")
            self.refresh_devices()

        def stop(self) -> None:
            proc = self.proc
            if proc is None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

        def on_close(self) -> None:
            self.stop()
            self.destroy()

    app = PhoneApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    prog = os.path.basename(sys.argv[0])
    if "gui" in prog:
        raise SystemExit(main_gui())
    raise SystemExit(main_headless())
