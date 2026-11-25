"""SIMULATED phone broker for tests only. Stands in for DroidCommand + VictorSuite + VictorSMS on a phone: it speaks the real 1.1 wire protocol
over real HTTPS, owns a REAL pseudoterminal / subprocess in a private directory containing a harmless marker file, and applies a configurable
owner decision. It proves the coordinator, relay and contract; it is NOT Android and NOT VictorSuite."""
import base64, fcntl, json, os, pty, select, signal, struct, subprocess, tempfile, termios, threading, time
from simworker import SimWorker
from victorfleet.protocol import envelope, terminal_binding_hash, draft_content_hash

NEW_OPS = {"terminal.session": {"available": True, "granted": True, "status": "ready"},
           "shell.oneshot": {"available": True, "granted": True, "status": "ready"},
           "message.draft.sms": {"available": True, "granted": True, "status": "ready"},
           "message.draft.email": {"available": True, "granted": True, "status": "ready"},
           "mic.offer.droidcommand": {"available": True, "granted": True, "status": "ready"},
           "mic.offer.keyboard": {"available": True, "granted": True, "status": "ready"}}


class SimPhone(SimWorker):
    def __init__(self, *a, marker="PHONE-MARKER", ops=None, **kw):
        merged = dict(NEW_OPS)
        merged.update(ops or {})
        super().__init__(*a, ops=merged, **kw)
        self.marker = marker
        self.home = tempfile.mkdtemp(prefix="simphone-home-")
        open(os.path.join(self.home, f"{marker}.txt"), "w").write(marker)
        self.approve = True                 # owner's decision on the phone
        self.decide_draft = "received"      # received | declined | submitted | failed | ignore
        self.tamper_binding = False
        self.sessions = {}                  # sid -> dict(pid, fd, ack, seen, seq, mode)
        self.drafts = {}                    # draft_id -> state  (de-dup by id: a retried handoff can never submit twice)
        self.submitted = []                 # what "Send" actually submitted (should be at most once per draft)
        self.opens_seen = []
        self.mic_offers = {}                # offer_id -> payload, MEMORY ONLY (stands in for DroidCommand's in-memory inbox)
        self.decide_mic = "accepted"        # accepted | declined | ignore  (the owner's Start/Dismiss on the phone screen)
        self.starting = []
        self.outbound = []
        self.stop_flag = False
        self.paused = False                 # simulate a dropped connection
        self.thread = None
        self.lock = threading.Lock()

    # ----- lifecycle
    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_flag = True
        if self.thread:
            self.thread.join(5)
        for s in self.sessions.values():
            self._kill(s)

    def _loop(self):
        while not self.stop_flag:
            if self.paused:
                time.sleep(0.05)
                continue
            try:
                out = []
                with self.lock:
                    out.extend(self.outbound)
                    self.outbound = []
                st, body = self.exchange(out, wait_s=0)
                if st == 200:
                    for m in body["messages"]:
                        self._handle(m)
                    if out and any(m["type"] == "terminal_status" and m["payload"]["state"] == "open" for m in out):
                        while self.starting:                    # `open` was accepted by the coordinator in this exchange
                            self._start_shell(self.starting.pop(0))
                for sid, s in list(self.sessions.items()):
                    self._pump(sid, s)
            except Exception as e:                      # pragma: no cover - surfaced through test failures
                self.error = e
            time.sleep(0.03)

    error = None

    def send(self, mtype, payload):
        with self.lock:
            self.outbound.append(self.msg(mtype, payload))

    # ----- coordinator messages
    def _handle(self, m):
        t, p = m["type"], m["payload"]
        if t == "terminal_open":
            self.opens_seen.append(p["session_id"])
            self.send("terminal_status", {"session_id": p["session_id"], "state": "awaiting_approval"})
            expect = terminal_binding_hash(p["session_id"], self.device_id, p["desktop"], p["privilege"], p["mode"], p.get("command"), p["nonce"])
            if p["binding"] != expect:
                self.send("terminal_status", {"session_id": p["session_id"], "state": "failed", "reason": "binding not reproducible"})
                return
            if not self.approve:
                self.send("terminal_status", {"session_id": p["session_id"], "state": "denied", "reason": "owner declined"})
                return
            # Contract: announce `open` (with the approval binding) BEFORE any process output exists; start the shell only after that.
            self.send("terminal_status", {"session_id": p["session_id"], "state": "open",
                                          "binding": p["binding"] if not self.tamper_binding else "0" * 64})
            self.starting.append(p)
        elif t == "terminal_close":
            s = self.sessions.get(p["session_id"])
            if s:
                self._kill(s)
        elif t == "mic_offer":
            self._handle_mic(p)
        elif t == "draft_deliver":
            did = p["draft_id"]
            assert draft_content_hash(p["channel"], p["draft"]) == p["content_hash"]
            if did in self.drafts:                       # retried handoff: report current state, NEVER act on it
                self.send("draft_status", {"draft_id": did, "state": self.drafts[did], "content_hash": p["content_hash"]})
                return
            self.drafts[did] = "received"
            self.send("draft_status", {"draft_id": did, "state": "received", "content_hash": p["content_hash"]})
            d = self.decide_draft
            if d == "declined":
                self.drafts[did] = "declined"
                self.send("draft_status", {"draft_id": did, "state": "declined", "content_hash": p["content_hash"]})
            elif d == "submitted":
                self.submitted.append((did, p["draft"]))      # the owner pressed Send ON THE PHONE
                self.drafts[did] = "submitted"
                self.send("draft_status", {"draft_id": did, "state": "submitted", "content_hash": p["content_hash"]})
            elif d == "failed":
                self.drafts[did] = "failed"
                self.send("draft_status", {"draft_id": did, "state": "failed", "detail": "SMTP refused", "content_hash": p["content_hash"]})

    def _handle_mic(self, p):
        oid = p["offer_id"]
        if oid in self.mic_offers:
            return
        self.mic_offers[oid] = p
        self.send("mic_status", {"offer_id": oid, "state": "received"})
        if self.decide_mic in ("accepted", "declined"):
            self.send("mic_status", {"offer_id": oid, "state": self.decide_mic})

    # ----- the "VictorSuite" side: a real pty / subprocess
    def _start_shell(self, p):
        sid = p["session_id"]
        env = {"PATH": os.environ["PATH"], "HOME": self.home, "PS1": "$ ", "TERM": "xterm"}
        if p["mode"] == "oneshot":
            proc = subprocess.Popen(["/bin/sh", "-c", p["command"]], cwd=self.home, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL)
            self.sessions[sid] = {"proc": proc, "ack": 0, "seen": 0, "seq": 0, "mode": "oneshot", "dead": False, "pending": b"",
                                  "bufs": {"stdout": proc.stdout, "stderr": proc.stderr}}
        else:
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(self.home)
                os.execve("/bin/sh", ["/bin/sh"], env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", p["rows"], p["cols"], 0, 0))
            self.sessions[sid] = {"pid": pid, "fd": fd, "ack": 0, "seen": 0, "seq": 0, "mode": "interactive", "dead": False}

    def _kill(self, s):
        s["dead"] = True
        try:
            if s["mode"] == "interactive":
                os.kill(s["pid"], signal.SIGKILL); os.waitpid(s["pid"], 0); os.close(s["fd"])
            else:
                s["proc"].kill(); s["proc"].wait()
        except Exception:
            pass

    def _pump(self, sid, s):
        if s["dead"]:
            return
        chunks = []
        if s["mode"] == "interactive":
            r, _, _ = select.select([s["fd"]], [], [], 0)
            if r:
                try:
                    d = os.read(s["fd"], 4096)
                except OSError:
                    d = b""
                if d:
                    s["seq"] += 1
                    chunks.append({"seq": s["seq"], "kind": "data", "data": base64.b64encode(d).decode()})
            done, status = os.waitpid(s["pid"], os.WNOHANG)
            if done:
                s["seq"] += 1
                code = os.waitstatus_to_exitcode(status)
                chunks.append({"seq": s["seq"], "kind": "exit", "status": code})
                s["dead"] = True
                self.send("terminal_status", {"session_id": sid, "state": "closed", "exit_status": code, "reason": "shell exited"})
        else:
            for name, f in list(s["bufs"].items()):
                r, _, _ = select.select([f], [], [], 0)
                if r:
                    d = os.read(f.fileno(), 4096)
                    if d:
                        s["seq"] += 1
                        chunks.append({"seq": s["seq"], "kind": "data", "channel": name, "data": base64.b64encode(d).decode()})
                    else:
                        del s["bufs"][name]                      # EOF on this stream
            if s["proc"].poll() is not None and not s["bufs"]:
                s["seq"] += 1
                chunks.append({"seq": s["seq"], "kind": "exit", "status": s["proc"].returncode})
                s["dead"] = True
                self.send("terminal_status", {"session_id": sid, "state": "closed", "exit_status": s["proc"].returncode, "reason": "command finished"})
        st, body, _ = self.req("POST", f"/v1/terminal/{sid}/exchange", {"ack": s["ack"], "chunks": chunks, "wait_s": 0})
        if st != 200:
            self._kill(s)
            return
        for c in body["chunks"]:
            if c["seq"] <= s["ack"]:
                continue                                  # duplicate: dedup by seq, never write input twice
            s["ack"] = c["seq"]
            if s["mode"] != "interactive" or s["dead"]:
                if c["kind"] == "close":
                    self._kill(s)
                continue
            if c["kind"] == "data":
                os.write(s["fd"], base64.b64decode(c["data"]))
            elif c["kind"] == "resize":
                fcntl.ioctl(s["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", c["rows"], c["cols"], 0, 0))
            elif c["kind"] == "signal":
                os.killpg(os.getpgid(s["pid"]), getattr(signal, "SIG" + c["name"]))
            elif c["kind"] == "close":
                self._kill(s)
        # an unacked outbound chunk that the relay refused would have shown up as a sequence gap there; ours are always contiguous
        if s["dead"] and not s.get("final_sent"):
            s["final_sent"] = True
