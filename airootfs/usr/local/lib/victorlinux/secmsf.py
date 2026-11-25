"""Real Metasploit Framework RPC client for the VictorLinux security backend.

Uses the documented MessagePack RPC interface
(https://docs.metasploit.com/docs/using-metasploit/advanced/RPC/how-to-use-metasploit-messagepack-rpc.html),
not the resource-script/`msfconsole -r` path: RPC gives structured auth, real job
tracking (`job.list`/`job.info`/`job.stop`) and a real cancellation primitive, none of
which a one-shot `msfconsole -r script.rc` process has. The resource-script path stays
available in DroidCommand AI's own Kotlin `ShellBackedMetasploitExecutor` as the documented
desktop/CLI fallback (see that module's doc comment) when no `msfrpcd` is reachable.

Verified live 2026-11-19 against a real `msfrpcd` (Metasploit Framework 6.5.12-dev, built
from a pinned rapid7/metasploit-framework checkout, see docs/security-backend.md#metasploit-coverage
for the exact commands and the full console transcript this was checked against):
`auth.login`, `core.version`, `console.create`/`console.write`/`console.read`/
`console.destroy`, and a real `auxiliary/scanner/portscan/tcp` run against 127.0.0.1 with
real captured output. `module.execute`'s own `job.info` was also exercised and found to
raise a structured RPC error ("Invalid Job", error_code 500) once a fast job has already
finished and been reaped - mapped below to ERR_UNKNOWN_JOB rather than treated as a
transport failure, since "the job already finished" is not the same fact as "the request
failed to reach Metasploit".
"""
import http.client
import ssl
import time

try:
    import msgpack
except ImportError:  # pragma: no cover - exercised by test_secmsf.py's ImportError path
    msgpack = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55553


