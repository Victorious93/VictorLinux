#!/usr/bin/env python3
"""VictorLinux Phone dashboard: up to three live, controllable Android screens in one window (Tk).

The window draws the tiles' chrome (name, state, toolbar, highlighted input target); the video itself is one scrcpy
process per phone positioned over its tile (see victordash.py for the design and its limits). Mouse and keyboard
actions and spoken commands all go through DashController.execute().
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import victordash as vd
import victordashctl as dc
import victormic as vm
import victorpcdetect as pcd
import victorphone as vp
import victorvoice as vv


def main_dash(argv: list[str] | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("VictorLinux Phone dashboard cannot start: Tk is not installed.\n"
              "  Arch: pacman -S tk      Debian: apt-get install python3-tk\n"
              "The headless tool still works: victor-phone-headless --help\n", file=sys.stderr)
        return 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Run the phone dashboard as your normal desktop user, not root. It needs your display session and your adb keys.", file=sys.stderr)
        return 1
    if not vp.has_display():
        print("No graphical session (DISPLAY/WAYLAND_DISPLAY is empty). Use victor-phone-headless on a console.", file=sys.stderr)
        return 1

    P = vp.PALETTE
    root = tk.Tk()
    root.title("VictorLinux Phone dashboard")
    root.geometry("1280x820")
    root.minsize(900, 600)
    vp.apply_brand_theme(root)

    ctl = dc.DashController(profile=os.environ.get("VICTOR_PHONE_PROFILE", "balanced"))
    feats = ctl.probe()
    cfg = vm.config_path()
    policy = vm.load_policy(cfg)
    speaker = pcd.Speaker()
    status_var = tk.StringVar(value="Ready.")
    heard_var = tk.StringVar(value="")
    mic_var = tk.StringVar(value="")
    pending: dict = {}                                   # a voice command waiting for the owner to name a phone
    ptt_state: dict = {"ptt": None, "pump": None}

    # --- header ----------------------------------------------------------------
    top = ttk.Frame(root, padding=(14, 10)); top.pack(fill="x")
    ttk.Label(top, text="PHONES", style="Eyebrow.TLabel").pack(anchor="w")
    row = ttk.Frame(top); row.pack(fill="x")
    ttk.Label(row, text="VictorLinux Phone dashboard", style="Brand.TLabel").pack(side="left")
    prof = tk.StringVar(value=ctl.profile)
    ttk.Label(row, text="  Quality").pack(side="left")
    cb = ttk.Combobox(row, textvariable=prof, values=list(vd.PROFILES), state="readonly", width=10); cb.pack(side="left", padx=6)

    def set_profile(_e=None):
        ctl.profile = prof.get()
        for t in list(ctl.dash.tiles.values()):
            ctl._spawn(t)
        status_var.set(f"Quality: {ctl.profile}. Streams restarted.")
    cb.bind("<<ComboboxSelected>>", set_profile)
    ttk.Button(row, text="Detect PC", command=lambda: show_detect()).pack(side="right")
    ttk.Button(row, text="Release input (Esc)", command=lambda: act(vv.Action("release_input", scope="layout"))).pack(side="right", padx=6)
    if feats["problems"]:
        ttk.Label(top, text="  ".join(feats["problems"]), style="Muted.TLabel", wraplength=1180).pack(anchor="w", pady=(4, 0))

    body = ttk.Frame(root, padding=(14, 0)); body.pack(fill="both", expand=True)
    side = ttk.Frame(body, style="Card.TFrame", padding=10); side.pack(side="left", fill="y")
    ttk.Label(side, text="Paired / connected phones", style="Eyebrow.TLabel").pack(anchor="w")
    lb = tk.Listbox(side, width=30, height=10, exportselection=False, relief="flat"); lb.pack(fill="y", pady=6)
    scanned: list[vd.Phone] = []

    def refresh():
        nonlocal scanned
        try:
            scanned = ctl.scan()
        except FileNotFoundError:
            scanned = []
            status_var.set("adb is not installed. " + (feats["problems"][0] if feats["problems"] else ""))
        lb.delete(0, "end")
        for p in scanned:
            lb.insert("end", f"{p.name}  [{p.transport}]  {p.state}")
        if not scanned and not feats["problems"]:
            status_var.set(vp.device_hint([]) or "No phones.")

    def add_selected():
        sel = lb.curselection()
        if not sel:
            status_var.set("Select a phone in the list first."); return
        phone = scanned[sel[0]]
        try:
            ctl.add_phone(phone)
        except vd.PickReplacement as e:
            names = [f"{t.slot + 1}: {t.phone.name}" for t in e.candidates]
            pick = ask_choice("All three tiles are in use", "Replace which phone?", names)
            if pick is None:
                return
            ctl.add_phone(phone, replace_slot=e.candidates[pick].slot)
        except ValueError as e:
            status_var.set(str(e))
        redraw()

    ttk.Button(side, text="Show on dashboard", style="Accent.TButton", command=add_selected).pack(fill="x", pady=2)
    ttk.Button(side, text="Show all (max 3)", command=lambda: [ctl.add_phone(p) for p in scanned[:3] if p.ready and not ctl.dash.tile_for(p.stable_id or p.adb_serial)] and redraw()).pack(fill="x", pady=2)
    ttk.Button(side, text="Refresh", command=refresh).pack(fill="x", pady=2)
    ttk.Button(side, text="Pair wireless phone...", command=lambda: pair_wireless()).pack(fill="x", pady=2)

    tiles_area = ttk.Frame(body); tiles_area.pack(side="left", fill="both", expand=True, padx=(10, 0))
    frames: dict[int, dict] = {}

    def ask_choice(title: str, prompt: str, options: list[str]) -> int | None:
        win = tk.Toplevel(root); win.title(title); win.transient(root); win.grab_set()
        ttk.Label(win, text=prompt, padding=10).pack()
        out: list = [None]
        for i, o in enumerate(options):
            ttk.Button(win, text=o, command=lambda i=i: (out.__setitem__(0, i), win.destroy())).pack(fill="x", padx=10, pady=2)
        ttk.Button(win, text="Cancel", command=win.destroy).pack(fill="x", padx=10, pady=8)
        root.wait_window(win)
        return out[0]

    def tile_ui(slot: int) -> dict:
        if slot in frames:
            return frames[slot]
        f = tk.Frame(tiles_area, bg=P["card"], highlightthickness=2, highlightbackground=P["border"])
        title = ttk.Label(f, text="", style="Status.TLabel"); title.pack(fill="x")
        stage = tk.Frame(f, bg="#05080F"); stage.pack(fill="both", expand=True)
        tools = ttk.Frame(f); tools.pack(fill="x")
        for c in range(3):
            tools.columnconfigure(c, weight=1, uniform="b")
        d = {"frame": f, "title": title, "stage": stage}
        for i, (label, a) in enumerate((("Back", "back"), ("Home", "home"), ("Recents", "recents"))):
            ttk.Button(tools, text=label, command=lambda a=a, s=slot: act(vv.Action(a, scope="device"), slot=s)).grid(row=0, column=i, sticky="ew", padx=2, pady=(3, 0))
        d["ctl"] = ttk.Button(tools, text="Control", command=lambda s=slot: toggle_control(s)); d["ctl"].grid(row=1, column=0, sticky="ew", padx=2, pady=3)
        ttk.Button(tools, text="Enlarge", command=lambda s=slot: enlarge(s)).grid(row=1, column=1, sticky="ew", padx=2)
        ttk.Button(tools, text="Disconnect", style="Danger.TButton", command=lambda s=slot: act(vv.Action("disconnect", scope="device"), slot=s)).grid(row=1, column=2, sticky="ew", padx=2)
        f.bind("<Button-1>", lambda e, s=slot: focus_slot(s)); title.bind("<Button-1>", lambda e, s=slot: focus_slot(s))
        frames[slot] = d
        return d

    def focus_slot(slot):
        ctl.dash.focus(slot) if slot in ctl.dash.tiles else None
        redraw(positions=False)

    def toggle_control(slot):
        t = ctl.dash.tiles.get(slot)
        if not t:
            return
        if t.mode == "control":
            status_var.set(ctl.revoke_control(slot).message)
        else:
            if not messagebox.askyesno("Control phone", f"Allow mouse and keyboard control of {t.phone.name} ({t.phone.adb_serial})?\n"
                                       "Only this phone receives input. You can release it at any time with Esc."):
                return
            status_var.set(ctl.grant_control(slot).message)
        redraw()

    enlarged = {"slot": None}

    def enlarge(slot):
        enlarged["slot"] = None if enlarged["slot"] == slot else slot
        redraw()

    def redraw(positions: bool = True):
        live = sorted(ctl.dash.tiles)
        shown = [enlarged["slot"]] if enlarged["slot"] in ctl.dash.tiles else live
        for s, d in list(frames.items()):
            if s not in ctl.dash.tiles:
                d["frame"].destroy(); del frames[s]
        for i, s in enumerate(shown):
            d = tile_ui(s)
            d["frame"].grid(row=0, column=i, sticky="nsew", padx=4)
            tiles_area.columnconfigure(i, weight=1, uniform="t")
            t = ctl.dash.tiles[s]
            mode = "CONTROL" if t.mode == "control" else "view-only"
            d["title"].configure(text=f"{s + 1}  {t.phone.name}  [{t.phone.transport}]  {t.state}  {mode}")
            d["ctl"].configure(text="Release control" if t.mode == "control" else "Control")
            d["frame"].configure(highlightbackground=P["accent"] if ctl.dash.focus_slot == s else P["border"],
                                 highlightcolor=P["accent"])
        for s in live:
            if s not in shown and s in frames:
                frames[s]["frame"].grid_remove()
        for c in range(len(shown), 3):
            tiles_area.columnconfigure(c, weight=0)
        tiles_area.rowconfigure(0, weight=1)

    def pair_wireless():
        win = tk.Toplevel(root); win.title("Pair wireless phone"); win.transient(root)
        ttk.Label(win, padding=10, wraplength=420, text="On the phone: Developer options > Wireless debugging > Pair device with pairing code.\n"
                  "Enter the pairing address and the 6-digit code, then the connect address shown on the main Wireless debugging screen.").pack()
        e1, e2, e3 = (ttk.Entry(win, width=28) for _ in range(3))
        for lbl, e in (("Pairing address (IP:port)", e1), ("Pairing code", e2), ("Connect address (IP:port)", e3)):
            ttk.Label(win, text=lbl).pack(anchor="w", padx=10); e.pack(padx=10, pady=2)

        def go():
            try:
                pa = vd.adb_pair_argv(e1.get().strip()); ca = vd.adb_connect_argv(e3.get().strip())
            except ValueError as ex:
                messagebox.showerror("Pair", str(ex)); return
            code = e2.get().strip()
            if not code.isdigit() or len(code) != 6:
                messagebox.showerror("Pair", "The pairing code is 6 digits."); return

            def work():
                r = subprocess.run(pa, input=code + "\n", capture_output=True, text=True, timeout=30)
                r2 = subprocess.run(ca, capture_output=True, text=True, timeout=30) if r.returncode == 0 else r
                root.after(0, lambda: (status_var.set(((r2.stdout or r2.stderr) or "done").strip().splitlines()[-1]), refresh(), win.destroy()))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(win, text="Pair and connect", style="Accent.TButton", command=go).pack(pady=10)

    # --- actions (mouse, keyboard and voice all land here) -------------------------
    def act(action: vv.Action, slot: int | None = None, key: str | None = None):
        if action.kind in vv.PC:
            do_pc(action); return
        if action.scope == "device" and key is None:
            if slot is None:
                slot = ctl.dash.focus_slot
            t = ctl.dash.tiles.get(slot) if slot is not None else None
            key = (t.phone.stable_id or t.phone.adb_serial) if t else None
        r = ctl.execute(action, target_key=key)
        status_var.set(r.message if r.message not in ("layout", "pc") else status_var.get())
        if action.kind in ("show_all", "show_n"):
            n = 3 if action.kind == "show_all" else int(action.param)
            for p in [p for p in scanned if p.ready][:n]:
                try:
                    ctl.add_phone(p)
                except vd.PickReplacement:
                    break
        redraw()

    def do_pc(action: vv.Action):
        if action.kind == "stop_reading":
            speaker.stop(); status_var.set("Stopped reading."); return
        res = pcd.read_selection()
        if not res.ok:
            status_var.set(res.explanation); return
        try:
            speaker.say(res.text); status_var.set(f"Reading {len(res.text)} characters ({res.explanation}).")
        except RuntimeError as e:
            status_var.set(str(e))

    # --- microphone ---------------------------------------------------------------
    mic = ttk.Frame(root, style="Card.TFrame", padding=10); mic.pack(fill="x", padx=14, pady=8)
    ttk.Label(mic, text="PC MICROPHONE", style="Eyebrow.TLabel").grid(row=0, column=0, sticky="w")
    mic_combo = ttk.Combobox(mic, state="readonly", width=46); mic_combo.grid(row=1, column=0, sticky="w")
    mode_var = tk.StringVar(value=policy.mode)
    when_var = tk.BooleanVar(value=policy.use_preferred_when_connected)
    ttk.Radiobutton(mic, text="Follow system default", variable=mode_var, value=vm.FOLLOW_DEFAULT, command=lambda: save_pol()).grid(row=1, column=1, padx=8)
    ttk.Radiobutton(mic, text="Use selected mic", variable=mode_var, value=vm.PREFERRED, command=lambda: save_pol()).grid(row=1, column=2)
    ttk.Checkbutton(mic, text="Use this mic when connected", variable=when_var, command=lambda: save_pol()).grid(row=1, column=3, padx=8)
    level = ttk.Progressbar(mic, length=140, maximum=100); level.grid(row=1, column=4, padx=8)
    ttk.Label(mic, textvariable=mic_var, style="Muted.TLabel", wraplength=1000).grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
    btns = ttk.Frame(mic); btns.grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))
    mgr: vm.MicManager
    shown_sources: list[vm.Source] = []

    def save_pol():
        policy.mode, policy.use_preferred_when_connected = mode_var.get(), when_var.get()
        i = mic_combo.current()
        if 0 <= i < len(shown_sources):
            policy.preferred_id = shown_sources[i].stable_id
        try:
            vm.save_policy(cfg, policy)
        except OSError as e:
            status_var.set(f"Could not save microphone settings: {e}")
        mgr.refresh()

    def on_mic(st: vm.MicStatus):
        def ui():
            shown_sources[:] = st.connected
            mic_combo["values"] = [f"{s.description} ({s.kind})" for s in st.connected]
            if st.active and st.active in st.connected:
                mic_combo.current(st.connected.index(st.active))
            mic_var.set(st.message + ("  Ready for push-to-talk." if st.ready_for_ptt else ""))
        root.after(0, ui)

    def on_cancel(why: str):
        def ui():
            if ptt_state["ptt"]:
                ptt_state["ptt"].cancel(why)
            heard_var.set(why)
        root.after(0, ui)

    mgr = vm.MicManager(vm.pactl_listing, policy, on_change=on_mic, on_cancel=on_cancel)
    mic_combo.bind("<<ComboboxSelected>>", lambda e: (mode_var.set(vm.PREFERRED), save_pol()))

    def test_mic():
        try:
            src = mgr.begin_utterance()
        except RuntimeError as e:
            status_var.set(str(e)); return
        def work():
            try:
                p = subprocess.Popen(vm.capture_argv(src.name), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                peak = 0.0
                for _ in range(8):
                    peak = max(peak, vm.pcm_level(p.stdout.read(3200)))
                    root.after(0, lambda v=peak: level.configure(value=v * 100))
                p.terminate()
            except OSError as e:
                root.after(0, lambda: status_var.set(f"Microphone test failed: {e}"))
            finally:
                mgr.end_utterance()
        threading.Thread(target=work, daemon=True).start()

    def toggle_mute():
        st = mgr.status
        if not st.active:
            status_var.set("No microphone to mute."); return
        try:
            vm.set_mute(st.active.name, not st.muted)
        except (OSError, subprocess.SubprocessError) as e:
            status_var.set(f"Could not change mute: {e}")
        mgr.refresh()

    ttk.Button(btns, text="Test mic", command=test_mic).pack(side="left")
    ttk.Button(btns, text="Mute / unmute", command=toggle_mute).pack(side="left", padx=6)

    # --- push to talk -----------------------------------------------------------------
    sp = vv.speech_status()
    ptt_btn = ttk.Button(btns, text="Hold to talk (Ctrl+Space)", style="Accent.TButton")
    ptt_btn.pack(side="left", padx=14)
    ttk.Button(btns, text="Read selected PC text", command=lambda: do_pc(vv.Action("read_selection", scope="pc"))).pack(side="left")
    ttk.Label(btns, textvariable=heard_var, style="Muted.TLabel").pack(side="left", padx=14)
    if not sp["ready"]:
        ptt_btn.state(["disabled"])
        heard_var.set("Voice is off: " + sp["help"].splitlines()[0] + " (see docs/phone-mirroring.md)")

    def current_focus_key():
        t = ctl.dash.tiles.get(ctl.dash.focus_slot) if ctl.dash.focus_slot is not None else None
        return (t.phone.stable_id or t.phone.adb_serial) if t else None

    def ptt_down(_e=None):
        if ptt_state["ptt"] or not sp["ready"]:
            return
        try:
            src = mgr.begin_utterance()
            ptt = vv.PushToTalk(lambda name: vv.ProcessCapture(vm.capture_argv(name)), lambda: vv.VoskRecognizer(sp["model"]),
                                lambda: src.name, current_focus_key, tts_busy=lambda: speaker.busy)
            ptt.press()
        except (RuntimeError, vv.SpeechUnavailable) as e:
            heard_var.set(str(e)); mgr.end_utterance(); return
        ptt_state["ptt"] = ptt
        heard_var.set("Listening...")

        def pump():
            if ptt_state["ptt"] is ptt:
                threading.Thread(target=ptt.pump, daemon=True).start()
                root.after(100, pump)
        pump()

    def ptt_up(_e=None):
        ptt = ptt_state["ptt"]
        if not ptt:
            return
        ptt_state["ptt"] = None
        mgr.end_utterance()
        utt, a = ptt.release()
        if a is None:
            return
        if isinstance(a, vv.Unrecognized):
            heard_var.set("Not a dashboard command."); return
        run_voice(a, utt)

    def run_voice(a: vv.Action, utt: vv.Utterance):
        if a.scope == "device":
            r = vv.resolve_target(a.target, ctl.keys_for(), utt.focus_at_start, current_focus_key())
            if isinstance(r, vv.NeedTarget):
                pick = ask_choice("Which phone?", r.reason, list(r.candidates)) if r.candidates else None
                if pick is None:
                    heard_var.set(r.reason); return
                key = next((k.stable_key for k in ctl.keys_for() if k.names[0] == r.candidates[pick]), None)
            else:
                key = r.key
            t = ctl.dash.tile_for(key) if key else None
            heard_var.set(f"{a.kind} -> {t.phone.name if t else '?'}")
            act(a, key=key)
        else:
            heard_var.set(f"{a.kind}")
            act(a)

    ptt_btn.bind("<ButtonPress-1>", ptt_down); ptt_btn.bind("<ButtonRelease-1>", ptt_up)
    root.bind("<Control-space>", ptt_down); root.bind("<KeyRelease-space>", ptt_up)
    root.bind("<Escape>", lambda e: act(vv.Action("release_input", scope="layout")))
    root.bind("<F11>", lambda e: root.attributes("-fullscreen", not root.attributes("-fullscreen")))

    # --- detect PC --------------------------------------------------------------------
    def show_detect():
        info = pcd.detect_pc()
        win = tk.Toplevel(root); win.title("This PC"); win.geometry("640x420")
        txt = tk.Text(win, wrap="word"); txt.pack(fill="both", expand=True)
        lines = [f"Computer: {info['hostname']}", f"OS: {info['os']}",
                 f"Session: {info['session']['type']}  display={info['session']['display'] or info['session']['wayland']}"]
        lines += [f"Display {d['name']}: {d['width']}x{d['height']}" for d in info["displays"]]
        lines += [f"Microphone: {a['description']} ({a['kind']}){' [active]' if a['active'] else ''}{' MUTED' if a['muted'] else ''}" for a in info["audio_inputs"]]
        lines.append(info["audio_message"])
        lines.append("Phone -> this PC sharing: " + info["sharing"]["why"])
        txt.insert("1.0", "\n".join(lines)); txt.configure(state="disabled")

    # --- loops ------------------------------------------------------------------------
    watcher = vm.HotplugWatcher(lambda: root.after(0, mgr.refresh))
    watcher.start()

    def tick():
        notes = ctl.poll()
        if notes:
            status_var.set(" ".join(notes)); redraw()
        root.after(2000, tick)

    def on_close():
        watcher.stop()
        speaker.stop()
        ctl.shutdown()
        root.destroy()

    ttk.Label(root, textvariable=status_var, style="Status.TLabel").pack(fill="x", side="bottom")
    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh(); mgr.refresh(); redraw(); root.after(2000, tick)
    if os.environ.get("VICTOR_PHONE_DASH_AUTOADD"):                   # render test: show the first N phones at start
        for ph in scanned[: int(os.environ["VICTOR_PHONE_DASH_AUTOADD"])]:
            ctl.add_phone(ph)
        redraw()
    if os.environ.get("VICTOR_PHONE_DASH_SELFTEST"):                 # used by the Xvfb render test
        root.after(int(os.environ["VICTOR_PHONE_DASH_SELFTEST"]), on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main_dash())
