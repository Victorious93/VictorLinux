"""Hotspot tab (Tk): `victor-ctl hotspot ...` - isolated Wi-Fi so a phone can reach the API without a router."""
from tkinter import ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Hotspot")
    ttk.Label(f, wraplength=860, text="Opens an isolated Wi-Fi network from this PC. Joined phones reach only the API "
              "(API key still required); no internet is shared. It never starts by itself. Turn on remote access "
              "(listening on all networks) first.").pack(anchor="w")
    out = app.mkout(f)
    bar = ttk.Frame(f); bar.pack(fill="x", pady=6, before=out)

    def call(*args, busy="working..."):
        app.bg(lambda: app.ctl("hotspot", *args, timeout=120), lambda r: app.out(out, "\n".join(f"{k}: {v}" for k, v in r.items()) if isinstance(r, dict) else str(r)), busy)

    ttk.Button(bar, text="Start hotspot", style="Accent.TButton", command=lambda: call("start", busy="starting hotspot...")).pack(side="left", padx=2)
    ttk.Button(bar, text="Stop", command=lambda: call("stop")).pack(side="left", padx=2)
    ttk.Button(bar, text="Status", command=lambda: call("status")).pack(side="left", padx=2)
    ttk.Button(bar, text="Show name and password", command=lambda: call("show-password")).pack(side="left", padx=2)
    ttk.Button(bar, text="New password", command=lambda: call("new-password")).pack(side="left", padx=2)
    app.after(900, lambda: call("status"))
