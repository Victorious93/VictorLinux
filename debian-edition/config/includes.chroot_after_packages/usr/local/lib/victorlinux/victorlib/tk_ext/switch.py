"""Switch-model tab (Tk): `victor-ctl switch MODEL` - no reboot, rolls back on failure."""
import os
import tkinter as tk
from tkinter import ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Switch model")
    ttk.Label(f, wraplength=860, text="Loads another model without a reboot. The API is unavailable while the new model loads "
              "(Debian serves it straight from llama-server). If the new model fails, the previous one is started again.").pack(anchor="w")
    names = tk.StringVar()
    box = ttk.Combobox(f, textvariable=names, state="readonly", width=60); box.pack(anchor="w", pady=6)
    out = app.mkout(f)

    def refresh():
        def done(r):
            files = [m["name"] if isinstance(m, dict) else str(m) for m in (r if isinstance(r, list) else r.get("models", []))]
            box["values"] = files
        app.bg(lambda: app.ctl("model", "list"), done, "reading models...")

    def go():
        if names.get():
            app.bg(lambda: app.ctl("switch", names.get(), timeout=700), lambda r: app.out(out, str(r)), "switching model...")

    bar = ttk.Frame(f); bar.pack(fill="x")
    ttk.Button(bar, text="Refresh list", command=refresh).pack(side="left", padx=2)
    ttk.Button(bar, text="Switch now", style="Accent.TButton", command=go).pack(side="left", padx=2)
    app.after(800, refresh)
