"""Chat tab (Tk): encrypted persistent chats through `victor-ctl chats ...`.

The passphrase is asked once and kept only in this window's memory until Lock is pressed or the window closes;
it is passed to victor-ctl on stdin, never on the command line.
"""
import json
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


def build(app):
    f = ttk.Frame(app.nb, padding=8)
    app.nb.add(f, text="Chat")
    st = {"pw": None, "id": "", "ids": []}
    ttk.Label(f, wraplength=860, text="Chats and chat settings are saved encrypted on the data partition, so they survive a reboot. "
              "Only your passphrase opens them. A model must be running to send messages.").pack(anchor="w")
    top = ttk.Frame(f); top.pack(fill="both", expand=True, pady=4)
    lb = tk.Listbox(top, width=32, height=14); lb.pack(side="left", fill="y")
    log = app.mkout(top)
    entry = ttk.Entry(f); entry.pack(fill="x", pady=4)
    bar = ttk.Frame(f); bar.pack(fill="x")

    def pw(confirm=False):
        if st["pw"] is None:
            p = simpledialog.askstring("Chat passphrase", "Chat passphrase (10+ characters):", show="*", parent=app)
            if not p:
                return None
            if confirm and simpledialog.askstring("Chat passphrase", "Repeat the passphrase:", show="*", parent=app) != p:
                messagebox.showerror("Chat", "The passphrases do not match."); return None
            st["pw"] = p
        return st["pw"]

    def call(*args, done=None):
        p = pw()
        if p is None:
            return
        app.bg(lambda: app.ctl("chats", *args, stdin=p + "\n"), done, "working...")

    def show_list(r):
        st["ids"] = [c["id"] for c in r["chats"]]
        lb.delete(0, "end")
        for c in r["chats"]:
            lb.insert("end", f"{c['title']} ({c['messages']})")

    def show_chat(c):
        st["id"] = c["id"]
        app.out(log, "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in c["messages"]) or "No messages yet.")

    def on_pick(_e=None):
        if lb.curselection():
            call("show", st["ids"][lb.curselection()[0]], done=show_chat)

    def setup():
        p = pw(confirm=True)
        if p:
            app.bg(lambda: app.ctl("chats", "init", stdin=p + "\n"), lambda r: call("list", done=show_list), "creating vault...")

    def send():
        msg = entry.get().strip()
        if msg:
            entry.delete(0, "end")
            call("send", msg, *(["--id", st["id"]] if st["id"] else []), done=lambda c: (show_chat(c), call("list", done=show_list)))

    def export(fmt):
        out = filedialog.asksaveasfilename(defaultextension="." + fmt, title="Export chats (NOT encrypted)")
        if out:
            call("export", "--format", fmt, "--out", out, done=lambda r: messagebox.showinfo("Chat", r["warning"]))

    def clear():
        if messagebox.askyesno("Clear chats", "Delete every saved chat?"):
            app.bg(lambda: app.ctl("chats", "clear", "--yes"), lambda r: (lb.delete(0, "end"), app.out(log, f"Deleted {r['deleted_chats']} chat(s).")))

    def lock():
        st.update(pw=None, id="", ids=[]); lb.delete(0, "end"); app.out(log, "Locked. The passphrase was dropped from memory.")

    lb.bind("<<ListboxSelect>>", on_pick)
    entry.bind("<Return>", lambda e: send())
    for text, fn in (("Create vault", setup), ("Unlock / refresh", lambda: call("list", done=show_list)), ("New chat", lambda: (st.update(id=""), app.out(log, "New chat."))),
                     ("Send", send), ("Export Markdown", lambda: export("md")), ("Export JSON", lambda: export("json")), ("Clear all", clear), ("Lock", lock)):
        ttk.Button(bar, text=text, command=fn, style="Accent.TButton" if text == "Send" else "TButton").pack(side="left", padx=2)
