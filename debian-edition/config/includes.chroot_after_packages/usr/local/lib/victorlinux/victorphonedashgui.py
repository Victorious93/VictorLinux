"""Tk window for the phone dashboard: up to three live Android screens plus push-to-talk. Work by Victorious.

All decisions live in victorphonedash / victorscreens / victordispatch; this file only draws and forwards events.
Shortcuts (when the dashboard has keyboard focus):
  F8 (hold)          push-to-talk on the selected microphone, bound to the focused phone at press
  F12 or Ctrl+Esc    release input (every tile goes view-only)
  Ctrl+1 / 2 / 3     focus tile 1 / 2 / 3
Screen text read aloud and recognised speech are shown transiently and never written to the log pane."""
from __future__ import annotations

import subprocess
import sys
from typing import Any

import victordispatch as vd
import victorphonedash as pd
import victorscreens as vs

BG, PANEL, FG, MUTED, ACCENT, PINK, WARN = "#0B0F1A", "#121a2b", "#E6F1FF", "#7d8aa6", "#00E5FF", "#FF2BD6", "#ffb020"
STATE_COLOURS = {"live": ACCENT, "connecting": WARN, "reconnecting": WARN, "disconnected": PINK}


def _adb_phones() -> list[tuple[str, str]]:
    try:
        from victorphone import list_devices
        devs, _ = list_devices()
    except Exception:
        return []
    return [(d.serial, d.model or d.serial) for d in devs if d.ready]


def _mic_plan() -> pd.CapturePlan:
    import victormic as vm
    s = vm.load_settings()
    pc, default = vm.list_pc_sources()
    reg = vm.read_status_registry()
    return pd.capture_plan(s, pc, default, reg, pd.virtual_source_present())


def _read_selected() -> str:
    try:
        import pcread
        r = pcread.read_selection(pcread.AtspiBackend(), "local", True)
        return r.text if r.ok else f"Nothing read ({r.reason})"
    except Exception as e:
        return f"Reading is not available ({e.__class__.__name__})"


