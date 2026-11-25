"""Images tab (Tk): describe a picture, stable-diffusion.cpp draws it on this PC (off by default, nothing downloaded)."""
import tkinter as tk
from tkinter import ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Images")
    ttk.Label(f, wraplength=860, text="Offline image generation with a stable-diffusion.cpp program and a model you copy into "
              "images/models on the data partition. Off by default; nothing is downloaded. Pictures are saved to images/out.").pack(anchor="w")
    prompt = tk.StringVar()
    row = ttk.Frame(f); row.pack(fill="x", pady=6)
    ttk.Label(row, text="Describe the picture").pack(side="left")
    ttk.Entry(row, textvariable=prompt, width=70).pack(side="left", padx=6, fill="x", expand=True)
    bar = ttk.Frame(f); bar.pack(fill="x", pady=4)
    out = app.mkout(f)

    def draw():
        if prompt.get().strip():
            app.bg(lambda: app.ctl("image", "generate", prompt.get(), timeout=1900), lambda r: app.out(out, str(r)),
                   "drawing (this can take minutes on CPU)...")

    ttk.Button(bar, text="Draw", style="Accent.TButton", command=draw).pack(side="left", padx=2)
    ttk.Button(bar, text="Status", command=lambda: app.bg(lambda: app.ctl("image", "status"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
    ttk.Button(bar, text="Turn on", command=lambda: app.bg(lambda: app.ctl("image", "set", "enabled=true"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
    ttk.Button(bar, text="Turn off", command=lambda: app.bg(lambda: app.ctl("image", "set", "enabled=false"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
