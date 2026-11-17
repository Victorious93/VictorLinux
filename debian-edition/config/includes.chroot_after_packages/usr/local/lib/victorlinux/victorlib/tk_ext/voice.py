"""Voice tab (Tk): record a few seconds (arecord), transcribe with whisper.cpp, ask the local model, speak with Piper."""
import tkinter as tk
from tkinter import filedialog, ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Voice")
    ttk.Label(f, wraplength=860, text="Offline voice chat. Speech recognition (whisper.cpp) and the voice (Piper) run on this PC "
              "with files you put in the voice folder. Off by default; recordings are deleted right after use.").pack(anchor="w")
    secs = tk.IntVar(value=6)
    speak = tk.BooleanVar(value=True)
    bar = ttk.Frame(f); bar.pack(fill="x", pady=6)
    out = app.mkout(f)

    def show(r):
        if isinstance(r, dict) and "reply" in r:
            app.out(out, f"You: {r['text']}\n\nModel: {r['reply']}" + (f"\n\n({r['note']})" if r.get("note") else ""))
        else:
            app.out(out, str(r))

    def talk():
        args = ["voice", "listen", "--seconds", str(secs.get())] + (["--speak"] if speak.get() else [])
        app.bg(lambda: app.ctl(*args, timeout=900), show, f"listening for {secs.get()} s...")

    def from_file():
        p = filedialog.askopenfilename(filetypes=[("16 kHz mono WAV", "*.wav")])
        if p:
            app.bg(lambda: app.ctl("voice", "ask", p, *(["--speak"] if speak.get() else []), timeout=900), show, "transcribing...")

    ttk.Button(bar, text="Talk", style="Accent.TButton", command=talk).pack(side="left", padx=2)
    ttk.Spinbox(bar, from_=2, to=24, width=4, textvariable=secs).pack(side="left", padx=2)
    ttk.Label(bar, text="seconds").pack(side="left")
    ttk.Checkbutton(bar, text="Speak reply", variable=speak).pack(side="left", padx=8)
    ttk.Button(bar, text="Ask from WAV file...", command=from_file).pack(side="left", padx=2)
    ttk.Button(bar, text="Status", command=lambda: app.bg(lambda: app.ctl("voice", "status"), lambda r: app.out(out, str(r)))).pack(side="left", padx=2)
    ttk.Button(bar, text="Turn on", command=lambda: app.bg(lambda: app.ctl("voice", "set", "enabled=true"), lambda r: app.out(out, "Voice chat on."))).pack(side="left", padx=2)
