"""Tune tab (Tk): `victor-ctl tune run|status|forget` - one-time thread benchmark saved for this PC."""
import json
from tkinter import ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Tune")
    ttk.Label(f, wraplength=860, text="Runs llama-bench once for the selected model with a few thread counts and keeps the fastest. "
              "The result is saved for this PC only and used at start while threads are on automatic. Stop inference first. "
              "GPU layers stay at 'all' and the context stays as you set it.").pack(anchor="w")
    bar = ttk.Frame(f); bar.pack(fill="x", pady=4)
    out = app.mkout(f)

    def show(r):
        cur = r.get("current") if isinstance(r, dict) and "current" in r else r
        if cur:
            text = (f"Fastest: {cur['tg']} tokens/s generation, {cur['pp']} tokens/s prompt with {cur['threads']} threads\n\n"
                    + "\n".join(f"threads {t['threads']}: {t['tg']} tok/s" for t in cur.get("trials", [])))
        else:
            text = "No profile for the selected model on this PC yet."
        app.out(out, text + "\n\n" + json.dumps(r, indent=2))

    for text, args, busy in (("Run benchmark", ("tune", "run"), "benchmarking (a few minutes)..."), ("Show profile", ("tune", "status"), "reading..."),
                             ("Forget this model", ("tune", "forget"), "removing..."), ("Forget all (this PC)", ("tune", "forget", "--all"), "removing...")):
        ttk.Button(bar, text=text, style="Accent.TButton" if text.startswith("Run") else "TButton",
                   command=lambda a=args, b=busy: app.bg(lambda: app.ctl(*a, timeout=3700), show, b)).pack(side="left", padx=2)