class MsfRpcError(Exception):
    """Raised for both transport failures (connect/timeout) and structured RPC error
    responses (`{"error": true, ...}`). `code` distinguishes which: see ERR_* constants."""

    def __init__(self, code, message, rpc_error=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rpc_error = rpc_error


ERR_TRANSPORT = "transport_error"
ERR_AUTH = "auth_failed"
ERR_UNKNOWN_JOB = "unknown_job"
ERR_RPC = "rpc_error"


def _unwrap_keys(d):
    """msgpack with raw=False still returns dict keys as bytes for RPC hash responses in
    some msfrpcd versions (observed live); normalize to str keys once, here, so every
    caller below can use plain string keys."""
    if not isinstance(d, dict):
        return d
    return {(k.decode() if isinstance(k, bytes) else k): v for k, v in d.items()}


class MetasploitRpcClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, ssl_context=None, timeout=20):
        if msgpack is None:
            raise MsfRpcError(ERR_TRANSPORT, "the 'msgpack' package is not installed")
        self.host, self.port, self.timeout = host, port, timeout
        self.ssl_context = ssl_context  # None = plain HTTP, matches msfrpcd's loopback-only default
        self._token = None

    # -- transport -------------------------------------------------------
    def _call(self, method, *args):
        body = msgpack.packb([method] + list(args), use_bin_type=True)
        try:
            if self.ssl_context is not None:
                conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ssl_context)
            else:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.request("POST", "/api/", body=body, headers={"Content-Type": "binary/message-pack"})
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            raise MsfRpcError(ERR_TRANSPORT, f"could not reach msfrpcd at {self.host}:{self.port}: {e}") from e
        # msfrpcd answers a structured RPC-level error (job/console not found, bad method,
        # ...) with a real msgpack body describing it, but ALSO sets the HTTP status to
        # the error's own code (observed live: HTTP 500 carrying a well-formed
        # {"error": true, "error_message": "Invalid Job", ...} body). Checking resp.status
        # before trying to parse the body would misreport that as a bare transport failure
        # and throw away the structured reason - so the body is always parsed first, and
        # only an unparseable body on a non-200 status becomes ERR_TRANSPORT.
        try:
            result = msgpack.unpackb(data, raw=False)
        except Exception as e:
            if resp.status != 200:
                raise MsfRpcError(ERR_TRANSPORT, f"msfrpcd returned HTTP {resp.status} with an unparseable body") from e
            raise MsfRpcError(ERR_TRANSPORT, f"malformed msgpack response: {e}") from e
        result = _unwrap_keys(result)
        if isinstance(result, dict) and result.get("error"):
            msg = result.get("error_message") or result.get("error_string") or "unknown RPC error"
            if isinstance(msg, bytes):
                msg = msg.decode(errors="replace")
            code = ERR_UNKNOWN_JOB if "Invalid Job" in str(msg) or "Invalid Console" in str(msg) else ERR_RPC
            raise MsfRpcError(code, msg, rpc_error=result)
        if resp.status != 200:
            # A non-200 with a parseable body that was NOT a structured RPC error (e.g. a
            # reverse proxy/HTTP-layer failure in front of msfrpcd) - a real transport
            # problem, not a tool-level one.
            raise MsfRpcError(ERR_TRANSPORT, f"msfrpcd returned HTTP {resp.status}")
        return result

    # -- auth --------------------------------------------------------------
    def login(self, user, password):
        result = self._call("auth.login", user, password)
        token = result.get("token")
        if isinstance(token, bytes):
            token = token.decode()
        if not token or result.get("result") not in ("success", b"success"):
            raise MsfRpcError(ERR_AUTH, "auth.login did not return a token")
        self._token = token
        return token

    def logout(self):
        if self._token:
            try:
                self._call("auth.logout", self._token, self._token)
            finally:
                self._token = None

    def _tok(self):
        if not self._token:
            raise MsfRpcError(ERR_AUTH, "not logged in")
        return self._token

    # -- version / capability -------------------------------------------
    def version(self):
        v = self._call("core.version", self._tok())
        return {k: (val.decode() if isinstance(val, bytes) else val) for k, val in v.items()}

    # -- console-based module execution ----------------------------------
    def console_create(self):
        c = self._call("console.create", self._tok())
        cid = c.get("id")
        return cid.decode() if isinstance(cid, bytes) else cid

    def console_write(self, cid, line):
        if "\n" in line.rstrip("\n") or "\r" in line:      # one call = one console command (defence in depth)
            raise ValueError("refusing a console line with an embedded line break")
        self._call("console.write", self._tok(), cid, line if line.endswith("\n") else line + "\n")

    def console_read(self, cid):
        r = self._call("console.read", self._tok(), cid)
        data = r.get("data", b"")
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        return data, bool(r.get("busy"))

    def console_destroy(self, cid):
        try:
            self._call("console.destroy", self._tok(), cid)
        except MsfRpcError:
            pass  # best-effort: the console may already be gone (restart/timeout)

    def run_module_via_console(self, module_type, module_path, options, on_output=None,
                                is_cancelled=None, completion_markers=None, poll_interval=1.0,
                                timeout_s=180):
        """Runs one module through a real msfconsole session over RPC and returns the full
        captured text output. This is the one real request-to-real-result path this backend
        demonstrates against a live msfrpcd (see this module's own docstring for the exact
        verification run). Cancellation sends Ctrl-C into the console then destroys it -
        confirmed live to stop module execution, which the caller should still treat as
        best-effort (see secbackend.py's cancel handler for how "confirmed stopped" vs
        "outcome unknown" is reported back to the client).
        """
        if module_type not in ("exploit", "auxiliary", "post"):
            raise ValueError(f"unsupported module_type {module_type!r}")
        markers = completion_markers or (
            "Auxiliary module execution completed",
            "Exploit completed",
            "Post module execution completed",
        )
        cid = self.console_create()
        output = []
        try:
            self.console_read(cid)  # drain the banner
            self.console_write(cid, f"use {module_type}/{module_path}")
            for name, value in options.items():
                self.console_write(cid, f"set {name} {value}")
            self.console_write(cid, "run" if module_type != "exploit" else "exploit")

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if is_cancelled and is_cancelled():
                    self.console_write(cid, "\x03")  # Ctrl-C: interrupt the running module
                    time.sleep(0.5)
                    chunk, _ = self.console_read(cid)
                    if chunk:
                        output.append(chunk)
                    raise MsfRpcError(ERR_RPC, "job cancelled")
                chunk, busy = self.console_read(cid)
                if chunk:
                    output.append(chunk)
                    if on_output:
                        on_output(chunk)
                joined = "".join(output)
                if not busy and any(m in joined for m in markers):
                    break
                time.sleep(poll_interval)
            else:
                raise MsfRpcError(ERR_RPC, f"module did not complete within {timeout_s}s")
        finally:
            self.console_destroy(cid)
        return "".join(output)