def open_dashboard(master: Any = None, profile: str = "normal") -> Any:
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(master) if master is not None else tk.Tk()
    win.title("VictorLinux Phones")
    win.configure(bg=BG)
    win.geometry("1180x760")
    win.minsize(640, 420)

    mgr = vs.TileManager()
    embed_ok, embed_note = pd.embedding_supported()
    frames: dict[str, dict[str, Any]] = {}
    embedded: dict[str, str] = {}           # device -> X window id
    state: dict[str, Any] = {"ptt_release_job": None, "closing": False}

    def placement(t: vs.Tile) -> list[str]:
        if embed_ok:
            return []
        x, y, w, h = mgr.layout(max(win.winfo_width(), 1), max(win.winfo_height() - 110, 1)).get(t.device_id, (0, 0, 400, 800))
        ox, oy = win.winfo_rootx(), win.winfo_rooty() + 110
        return [f"--window-x={ox + x}", f"--window-y={oy + y}", f"--window-width={w}", f"--window-height={h}"]

    runner = pd.TileRunner(mgr, profile=profile, placement_fn=placement)
    ready, stt_msg = pd.recognizer_status()
    rec = None
    if ready:
        try:
            import victorptt as vp
            rec = vp.make_recognizer("vosk")
        except Exception as e:          # model present but unusable: say so, keep manual control
            ready, stt_msg = False, f"Speech model could not load ({e.__class__.__name__}).\n" + pd.STT_INSTALL_HELP
    ptt = pd.PttController(mgr, rec, _mic_plan, read_fn=_read_selected)

    # ---------------------------------------------------------------- toolbar
    bar = tk.Frame(win, bg=BG)
    bar.pack(fill="x", padx=12, pady=(10, 4))
    tk.Label(bar, text="PHONES", fg=ACCENT, bg=BG, font=("TkDefaultFont", 11, "bold")).pack(side="left")
    phone_var = tk.StringVar()
    phone_box = ttk.Combobox(bar, textvariable=phone_var, width=34, state="readonly")
    phone_box.pack(side="left", padx=8)

    def refresh_phones() -> None:
        phones = _adb_phones()
        phone_box["values"] = [f"{n}  ({s})" for s, n in phones]
        phone_box._phones = phones  # type: ignore[attr-defined]
        if phones and not phone_var.get():
            phone_box.current(0)

    def add_selected() -> None:
        phones = getattr(phone_box, "_phones", [])
        i = phone_box.current()
        if i < 0 or i >= len(phones):
            status("Choose a phone first (USB debugging must be allowed on the phone).")
            return
        serial, nick = phones[i]
        try:
            pd.add_phone(mgr, serial, nick)
        except vs.FourthPhone as fp:
            choice = ask_replace(fp.tiles)
            if not choice:
                status("Kept the three phones that are showing.")
                return
            stop_tile(choice)
            pd.add_phone(mgr, serial, nick, replace=choice)
        build_tiles()

    tk.Button(bar, text="↻", command=refresh_phones, bg=PANEL, fg=FG, relief="flat").pack(side="left")
    tk.Button(bar, text="Add phone", command=add_selected, bg=ACCENT, fg=BG, relief="flat").pack(side="left", padx=6)
    prof_var = tk.StringVar(value=profile)

    def set_profile(*_: Any) -> None:
        runner.profile = prof_var.get()
        for dev in list(runner.procs):         # restart streams so the new limits apply
            runner._kill(dev)
        status(f"Load profile: {runner.profile}")

    ttk.Combobox(bar, textvariable=prof_var, values=list(pd.PROFILES), width=7, state="readonly").pack(side="left", padx=6)
    prof_var.trace_add("write", set_profile)
    ptt_btn = tk.Button(bar, text="🎤 Hold to talk (F8)", bg=PINK, fg=BG, relief="flat")
    ptt_btn.pack(side="right")
    tk.Button(bar, text="Release input (F12)", command=lambda: do_release(), bg=PANEL, fg=FG, relief="flat").pack(side="right", padx=6)

    status_var = tk.StringVar(value=embed_note)
    tk.Label(win, textvariable=status_var, fg=MUTED, bg=BG, anchor="w", justify="left").pack(fill="x", padx=12)
    spoken_var = tk.StringVar(value="" if ready else stt_msg)
    tk.Label(win, textvariable=spoken_var, fg=FG, bg=BG, anchor="w", justify="left", wraplength=1100).pack(fill="x", padx=12)

    area = tk.Frame(win, bg=BG)
    area.pack(fill="both", expand=True, padx=12, pady=8)

    def status(msg: str) -> None:
        status_var.set(msg)

    # ---------------------------------------------------------------- tiles
    def ask_replace(devs: list[str]) -> str:
        dlg = tk.Toplevel(win); dlg.configure(bg=BG); dlg.title("Replace a phone"); dlg.transient(win); dlg.grab_set()
        tk.Label(dlg, text="Three phones are showing. Which one should the new phone replace?", fg=FG, bg=BG).pack(padx=16, pady=10)
        pick = tk.StringVar(value="")
        for d in devs:
            t = mgr.get(d)
            tk.Radiobutton(dlg, text=f"{t.nickname} ({d})", variable=pick, value=d, fg=FG, bg=BG, selectcolor=PANEL,
                           activebackground=BG).pack(anchor="w", padx=20)
        row = tk.Frame(dlg, bg=BG); row.pack(pady=10)
        tk.Button(row, text="Replace", command=dlg.destroy, bg=ACCENT, fg=BG).pack(side="left", padx=4)
        tk.Button(row, text="Cancel", command=lambda: (pick.set(""), dlg.destroy()), bg=PANEL, fg=FG).pack(side="left", padx=4)
        win.wait_window(dlg)
        return pick.get()

    def stop_tile(dev: str) -> None:
        runner._kill(dev)
        embedded.pop(dev, None)
        f = frames.pop(dev, None)
        if f:
            f["outer"].destroy()

    def key(dev: str, action: str) -> None:
        out = vd.dispatch(vd.vv.Command(action, dev), mgr)      # same gate as a spoken command
        status(out.message if out.ok else out.message + " — give this tile input first.")

    def toggle_perm(dev: str) -> None:
        t = mgr.get(dev)
        t.permissions = "view" if t.permissions == "control" else "control"
        if t.permissions == "view" and mgr.input_held == dev:
            mgr.release_input()
        refresh_headers()

    def take_input(dev: str) -> None:
        try:
            mgr.grant_input(dev)
            status(f"Input goes to {mgr.get(dev).nickname} only.")
        except PermissionError as e:
            status(str(e) + " (allow control on the tile and wait until it is live)")

    def do_release() -> None:
        mgr.release_input()
        status("Input released: every tile is view-only.")

    def disconnect(dev: str) -> None:
        vd.dispatch(vd.vv.Command("disconnect", dev), mgr)
        runner.sync()
        embedded.pop(dev, None)
        refresh_headers()

    def reconnect(dev: str) -> None:
        mgr.set_state(dev, "connecting")
        runner.retry_at.pop(dev, None)
        embedded.pop(dev, None)

    def remove(dev: str) -> None:
        stop_tile(dev)
        mgr.remove(dev)
        relayout()

    def build_tiles() -> None:
        for t in mgr.tiles:
            if t.device_id in frames:
                continue
            dev = t.device_id
            outer = tk.Frame(area, bg=PANEL, highlightthickness=2, highlightbackground=PANEL)
            head = tk.Frame(outer, bg=PANEL); head.pack(fill="x")
            title = tk.Label(head, fg=FG, bg=PANEL, anchor="w", font=("TkDefaultFont", 10, "bold"))
            title.pack(side="left", padx=6, pady=3)
            info = tk.Label(head, fg=MUTED, bg=PANEL, anchor="w"); info.pack(side="left")
            btns = tk.Frame(outer, bg=PANEL); btns.pack(fill="x")
            perm_btn = tk.Button(btns, command=lambda d=dev: toggle_perm(d), bg=BG, fg=FG, relief="flat")
            for label, cmd in (("◀", lambda d=dev: key(d, "back")), ("●", lambda d=dev: key(d, "home")),
                               ("▢", lambda d=dev: key(d, "recents"))):
                tk.Button(btns, text=label, command=cmd, bg=BG, fg=ACCENT, relief="flat", width=2).pack(side="left", padx=1)
            perm_btn.pack(side="left", padx=4)
            tk.Button(btns, text="Take input", command=lambda d=dev: take_input(d), bg=BG, fg=FG, relief="flat").pack(side="left")
            tk.Button(btns, text="✕", command=lambda d=dev: remove(d), bg=BG, fg=PINK, relief="flat").pack(side="right")
            tk.Button(btns, text="Disconnect", command=lambda d=dev: disconnect(d), bg=BG, fg=PINK, relief="flat").pack(side="right")
            tk.Button(btns, text="Reconnect", command=lambda d=dev: reconnect(d), bg=BG, fg=FG, relief="flat").pack(side="right")
            body = tk.Frame(outer, bg="#000000", container=embed_ok)
            body.pack(fill="both", expand=True)
            for w in (outer, head, title, info):
                w.bind("<Button-1>", lambda _e, d=dev: (mgr.set_focus(d), relayout()))
            frames[dev] = {"outer": outer, "title": title, "info": info, "perm": perm_btn, "body": body}
        relayout()

    def refresh_headers() -> None:
        for i, t in enumerate(mgr.tiles):
            f = frames.get(t.device_id)
            if not f:
                continue
            focused = t.device_id == mgr.focus
            holder = mgr.input_held == t.device_id
            f["title"].configure(text=f"{i + 1}. {t.nickname}" + ("  ◆ focus" if focused else ""))
            f["info"].configure(text=f"  {t.state} · {t.permissions}{' · INPUT' if holder else ''}",
                                fg=STATE_COLOURS.get(t.state, MUTED))
            f["perm"].configure(text="View only" if t.permissions == "control" else "Allow control")
            f["outer"].configure(highlightbackground=PINK if holder else (ACCENT if focused else PANEL))

    def relayout(_e: Any = None) -> None:
        w, h = max(area.winfo_width(), 1), max(area.winfo_height(), 1)
        for dev, (x, y, tw, th) in mgr.layout(w, h).items():
            f = frames.get(dev)
            if f:
                f["outer"].place(x=x, y=y, width=tw, height=th)
        refresh_headers()
        win.after(50, resize_embedded)

    def resize_embedded() -> None:
        for dev, wid in list(embedded.items()):
            f = frames.get(dev)
            if f:
                b = f["body"]
                subprocess.run(["xdotool", "windowsize", wid, str(max(1, b.winfo_width())), str(max(1, b.winfo_height()))],
                               capture_output=True, timeout=3)

    area.bind("<Configure>", relayout)

    def tick() -> None:
        if state["closing"]:
            return
        restarted = runner.sync()
        for dev in restarted:
            embedded.pop(dev, None)
        if embed_ok:
            for t in mgr.tiles:
                dev = t.device_id
                if dev in runner.procs and dev not in embedded and dev in frames:
                    wid = pd.find_window(pd.tile_title(t))
                    if wid:
                        b = frames[dev]["body"]
                        for argv in pd.embed_argvs(wid, b.winfo_id(), b.winfo_width(), b.winfo_height()):
                            subprocess.run(argv, capture_output=True, timeout=3)
                        embedded[dev] = wid
        refresh_headers()
        win.after(500, tick)

    # ---------------------------------------------------------------- push-to-talk
    def ptt_down(_e: Any = None) -> None:
        if state["ptt_release_job"]:            # X11 key auto-repeat: a release followed by a press is not a release
            win.after_cancel(state["ptt_release_job"]); state["ptt_release_job"] = None
            return
        if not ptt.active:
            status(ptt.press())
            if rec is None:
                spoken_var.set(stt_msg)

    def ptt_up_now() -> None:
        state["ptt_release_job"] = None
        if not ptt.active:
            return
        out = ptt.release()
        spoken_var.set(out.message)            # transient only; never written to the log pane or disk
        status("Done." if out.ok else "Not sent: " + out.message)
        relayout()

    def ptt_up(_e: Any = None) -> None:
        state["ptt_release_job"] = win.after(80, ptt_up_now)

    ptt_btn.bind("<ButtonPress-1>", ptt_down)
    ptt_btn.bind("<ButtonRelease-1>", ptt_up)
    win.bind("<KeyPress-F8>", ptt_down)
    win.bind("<KeyRelease-F8>", ptt_up)
    win.bind("<F12>", lambda _e: do_release())
    win.bind("<Control-Escape>", lambda _e: do_release())
    for n in (1, 2, 3):
        win.bind(f"<Control-Key-{n}>", lambda _e, n=n: (mgr.set_focus(mgr.tiles[n - 1].device_id), relayout())
                 if len(mgr.tiles) >= n else None)

    def on_close() -> None:
        state["closing"] = True
        ptt.cancel()
        runner.stop_all()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)
    win._victor = {"mgr": mgr, "runner": runner, "ptt": ptt, "embedded": embedded, "add": add_selected,  # test hooks
                   "refresh": refresh_phones, "frames": frames, "build": build_tiles, "status": status_var,
                   "close": on_close}
    refresh_phones()
    win.after(500, tick)
    return win


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("The phone dashboard needs Tk (Arch: tk, Debian: python3-tk). victor-phone-headless screens still works.",
              file=sys.stderr)
        return 1
    profile = "low" if "--low" in argv else "normal"
    w = open_dashboard(None, profile)
    w.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
