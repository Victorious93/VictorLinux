#!/usr/bin/env python3
"""VictorLinux security-tool execution backend (SET / Metasploit).

A SEPARATE authenticated HTTP service from `gateway.py` (inference) and `control_center.py`
(the local-only admin UI) - see `secprotocol.py`'s module docstring for why. Like both of
those, it is opt-in: nothing starts it at boot (no systemd unit ships for it), it is started
the same way `victor-control-center` starts `control_center.py` - by running
`victor-secbackend serve` as the current unprivileged user. Binds to 127.0.0.1 by default;
reaching it from the LAN/a VPN needs an explicit `--bind` plus the owner's own
reverse-proxy/VPN setup, exactly like `gateway.py`'s `remote` setting, and still requires an
API key either way (`security.require_auth` is never false here, unlike the inference
gateway, which only requires auth once it stops being loopback-only - running a real
exploit/SET module is a materially different risk than serving a chat completion).

API keys for this service are stored in a SEPARATE secrets namespace
(`security_api_keys`/`security_api_key_meta`) from the inference gateway's `api_keys`, so a
chat/model API key never grants tool execution and vice versa (see secprotocol.py and
docs/security-backend.md#access-control`).
"""
import argparse
import http.client
import http.server
import json
import os
import socketserver
import ssl
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apikeys  # noqa: E402
import secjobs  # noqa: E402
import secmsf  # noqa: E402
import secprotocol as proto  # noqa: E402
import secset  # noqa: E402
import victorcommon as vc  # noqa: E402

SECRETS_KEY_LIST = "security_api_keys"
SECRETS_KEY_META = "security_api_key_meta"
DEFAULT_PORT = 18099


def _secrets():
    sec = vc.load_secrets()
    sec.setdefault(SECRETS_KEY_LIST, [])
    sec.setdefault(SECRETS_KEY_META, {})
    return sec


def _check_key(sec, presented):
    """Same hashing/compare-digest discipline as apikeys.check, against the SEPARATE
    security namespace so apikeys.check (gateway's own `api_keys` list) is never reused."""
    if not presented:
        return None, None, "missing"
    shim = {"api_keys": sec.get(SECRETS_KEY_LIST, []), "api_key_meta": sec.get(SECRETS_KEY_META, {})}
    h, meta, why = apikeys.check(shim, presented)
    return h, meta, why


def create_security_key(name, expires_days=0, rpm=0):
    """Mint a key scoped to tool execution only. Used by `victor-secbackend create-key`
    and (if wired in later) a Control Center panel - never by anything that also hands out
    inference keys."""
    sec = _secrets()
    shim = {"api_keys": sec[SECRETS_KEY_LIST], "api_key_meta": sec[SECRETS_KEY_META]}
    key, pub = apikeys.create(shim, name, expires_days, rpm)
    sec[SECRETS_KEY_LIST], sec[SECRETS_KEY_META] = shim["api_keys"], shim["api_key_meta"]
    vc.save_secrets(sec)
    return key, pub


