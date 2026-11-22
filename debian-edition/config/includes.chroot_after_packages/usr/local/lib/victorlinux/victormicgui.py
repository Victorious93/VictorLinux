"""Settings -> Audio -> Microphone window (Tk). Uses the same model as `victor-mic`. UNVERIFIED: written without a
display or Tk in the build sandbox; only py_compile and the pure model were exercised."""
from __future__ import annotations

import victormic as vm


def open_window(parent=None, registry: "vm.SessionRegistry | None" = None) -> None:
    import tkinter as tk
    from tkinter import ttk

    reg = registry or vm.SessionRegistry("local")
    win = tk.Toplevel(parent) if parent else tk.Tk()
    win.title("Microphone")
    s = vm.load_settings()
    sel = tk.StringVar()
    info = tk.StringVar()
    choices: dict[str, dict] = {}

    def rebuild() -> None:
        pc, default = vm.list_pc_sources()
        choices.clear()
        choices["Follow system default"] = {"mode": "default"}
        for p in pc:
            choices[f"{p.label} ({p.kind})"] = {"mode": "pc", "pc_stable_id": p.stable_id}
        for ses in reg.active():
            choices[ses.label()] = {"mode": "phone", "phone": {"device_id": ses.device_id, "provider": ses.provider}}
        box["values"] = list(choices)
        r = vm.resolve(vm.load_settings(), pc, default, reg)
        info.set(f"Selected: {r.selected}\nDelivering: {r.delivering or 'nothing'}  ·  {r.state}\n" + "\n".join(r.recovery))
        win.after(2000, rebuild)           # hot-plug without reopening

    def choose(_e=None) -> None:
        new = dict(vm.load_settings())
        new.update(choices[sel.get()])
        vm.save_settings(new)

    ttk.Label(win, text="Microphone").pack(anchor="w", padx=12, pady=(12, 0))
    box = ttk.Combobox(win, textvariable=sel, state="readonly", width=48)
    box.pack(padx=12, pady=6)
    box.bind("<<ComboboxSelected>>", choose)
    ttk.Label(win, textvariable=info, justify="left").pack(anchor="w", padx=12, pady=6)
    fb = tk.BooleanVar(value=s["fallback_to_preferred_phone"])
    ttk.Checkbutton(win, text="Use my preferred phone when no PC mic is available", variable=fb,
                    command=lambda: vm.save_settings({**vm.load_settings(), "fallback_to_preferred_phone": fb.get()})).pack(anchor="w", padx=12)
    mute = tk.BooleanVar(value=s["muted"])
    ttk.Checkbutton(win, text="Mute", variable=mute,
                    command=lambda: vm.save_settings({**vm.load_settings(), "muted": mute.get()})).pack(anchor="w", padx=12, pady=(0, 12))
    rebuild()
