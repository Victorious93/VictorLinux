"""Coral tab (Tk): Edge TPU status and a vision test. Vision and wake-word only; the TPU cannot run chat models."""
from tkinter import filedialog, ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Coral TPU")
    ttk.Label(f, wraplength=860, text="Coral Edge TPU: runs small vision and wake-word models you copy into coral/ on the data "
              "partition (compiled with edgetpu_compiler). It cannot run chat models. Off by default; nothing is downloaded.").pack(anchor="w")
    bar = ttk.Frame(f); bar.pack(fill="x", pady=6)
    out = app.mkout(f)

    def classify():
        p = filedialog.askopenfilename(filetypes=[("Pictures", "*.jpg *.jpeg *.png *.bmp")])
        if p:
            app.bg(lambda: app.ctl("coral", "classify", p), lambda r: app.out(out, str(r)), "classifying on the TPU...")

    ttk.Button(bar, text="Status", command=lambda: app.bg(lambda: app.ctl("coral", "status"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
    ttk.Button(bar, text="Classify a picture...", command=classify).pack(side="left", padx=2)
    ttk.Button(bar, text="Turn on", command=lambda: app.bg(lambda: app.ctl("coral", "set", "enabled=true"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
    ttk.Button(bar, text="Turn off", command=lambda: app.bg(lambda: app.ctl("coral", "set", "enabled=false"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
