"""victor-fleet: owner CLI. Talks to the running coordinator over its private Unix socket (never to the network).
`serve` runs the coordinator in the foreground (systemd unit or a terminal). Everything else is a client."""
import argparse, http.client, json, os, signal, socket, sys, time
from . import persistence


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost"); self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX); self.sock.settimeout(60); self.sock.connect(self.path)


def sock_path():
    d = os.environ.get("VICTOR_FLEET_DIR") or persistence.resolve()["path"]
    return os.path.join(d, "admin.sock")


def call(method, path, body=None):
    try:
        c = UnixConn(sock_path())
        c.request(method, path, json.dumps(body or {}) if method == "POST" else None, {"Content-Type": "application/json"})
        r = c.getresponse(); data = json.loads(r.read() or b"{}")
    except (FileNotFoundError, ConnectionRefusedError):
        print("ERROR: the fleet coordinator is not running (start it with: victor-fleet start)", file=sys.stderr); sys.exit(3)
    if r.status != 200:
        print(f"ERROR: {data.get('error', {}).get('message', data)}", file=sys.stderr); sys.exit(2)
    return data


def show(obj):
    print(json.dumps(obj, indent=2, default=str))


def data_dir():
    return os.environ.get("VICTOR_FLEET_DIR") or persistence.resolve()["path"]


def running_pid():
    try:
        pid = int(open(os.path.join(data_dir(), "coordinator.pid")).read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start_bg():
    import subprocess
    if running_pid():
        print(f"already running (pid {running_pid()})"); return
    d = data_dir(); os.makedirs(d, mode=0o700, exist_ok=True)
    log = open(os.path.join(d, "coordinator.log"), "ab")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.environ.get("PYTHONPATH")])))
    subprocess.Popen([sys.executable, "-m", "victorfleet.cli", "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    for _ in range(50):
        time.sleep(0.2)
        if os.path.exists(sock_path()) and running_pid():
            print(f"coordinator started (pid {running_pid()}); local-only unless you enabled fleet networking"); return
    print("ERROR: coordinator did not come up; see: victor-fleet logs --file", file=sys.stderr); sys.exit(3)


def stop_bg():
    pid = running_pid()
    if not pid:
        print("not running"); return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.2)
        if not running_pid():
            print("stopped"); return
    print("ERROR: coordinator did not stop within 10s", file=sys.stderr); sys.exit(3)


def serve(args):
    from .server import FleetService, load_config, save_config
    svc = FleetService()
    with open(os.path.join(svc.dir, "coordinator.pid"), "w") as f:
        f.write(str(os.getpid()))
    if svc.persistence["durable"] is False:
        print(f"WARNING: fleet state is NOT durable: {svc.persistence['reason']}", file=sys.stderr)
    svc.start()
    print(f"victor-fleet coordinator on https://{svc.bind}:{svc.port}  fingerprint sha256:{svc.fingerprint}", flush=True)
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.append(1))
    while not stop:
        time.sleep(0.5)
    svc.stop()
    try: os.unlink(os.path.join(svc.dir, "coordinator.pid"))
    except OSError: pass


def print_features(rows):
    for d in rows:
        f = d.get("features", {})
        print(f"{d['id']}  {d['name']}  connected={d['connected']}")
        for k in ("terminal", "shell_oneshot", "terminal_root", "messaging_sms", "messaging_email"):
            x = f.get(k, {})
            print(f"    {k:16} {x.get('state','?'):28} {x.get('action') or ''}")


def _open(a, mode, command=None):
    cols, rows = 80, 24
    try:
        sz = os.get_terminal_size(); cols, rows = sz.columns, sz.lines
    except OSError:
        pass
    who = "victorlinux:" + (os.environ.get("USER") or "owner") + "@" + socket.gethostname()
    return call("POST", "/terminal", {"device": a.device, "desktop": who, "mode": mode, "privilege": "root" if a.privileged else "user",
                                      "command": command, "cols": cols, "rows": rows})


def cmd_terminal(a):
    from . import desktop
    r = _open(a, "interactive")
    rel = desktop.Relay(call, r["session_id"], r["attach_token"])
    st = desktop.wait_open(rel, lambda s: print(s, file=sys.stderr))
    if st != "open":
        print(f"terminal not opened: {st}" + (f" ({rel.reason})" if rel.reason else ""), file=sys.stderr); return 4
    print("connected. Press Enter then ~. to detach.", file=sys.stderr)
    status = desktop.run_terminal(rel)
    print(f"\r\nsession ended: {rel.state}" + (f" ({rel.reason})" if rel.reason else ""), file=sys.stderr)
    return status if isinstance(status, int) else 0


