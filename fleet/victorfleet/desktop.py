"""Desktop-side client for terminal sessions: runs in the owner's own login session on the coordinator machine and talks only to the
coordinator's private Unix socket (never the network). Contents are written to the terminal and nowhere else."""
import base64, errno, os, select, signal, sys, termios, time, tty

TERMINAL_DONE = {"denied", "closed", "failed", "revoked", "expired", "disconnected"}


class Relay:
    """Sequenced chunk exchange with the coordinator for one session. De-duplicates what it receives, numbers what it sends."""

    def __init__(self, call, sid, token):
        self.call, self.sid, self.token = call, sid, token
        self.send_seq = 0
        self.recv_seq = 0
        self.state = "requested"
        self.reason = None
        self.exit_status = None
        self.backlog = []             # chunks received while still waiting for approval (kept, never dropped)

    def exchange(self, outgoing=(), wait_s=0):
        chunks = []
        for c in outgoing:
            self.send_seq += 1
            chunks.append(dict(c, seq=self.send_seq))
        r = self.call("POST", f"/terminal/{self.sid}/exchange", {"token": self.token, "ack": self.recv_seq, "chunks": chunks, "wait_s": wait_s})
        self.state, self.reason = r["state"], r.get("reason")
        if r.get("exit_status") is not None:
            self.exit_status = r["exit_status"]
        fresh, self.backlog = self.backlog, []
        for c in r.get("chunks", []):
            if c["seq"] == self.recv_seq + 1:
                self.recv_seq = c["seq"]
                fresh.append(c)
        return fresh


def data_chunk(b, channel=None):
    c = {"kind": "data", "data": base64.b64encode(b).decode()}
    if channel:
        c["channel"] = channel
    return c


def wait_open(relay, say=lambda s: None, timeout_s=150):
    """Block until the phone owner approved (state 'open') or the session ended. Returns the final state."""
    say("waiting for the phone owner to approve…")
    end = time.time() + timeout_s
    while time.time() < end:
        got = relay.exchange(wait_s=2)       # (bind first: exchange() swaps relay.backlog)
        relay.backlog.extend(got)
        if relay.state == "open" or relay.state in TERMINAL_DONE:
            break
    return relay.state


def run_oneshot(relay, out=sys.stdout.buffer, err=sys.stderr.buffer):
    """Collect output until the session ends. Returns the command's exit status (None when it never ran)."""
    while relay.state not in TERMINAL_DONE:
        for c in relay.exchange(wait_s=5):
            if c["kind"] == "data":
                (err if c.get("channel") == "stderr" else out).write(base64.b64decode(c["data"]))
                (err if c.get("channel") == "stderr" else out).flush()
            elif c["kind"] == "exit":
                relay.exit_status = c["status"]
    for c in relay.exchange():           # drain what arrived with the closing state
        if c["kind"] == "data":
            (err if c.get("channel") == "stderr" else out).write(base64.b64decode(c["data"]))
        elif c["kind"] == "exit":
            relay.exit_status = c["status"]
    return relay.exit_status


def run_terminal(relay, stdin_fd=0, stdout_fd=1, size=lambda: os.get_terminal_size(), say=lambda s: None):
    """Interactive attach. `~.` at the start of a line detaches and closes the session (like ssh). Ctrl-C is just byte 0x03 for the
    remote pty's line discipline. Returns the remote exit status (or None)."""
    resized = [False]
    old = None
    if os.isatty(stdin_fd):
        old = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    prev_winch = signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    line_start, tilde = True, False
    try:
        while relay.state not in TERMINAL_DONE:
            out = []
            if resized[0]:
                resized[0] = False
                try:
                    sz = size()
                    out.append({"kind": "resize", "cols": sz.columns, "rows": sz.lines})
                except OSError:
                    pass
            r, _, _ = select.select([stdin_fd], [], [], 0.2)
            if r:
                try:
                    b = os.read(stdin_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:
                        b = b""
                    else:
                        raise
                if not b:
                    out.append({"kind": "eof"})
                else:
                    keep = bytearray()
                    for ch in b:
                        if tilde and ch == ord("."):
                            out.append({"kind": "close", "reason": "detached"})
                            keep.clear()
                            relay.exchange(out)
                            return relay.exit_status
                        tilde = line_start and ch == ord("~")
                        line_start = ch in (10, 13)
                        keep.append(ch)
                    if keep:
                        out.append(data_chunk(bytes(keep)))
            for c in relay.exchange(out, wait_s=0 if r else 0.3):
                if c["kind"] == "data":
                    os.write(stdout_fd, base64.b64decode(c["data"]))
                elif c["kind"] == "exit":
                    relay.exit_status = c["status"]
        for c in relay.exchange():
            if c["kind"] == "data":
                os.write(stdout_fd, base64.b64decode(c["data"]))
            elif c["kind"] == "exit":
                relay.exit_status = c["status"]
    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
        if old is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
    return relay.exit_status