class Executors:
    """Wires secjobs.JobStore to the real secmsf/secset modules. A single instance per
    running backend process; held as a module-level default so tests can substitute fakes."""

    def __init__(self, msf_host="127.0.0.1", msf_port=secmsf.DEFAULT_PORT, msf_user=None, msf_pass=None):
        self.msf_host, self.msf_port = msf_host, msf_port
        self.msf_user, self.msf_pass = msf_user, msf_pass

    def capabilities(self):
        caps = {
            "protocol": {"major": proto.PROTOCOL_MAJOR, "minor": proto.PROTOCOL_MINOR},
            "operations": {},
        }
        msf_available, msf_reason, msf_version = False, "not configured", None
        if self.msf_user and self.msf_pass:
            try:
                c = secmsf.MetasploitRpcClient(self.msf_host, self.msf_port, timeout=5)
                c.login(self.msf_user, self.msf_pass)
                msf_version = c.version()
                c.logout()
                msf_available, msf_reason = True, None
            except secmsf.MsfRpcError as e:
                msf_reason = f"{e.code}: {e.message}"
        caps["operations"]["metasploit.module"] = {
            "available": msf_available,
            "reason": msf_reason,
            "version": msf_version,
        }
        set_on_path = secset.is_available()
        caps["operations"]["setoolkit.attack"] = {
            "available": set_on_path,
            "reason": None if set_on_path else f"'{secset.SETOOLKIT_BIN}' is not on PATH (or pexpect is missing)",
            "supported_vectors": sorted(secset.SUPPORTED_VECTORS),
            "unsupported_vectors": {
                k: v for k, v in proto.UNSUPPORTED_SET_VECTOR_REASONS.items()
            },
        }
        return caps

    def run(self, job):
        """Runs synchronously in the job's worker thread; secjobs.JobStore wraps this call
        with state transitions and persistence. Raises proto.ProtocolError on any failure -
        never returns a success result for a tool that did not actually finish."""
        is_cancelled = job.cancel_event.is_set
        if job.op == "metasploit.module":
            return self._run_metasploit(job, is_cancelled)
        if job.op == "setoolkit.attack":
            return self._run_setoolkit(job, is_cancelled)
        raise proto.ProtocolError("unknown_op", f"no executor for op '{job.op}'")  # unreachable: validated earlier

    def _run_metasploit(self, job, is_cancelled):
        if not (self.msf_user and self.msf_pass):
            raise proto.ProtocolError("capability_missing", "Metasploit RPC credentials are not configured")
        p = job.params
        c = secmsf.MetasploitRpcClient(self.msf_host, self.msf_port, timeout=job.timeout_ms / 1000 + 10)
        try:
            c.login(self.msf_user, self.msf_pass)
        except secmsf.MsfRpcError as e:
            raise proto.ProtocolError("executor_unavailable", f"could not reach msfrpcd: {e.message}") from e
        try:
            options = dict(p.get("options") or {})
            options["RHOST" if p["module_type"] == "exploit" else "RHOSTS"] = p["target_host"]
            if p.get("target_port"):
                options.setdefault("RPORT", str(p["target_port"]))
            if p.get("payload"):
                options["PAYLOAD"] = p["payload"]
            output = c.run_module_via_console(
                p["module_type"], p["module_path"], options,
                on_output=job.append_output, is_cancelled=is_cancelled,
                timeout_s=job.timeout_ms / 1000,
            )
        except secmsf.MsfRpcError as e:
            if "cancelled" in e.message:
                raise proto.ProtocolError("cancelled", "job cancelled") from e
            raise proto.ProtocolError("internal", f"metasploit module error: {e.message}") from e
        finally:
            c.logout()
        return {"summary": "module completed", "exit_evidence": "completion marker observed in console output"}

    def _run_setoolkit(self, job, is_cancelled):
        p = job.params
        if p["attack_vector"] != "QRCODE_GENERATOR":
            raise proto.ProtocolError("capability_missing", "only QRCODE_GENERATOR is implemented so far")
        try:
            png_path = secset.run_qrcode_generator(
                p.get("target_host") or p.get("target_email") or "",
                on_output=job.append_output, is_cancelled=is_cancelled,
                timeout_s=job.timeout_ms / 1000,
            )
        except secset.SetAdapterError as e:
            if e.code == "cancelled":
                raise proto.ProtocolError("cancelled", e.message) from e
            raise proto.ProtocolError(e.code if e.code in proto.ERROR_CODES else "internal", e.message) from e
        return {"summary": "QR code generated", "artifact_path": png_path}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VictorSecBackend"
    store = None  # set in main()

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s %s\n" % (vc.now(), self.client_address[0], fmt % a))

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        kh, meta, why = _check_key(_secrets(), tok)
        if why:
            return None, why
        return apikeys.key_id(kh), None

    def _read_json(self):
        raw_n = (self.headers.get("Content-Length") or "0").strip()
        if not raw_n.isascii() or not raw_n.isdigit():
            raise proto.ProtocolError("bad_request", "bad Content-Length")
        n = int(raw_n)
        if n > 1 << 20:
            raise proto.ProtocolError("bad_request", "request body too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError as e:
            raise proto.ProtocolError("bad_request", f"invalid JSON: {e}") from e

    def _route(self):
        path = self.path.split("?", 1)[0]
        query = {}
        if "?" in self.path:
            from urllib.parse import parse_qs
            query = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

        if path == "/security/v1/health":
            return self._json(200, {"status": "ok"})

        key_id, why = self._authed()
        if why:
            return self._json(401, {"error": {"code": "unauthorized",
                                               "message": "Missing or invalid security API key"}})

        try:
            if path == "/security/v1/capabilities" and self.command == "GET":
                return self._json(200, self.executors.capabilities())
            if path == "/security/v1/jobs" and self.command == "POST":
                job = self.store.submit(key_id, self._read_json())
                return self._json(202, job.to_public_dict(output_cursor=0))
            if path == "/security/v1/jobs" and self.command == "GET":
                return self._json(200, {"jobs": [j.to_public_dict() for j in self.store.list_jobs(key_id)]})
            if path.startswith("/security/v1/jobs/") and path.endswith("/cancel") and self.command == "POST":
                job_id = path[len("/security/v1/jobs/"):-len("/cancel")]
                job = self.store.cancel(key_id, job_id)
                return self._json(202, job.to_public_dict())
            if path.startswith("/security/v1/jobs/") and self.command == "GET":
                job_id = path[len("/security/v1/jobs/"):]
                since = int(query.get("since", "0"))
                job = self.store.get(key_id, job_id)
                return self._json(200, job.to_public_dict(output_cursor=since))
            return self._json(404, {"error": {"code": "unknown_op", "message": "no such route"}})
        except proto.ProtocolError as e:
            return self._json(e.http_status, e.to_dict())

    do_GET = do_POST = _route


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _cli_request(method, path, key, body=None, bind="127.0.0.1", port=DEFAULT_PORT):
    if not key:
        raise SystemExit("No API key given. Pass --key or set VICTOR_SECURITY_KEY "
                          "(create one with: victor-secbackend create-key <name>).")
    conn = http.client.HTTPConnection(bind, port, timeout=200)
    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        out = json.loads(resp.read())
    except OSError as e:
        raise SystemExit(f"Could not reach the security backend on {bind}:{port}: {e}\n"
                          f"Is it running? Start it with: victor-secbackend serve") from e
    finally:
        conn.close()
    return resp.status, out


def _cli_client(args):
    bind = getattr(args, "bind", "127.0.0.1")
    port = getattr(args, "port", DEFAULT_PORT)
    if args.cmd == "capabilities":
        status, out = _cli_request("GET", "/security/v1/capabilities", args.key, bind=bind, port=port)
    elif args.cmd == "list":
        status, out = _cli_request("GET", "/security/v1/jobs", args.key, bind=bind, port=port)
    elif args.cmd == "status":
        status, out = _cli_request("GET", f"/security/v1/jobs/{args.job_id}?since={args.since}",
                                    args.key, bind=bind, port=port)
    elif args.cmd == "cancel":
        status, out = _cli_request("POST", f"/security/v1/jobs/{args.job_id}/cancel", args.key, bind=bind, port=port)
    elif args.cmd == "submit":
        try:
            params = json.loads(args.params_json)
        except ValueError as e:
            raise SystemExit(f"params_json is not valid JSON: {e}") from e
        body = {
            "op": args.op, "params": params, "timeout_ms": args.timeout_ms,
            "idempotency_key": args.idempotency_key or f"cli-{uuid.uuid4().hex}",
        }
        status, out = _cli_request("POST", "/security/v1/jobs", args.key, body=body, bind=bind, port=port)
    else:  # pragma: no cover - unreachable, argparse restricts cmd to the registered subcommands
        raise SystemExit(f"unknown command {args.cmd!r}")
    print(json.dumps(out, indent=2))
    if status >= 400:
        sys.exit(1)


def main(argv=None):
    # --bind/--port/--msf-* are accepted BOTH before and after the subcommand (e.g. both
    # `secbackend.py --port 9 serve` and `secbackend.py serve --port 9` work) via a shared
    # parent parser, since argparse would otherwise reject them after a subparser's own
    # positional arguments and that is not how anyone actually types a CLI.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bind", default="127.0.0.1")
    common.add_argument("--port", type=int, default=DEFAULT_PORT)
    common.add_argument("--msf-host", default="127.0.0.1")
    common.add_argument("--msf-port", type=int, default=secmsf.DEFAULT_PORT)

    ap = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", parents=[common])
    ck = sub.add_parser("create-key", parents=[common])
    ck.add_argument("name")
    ck.add_argument("--expires-days", type=int, default=0)
    ck.add_argument("--rpm", type=int, default=0)

    # -- local diagnostic/operator CLI client: talks to a running `serve` instance over
    # loopback HTTP, so the Linux backend can be driven and diagnosed without Android. --
    cap = sub.add_parser("capabilities", parents=[common])
    cap.add_argument("--key", default=os.environ.get("VICTOR_SECURITY_KEY"))
    sub_submit = sub.add_parser("submit", parents=[common])
    sub_submit.add_argument("--key", default=os.environ.get("VICTOR_SECURITY_KEY"))
    sub_submit.add_argument("op")
    sub_submit.add_argument("params_json")
    sub_submit.add_argument("--idempotency-key", default=None)
    sub_submit.add_argument("--timeout-ms", type=int, default=180_000)
    sub_status = sub.add_parser("status", parents=[common])
    sub_status.add_argument("--key", default=os.environ.get("VICTOR_SECURITY_KEY"))
    sub_status.add_argument("job_id")
    sub_status.add_argument("--since", type=int, default=0)
    sub_cancel = sub.add_parser("cancel", parents=[common])
    sub_cancel.add_argument("--key", default=os.environ.get("VICTOR_SECURITY_KEY"))
    sub_cancel.add_argument("job_id")
    sub_list = sub.add_parser("list", parents=[common])
    sub_list.add_argument("--key", default=os.environ.get("VICTOR_SECURITY_KEY"))

    args = ap.parse_args(argv)

    if args.cmd == "create-key":
        key, pub = create_security_key(args.name, args.expires_days, args.rpm)
        print(json.dumps({"api_key": key, **pub}, indent=2))
        print("Store this key now; it is not shown again.", file=sys.stderr)
        return

    if args.cmd in ("capabilities", "submit", "status", "cancel", "list"):
        return _cli_client(args)

    sec = vc.load_secrets()
    if not sec.get(SECRETS_KEY_LIST):
        raise SystemExit("No security API keys exist yet. Run: victor-secbackend create-key <name>")
    msf_user = sec.get("security_msf_rpc_user")
    msf_pass = sec.get("security_msf_rpc_pass")

    executors = Executors(args.msf_host, args.msf_port, msf_user, msf_pass)
    Handler.executors = executors
    Handler.store = secjobs.JobStore(executors.run)

    srv = Server((args.bind, args.port), Handler)
    sys.stderr.write(f"{vc.now()} security backend listening on {args.bind}:{args.port} "
                     f"(auth required; metasploit RPC {'configured' if msf_user else 'NOT configured'})\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