def cmd_exec(a):
    import shlex
    from . import desktop
    cmd = a.shell if a.shell else (" ".join(shlex.quote(x) for x in a.argv) if a.argv else None)
    if not cmd:
        print("ERROR: give a command (after --) or --shell 'cmd'", file=sys.stderr); return 2
    r = _open(a, "oneshot", cmd)
    rel = desktop.Relay(call, r["session_id"], r["attach_token"])
    st = desktop.wait_open(rel, lambda s: print(s, file=sys.stderr))
    finished_fast = st == "closed" and (rel.exit_status is not None or any(c["kind"] == "exit" for c in rel.backlog))   # ran and ended before we looked
    if st != "open" and not finished_fast:
        print(f"command not run: {st}" + (f" ({rel.reason})" if rel.reason else ""), file=sys.stderr); return 4
    status = desktop.run_oneshot(rel)
    if status is None:
        print(f"no exit status was reported ({rel.state}: {rel.reason})", file=sys.stderr); return 5
    return status


def cmd_message(a):
    import base64, mimetypes
    if a.mcmd == "list": return show(call("GET", "/drafts"))
    if a.mcmd == "status": return show(call("GET", f"/drafts/{a.draft}"))
    if a.mcmd == "retry": return show(call("POST", f"/drafts/{a.draft}/retry"))
    body = a.body if a.body is not None else (open(a.body_file).read() if a.body_file else sys.stdin.read())
    channel = a.mcmd.split("-", 1)[1]
    draft = {"to": a.to, "body": body}
    if channel == "email":
        draft.update(cc=a.cc, bcc=a.bcc, subject=a.subject)
        if a.account: draft["account_hint"] = a.account
        if a.attach:
            draft["attachments"] = [{"name": os.path.basename(p), "mime": mimetypes.guess_type(p)[0] or "application/octet-stream",
                                     "data_b64": base64.b64encode(open(p, "rb").read()).decode()} for p in a.attach]
    r = call("POST", "/drafts", {"device": a.device, "channel": channel, "draft": draft, "origin": "victorlinux:" + (os.environ.get("USER") or "owner")})
    print("Draft handed to the phone for review. Nothing is sent until its owner taps Send there.", file=sys.stderr)
    show(r)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="victor-fleet", description="VictorLinux fleet coordinator control")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("serve", help="run the coordinator in the foreground")
    sp.add_parser("start", help="start the coordinator in the background as the current (unprivileged) user")
    sp.add_parser("stop", help="stop the coordinator (SIGTERM; in-flight state is already durable)")
    sp.add_parser("status")
    lg = sp.add_parser("logs"); lg.add_argument("-n", type=int, default=100); lg.add_argument("--file", action="store_true", help="tail the process log instead of the audit events")
    pr = sp.add_parser("pair", help="create a single-use pairing payload"); pr.add_argument("--ttl", type=int, default=600); pr.add_argument("--label")
    sp.add_parser("devices")
    for n in ("pause", "resume", "enable", "disable", "revoke", "unpair"):
        x = sp.add_parser(n); x.add_argument("device")
    cf = sp.add_parser("config", help="show or change coordinator settings (restart to apply)")
    cf.add_argument("--enable-network", metavar="ADDR", help="serve on this explicit LAN/VPN address instead of loopback")
    cf.add_argument("--disable-network", action="store_true"); cf.add_argument("--port", type=int)
    cf.add_argument("--plan-approval", choices=["on", "off"])
    sb = sp.add_parser("submit", help="submit a plan (JSON file or - for stdin)"); sb.add_argument("file"); sb.add_argument("--approve", action="store_true")
    pl = sp.add_parser("plans"); rv = sp.add_parser("review"); rv.add_argument("plan")
    ap_ = sp.add_parser("approve"); ap_.add_argument("plan")
    jb = sp.add_parser("jobs"); jb.add_argument("--plan")
    cn = sp.add_parser("cancel"); cn.add_argument("target", help="job id or plan id")
    rp = sp.add_parser("report"); rp.add_argument("plan")
    wf = sp.add_parser("workflow", help="build and submit a built-in workflow")
    wsp = wf.add_subparsers(dest="wf", required=True)
    bh = wsp.add_parser("batch-hash"); bh.add_argument("--root", required=True); bh.add_argument("--shards", type=int, default=4); bh.add_argument("--device", action="append")
    ak = wsp.add_parser("apk-test"); ak.add_argument("--root", required=True); ak.add_argument("--apk", required=True); ak.add_argument("--package", required=True); ak.add_argument("--device", required=True)
    hd = wsp.add_parser("handoff"); hd.add_argument("--op-a", required=True); hd.add_argument("--op-b", required=True)
    hd.add_argument("--args-a", default="{}"); hd.add_argument("--args-b", default="{}"); hd.add_argument("--device-a"); hd.add_argument("--device-b")
    tm = sp.add_parser("terminal", help="open a terminal on a paired phone (VictorSuite on the phone owns the shell; its owner must approve)")
    tm.add_argument("device"); tm.add_argument("--privileged", action="store_true", help="request a root shell (needs the phone's separate privileged grant)")
    ex = sp.add_parser("exec", help="run ONE command on a paired phone through VictorSuite and print its real output and exit status")
    ex.add_argument("device"); ex.add_argument("--privileged", action="store_true"); ex.add_argument("--shell", help="command line as one string"); ex.add_argument("argv", nargs="*", help="command and arguments (after --)")
    ts = sp.add_parser("terminals", help="list terminal sessions"); tc = sp.add_parser("terminal-close"); tc.add_argument("session")
    ms = sp.add_parser("message", help="prepare an SMS/email draft and hand it to VictorSMS on a phone for the owner to review")
    msp = ms.add_subparsers(dest="mcmd", required=True)
    for ch in ("sms", "email"):
        d = msp.add_parser("draft-" + ch); d.add_argument("device"); d.add_argument("--to", action="append", required=True)
        d.add_argument("--body"); d.add_argument("--body-file")
        if ch == "email":
            d.add_argument("--cc", action="append", default=[]); d.add_argument("--bcc", action="append", default=[]); d.add_argument("--subject", default="")
            d.add_argument("--account", help="sending account hint; the phone's owner picks the identity"); d.add_argument("--attach", action="append", default=[])
    mst = msp.add_parser("status"); mst.add_argument("draft"); msp.add_parser("list"); mre = msp.add_parser("retry"); mre.add_argument("draft")
    a = ap.parse_args(argv)

    if a.cmd == "serve": return serve(a)
    if a.cmd == "start": return start_bg()
    if a.cmd == "stop": return stop_bg()
    if a.cmd == "status":
        if not running_pid() and not os.path.exists(sock_path()):
            show({"running": False, "persistence": persistence.resolve()}); return
        show(call("GET", "/status"))
    elif a.cmd == "logs" and a.file:
        try: print("".join(open(os.path.join(data_dir(), "coordinator.log"), errors="replace").readlines()[-a.n:]), end="")
        except OSError: print("no process log yet")
    elif a.cmd == "logs": show(call("GET", f"/events?n={a.n}"))
    elif a.cmd == "pair": show(call("POST", "/pair", {"ttl_s": a.ttl, "label": a.label}))
    elif a.cmd == "devices":
        rows = call("GET", "/status")["devices"]
        show(rows) if not os.environ.get("VICTOR_FLEET_BRIEF") else print_features(rows)
    elif a.cmd == "terminals": show(call("GET", "/terminal"))
    elif a.cmd == "terminal-close": show(call("POST", f"/terminal/{a.session}/close"))
    elif a.cmd == "terminal": sys.exit(cmd_terminal(a))
    elif a.cmd == "exec": sys.exit(cmd_exec(a))
    elif a.cmd == "message": cmd_message(a)
    elif a.cmd in ("pause", "resume", "enable", "disable", "revoke", "unpair"): show(call("POST", f"/devices/{a.device}/{a.cmd}"))
    elif a.cmd == "config":
        from .server import load_config, save_config
        d = os.environ.get("VICTOR_FLEET_DIR") or persistence.resolve()["path"]
        c = load_config(d)
        if a.enable_network: c.update(network_enabled=True, bind=a.enable_network)
        if a.disable_network: c.update(network_enabled=False)
        if a.port: c["port"] = a.port
        if a.plan_approval: c["plan_approval"] = a.plan_approval == "on"
        if a.enable_network or a.disable_network or a.port or a.plan_approval: save_config(d, c)
        show(c)
    elif a.cmd == "submit":
        spec = json.load(sys.stdin if a.file == "-" else open(a.file)); show(call("POST", "/plans", {"spec": spec, "auto_approve": a.approve or None}))
    elif a.cmd == "plans": show(call("GET", "/plans"))
    elif a.cmd == "review": show(call("GET", f"/plans/{a.plan}/review"))
    elif a.cmd == "approve": show(call("POST", f"/plans/{a.plan}/approve"))
    elif a.cmd == "jobs": show(call("GET", "/jobs" + (f"?plan={a.plan}" if a.plan else "")))
    elif a.cmd == "cancel": show(call("POST", f"/plans/{a.target}/cancel") if a.target.startswith("plan-") and "." not in a.target else call("POST", f"/jobs/{a.target}/cancel"))
    elif a.cmd == "report": show(call("GET", f"/plans/{a.plan}/report"))
    elif a.cmd == "workflow":
        from . import workflows
        if a.wf == "batch-hash":
            root = os.path.realpath(a.root)
            files = sorted(os.path.relpath(os.path.join(dp, f), root) for dp, _, fs in os.walk(root) for f in fs if os.path.isfile(os.path.join(dp, f)) and not os.path.islink(os.path.join(dp, f)))
            spec = workflows.batch_hash_plan(root, files, a.shards, a.device)
        elif a.wf == "apk-test":
            devs = {d["id"]: d for d in call("GET", "/status")["devices"]}
            if a.device not in devs: print("ERROR: unknown device", file=sys.stderr); sys.exit(2)
            caps = dict(devs[a.device]["capabilities"], _device_id=a.device)
            try: spec = workflows.apk_test_plan(os.path.realpath(a.root), a.apk, a.package, caps)
            except ValueError as e: print(f"ERROR: {e}", file=sys.stderr); sys.exit(2)
        else:
            spec = workflows.handoff_plan(a.op_a, json.loads(a.args_a), a.op_b, json.loads(a.args_b), device_a=a.device_a, device_b=a.device_b)
        show(call("POST", "/plans", {"spec": spec}))


if __name__ == "__main__":
    main()
