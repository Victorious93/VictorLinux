#!/usr/bin/env python3
"""VictorLinux inference gateway.

Fronts the local llama-server (which only ever listens on 127.0.0.1 with a
private key) and adds: bearer-token authentication, optional TLS, a health
endpoint and an honest capability report. It exposes inference ONLY - there is
no tool execution, file access or shell on this port.
"""
import argparse
import http.client
import http.server
import json
import os
import socketserver
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import victorcommon as tc  # noqa: E402
import companion  # noqa: E402
import apikeys  # noqa: E402

OLLAMA_ROUTES = ("/api/version", "/api/tags", "/api/generate", "/api/chat")
ALLOWED_PREFIXES = ("/v1/", "/completion", "/completions", "/chat/completions", "/models", "/props",
                    "/tokenize", "/detokenize", "/slots", "/health", "/metrics")
# llama.cpp web UI static assets are served for GET / only when the UI is enabled
UI_ASSET_PREFIXES = ("/assets/", "/_app/", "./assets/")
UI_ASSET_PATHS = ("/favicon.ico", "/favicon.svg", "/manifest.json", "/manifest.webmanifest", "/robots.txt")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "authorization"}


SWAP_WAIT_S = 180            # hold API requests this long while the Control Center switches models


def swap_state():
    """The Control Center writes RUN_DIR/swap.json while it switches models and removes it when the new model answers
    (or the switch failed and the old one is back). Older than 15 minutes = stale, ignored."""
    try:
        with open(os.path.join(tc.RUN_DIR, "swap.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and time.time() - float(st.get("since", 0)) < 900 else None


class State:
    last_request = time.time()
    lock = threading.Lock()
    sec_lock = threading.Lock()
    sec_cache = {"mtime": None, "sec": {}}
    limiter = apikeys.RateLimiter()
    usage = None


def current_secrets():
    """Re-read secrets.json when it changes, so new, revoked and expired keys apply without a restart."""
    p = tc.secrets_path()
    try:
        m = os.stat(p).st_mtime_ns if p else None
    except OSError:
        m = None
    with State.sec_lock:
        if m != State.sec_cache["mtime"]:
            State.sec_cache = {"mtime": m, "sec": tc.load_secrets()}
        return State.sec_cache["sec"]


def capabilities(model_path=None):
    """Report only what a recorded real test proved for the CURRENT model."""
    caps = {
        "openai_compatible": True,
        "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/completions", "/v1/embeddings (if model supports it)"],
        "ollama_compat": ["/api/version", "/api/tags", "/api/generate", "/api/chat"],
        "llamacpp_native": ["/completion", "/props", "/tokenize", "/detokenize"],
        "streaming": "unverified", "tool_calling": "unverified",
        "tool_execution_on_pc": False,
        "note": "Remote inference does not grant permission to execute tools on this PC. "
                "Tool calling is advertised only after `victor-capcheck` passes for the loaded model.",
    }
    p = tc.data_path("server", "capabilities.json")
    if p and os.path.exists(p) and model_path:
        try:
            with open(p) as f:
                rec = json.load(f)
            st = os.stat(model_path)
            if rec.get("model_path") == model_path and rec.get("model_size") == st.st_size \
                    and rec.get("model_mtime") == int(st.st_mtime):
                caps["streaming"] = "verified" if rec.get("streaming") else "failed"
                caps["tool_calling"] = "verified" if rec.get("tool_calling") else "failed-or-unsupported"
                caps["verified_at"] = rec.get("tested_at")
                caps["verified_server"] = rec.get("server_version")
            else:
                caps["note"] += " A previous test exists but was for a different model file."
        except (OSError, ValueError):
            pass
    return caps


def tls_per_connection(server, ctx, handshake_timeout=15):
    """Serve TLS from a plain listening socket, doing each handshake in the connection's own thread with a timeout.
    Wrapping the LISTENING socket (`server.socket = ctx.wrap_socket(...)`) makes accept() perform the handshake, so one
    client that connects and sends nothing stalls the whole service for everyone (reproduced). Not used by plain HTTP."""
    def get_request():
        conn, addr = server.socket.accept()
        conn.settimeout(handshake_timeout)
        return conn, addr

    orig_finish = server.finish_request

    def finish_request(request, client_address):
        try:
            tls = ctx.wrap_socket(request, server_side=True)
        except (OSError, ValueError):          # ssl.SSLError and socket.timeout are OSErrors; scanners are routine
            return
        try:
            tls.settimeout(None)               # handler classes set their own read timeout in setup()
            orig_finish(tls, client_address)
        finally:
            try:
                tls.close()
            except OSError:
                pass

    orig_error = server.handle_error

    def handle_error(request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], OSError):     # resets, TLS EOF and read timeouts of scanners/phones are routine
            return
        orig_error(request, client_address)

    server.get_request = get_request
    server.finish_request = finish_request
    server.handle_error = handle_error
    return server


def content_length(headers, limit=None):
    """Content-Length as a non-negative int. Raises ValueError on garbage/negative (rfile.read(-1) would hang the thread)
    and on a body larger than `limit`."""
    raw = (headers.get("Content-Length") or "0").strip()
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("bad Content-Length")
    n = int(raw)
    if limit is not None and n > limit:
        raise ValueError("request body too large")
    return n


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VictorGateway"
    timeout = 120        # idle/stalled clients are dropped; model replies are streamed OUT, never waited for on this socket
    cfg = None

    def log_message(self, fmt, *a):   # keep logs free of tokens/URLs with queries
        sys.stderr.write("%s %s %s\n" % (tc.now(), self.client_address[0], fmt % a))

    # -- helpers
    def _json(self, code, obj, extra=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def _authed(self):
        """-> None when allowed, else a reason: missing | invalid | expired | rate. Keys are stored as sha256 hashes."""
        self._key = None
        cfg = self.cfg
        if not cfg["require_auth"]:
            return None
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        kh, meta, why = apikeys.check(current_secrets(), tok)
        if why:
            return why
        self._key = (apikeys.key_id(kh), meta.get("name"))
        if not State.limiter.allow(self._key[0], meta.get("rpm") or 0):
            return "rate"
        return None

    def _pair(self):
        """Redeem a single-use pairing token for a named key. Opt-in: only works while the owner's pairing window is open."""
        if self.command != "POST":
            return self._json(405, {"v": 1, "ok": False, "err": "bad_request"}, {"Allow": "POST"})
        if not self.cfg["tls"] and self.cfg["bind"] not in ("127.0.0.1", "::1", "localhost"):
            return self._json(403, {"v": 1, "ok": False, "err": "tls_required"})
        try:
            n = content_length(self.headers, 4096)
        except ValueError:
            n = 0
        if not 0 < n <= 4096:
            return self._json(400, {"v": 1, "ok": False, "err": "bad_request"})
        try:
            b = json.loads(self.rfile.read(n))
            assert isinstance(b, dict) and b.get("v") == 1
        except (ValueError, AssertionError):
            return self._json(400, {"v": 1, "ok": False, "err": "bad_request"})
        with State.sec_lock:
            sec = tc.load_secrets()
            try:
                key, pub = apikeys.redeem(sec, b.get("t"), b.get("device"))
                ok = True
            except apikeys.PairError as e:
                ok, err = False, str(e)
            tc.save_secrets(sec)                         # consumed token / attempt counter is persisted either way
            State.sec_cache["mtime"] = None
        if State.usage:
            State.usage.add(pub["id"] if ok else "-", pub["name"] if ok else "pairing", "POST", "/victor/pair", 200 if ok else 403, self.client_address[0])
        if not ok:
            return self._json(403, {"v": 1, "ok": False, "err": err})
        return self._json(200, {"v": 1, "ok": True, "api_key": key, "key_id": pub["id"], "name": pub["name"],
                                "note": "Store this key in the phone's keystore. It is not shown again."})

    def _route(self):
        self._status, self._key = 0, None
        try:
            return self._route_inner()
        finally:
            if self._key and State.usage:
                State.usage.add(self._key[0], self._key[1], self.command, self.path, self._status, self.client_address[0])

    def _route_inner(self):
        path = self.path.split("?", 1)[0]
        if path in ("/victor/health", "/tori/health"):
            up = self._upstream_ok()
            sw = swap_state()
            return self._json(200 if up and not sw else 503, {"status": "switching_model" if sw else "ok" if up else "model_not_running",
                                                   "gateway": "ok", "auth_required": self.cfg["require_auth"]})
        if path == "/victor/pair":
            return self._pair()
        why = self._authed()
        if why == "rate":
            return self._json(429, {"error": {"message": "This key's request limit was reached. Try again in a minute.",
                                              "type": "rate_limit_error"}}, {"Retry-After": "60"})
        if why == "expired":
            return self._json(401, {"error": {"message": "This API key has expired. Create a new one in the VictorLinux Control Center.",
                                              "type": "authentication_error"}}, {"WWW-Authenticate": 'Bearer realm="victor"'})
        if why:
            return self._json(401, {"error": {"message": "Missing or invalid API key. Send 'Authorization: Bearer <key>'.",
                                              "type": "authentication_error"}},
                              {"WWW-Authenticate": 'Bearer realm="victor"'})
        if path == "/victor/companion":
            if self.command != "GET":
                return self._json(405, {"error": {"message": "read-only endpoint"}}, {"Allow": "GET"})
            return self._json(200, companion.report(self.cfg))
        if path in ("/victor/capabilities", "/tori/capabilities"):
            return self._json(200, capabilities(self.cfg.get("model_path")))
        if path.startswith("/victor/"):
            return self._json(404, {"error": {"message": "unknown victor endpoint"}})
        if path in OLLAMA_ROUTES:
            return self._ollama(path)
        ui_asset = self.command == "GET" and (path.startswith(UI_ASSET_PREFIXES) or path in UI_ASSET_PATHS)
        if path != "/" and not path.startswith(ALLOWED_PREFIXES) and not ui_asset:
            return self._json(404, {"error": {"message": "path not exposed by gateway"}})
        self._proxy()

    # ---- Ollama-compatible subset (so Ollama-only clients can use this PC) -------------------------
    # Translated onto llama-server. Deliberately NOT forwarded: tools/functions, images, model pull/push/delete/create.
    def _read_json(self):
        n = content_length(self.headers, self.cfg["max_body"])
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            raise ValueError("invalid JSON body")

    def _await_swap(self, limit=None):
        """Hold the request while a model switch is in progress. True = go ahead; False = still switching."""
        deadline = time.time() + (SWAP_WAIT_S if limit is None else limit)
        while swap_state() is not None:
            if time.time() >= deadline:
                return False
            time.sleep(0.25)
        return True

    def _switching(self):
        st = swap_state() or {}
        return self._json(503, {"error": {"message": f"The model is being switched to {st.get('to') or 'another model'}. Retry in a few seconds.",
                                          "type": "model_switching"}}, {"Retry-After": "10"})

    def _up(self, method, path, body=None):
        if not self._await_swap():
            raise OSError("model switch in progress")
        c = http.client.HTTPConnection("127.0.0.1", self.cfg["upstream_port"], timeout=600)
        c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + self.cfg["upstream_key"], "Content-Type": "application/json"})
        return c, c.getresponse()

    def _ndjson_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _ndjson(self, obj):
        b = (json.dumps(obj) + "\n").encode()
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()

    def _sse_events(self, r):
        buf = b""
        while True:
            chunk = r.read1(8192) if hasattr(r, "read1") else r.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"data:"):
                    data = line[5:].strip()
                    if data and data != b"[DONE]":
                        try:
                            yield json.loads(data)
                        except ValueError:
                            pass

    def _ollama(self, path):
        model = self.cfg.get("model_path")
        name = os.path.basename(model) if model else "victor-local"
        with State.lock:
            State.last_request = time.time()
        if path == "/api/version":
            return self._json(200, {"version": "victor-compat (not Ollama)"})
        if path == "/api/tags":
            size = os.path.getsize(model) if model and os.path.exists(model) else 0
            return self._json(200, {"models": [{"name": "victor-local", "model": "victor-local", "size": size,
                                                "details": {"family": "llama.cpp", "format": "gguf", "file": name}}]})
        if self.command != "POST":
            return self._json(405, {"error": "use POST"})
        try:
            b = self._read_json()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        stream = bool(b.get("stream", True))          # Ollama's default is streaming
        opts = b.get("options") or {}
        mdl = b.get("model") or "victor-local"
        try:
            if path == "/api/generate":
                up = {"prompt": str(b.get("prompt", "")), "stream": stream}
                if "num_predict" in opts: up["n_predict"] = int(opts["num_predict"])
                for k_o, k_u in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"), ("seed", "seed")):
                    if k_o in opts: up[k_u] = opts[k_o]
                c, r = self._up("POST", "/completion", up)
            else:
                up = {"messages": b.get("messages") or [], "stream": stream}
                if "num_predict" in opts: up["max_tokens"] = int(opts["num_predict"])
                if "temperature" in opts: up["temperature"] = opts["temperature"]
                c, r = self._up("POST", "/v1/chat/completions", up)   # 'tools' intentionally dropped
        except (OSError, ValueError, TypeError):
            return self._json(503, {"error": "No model is loaded. Start the inference server from the VictorLinux Control Center."})
        if r.status != 200:
            err = r.read()[:500].decode("utf-8", "replace")
            c.close()
            return self._json(r.status if r.status < 500 else 502, {"error": f"upstream {r.status}: {err}"})
        ts = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # noqa: E731
        gen = path == "/api/generate"
        try:
            if not stream:
                d = json.loads(r.read())
                if gen:
                    out = {"model": mdl, "created_at": ts(), "response": d.get("content", ""), "done": True,
                           "prompt_eval_count": d.get("tokens_evaluated"), "eval_count": d.get("tokens_predicted")}
                else:
                    u = d.get("usage") or {}
                    out = {"model": mdl, "created_at": ts(), "done": True,
                           "message": {"role": "assistant", "content": d["choices"][0]["message"].get("content") or ""},
                           "prompt_eval_count": u.get("prompt_tokens"), "eval_count": u.get("completion_tokens")}
                return self._json(200, out)
            self._ndjson_start()
            last = {}
            for ev in self._sse_events(r):
                if gen:
                    last = ev
                    self._ndjson({"model": mdl, "created_at": ts(), "response": ev.get("content", ""), "done": False})
                else:
                    ch = (ev.get("choices") or [{}])[0]
                    txt = (ch.get("delta") or {}).get("content") or ""
                    if ev.get("usage"): last = ev["usage"]
                    if txt:
                        self._ndjson({"model": mdl, "created_at": ts(), "message": {"role": "assistant", "content": txt}, "done": False})
            fin = {"model": mdl, "created_at": ts(), "done": True, "done_reason": "stop"}
            if gen:
                fin.update(response="", prompt_eval_count=last.get("tokens_evaluated"), eval_count=last.get("tokens_predicted"))
            else:
                fin.update(message={"role": "assistant", "content": ""}, prompt_eval_count=last.get("prompt_tokens"),
                           eval_count=last.get("completion_tokens"))
            self._ndjson(fin)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            c.close()

    def _upstream_ok(self):
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.cfg["upstream_port"], timeout=3)
            c.request("GET", "/health")
            ok = c.getresponse().status == 200
            c.close()
            return ok
        except OSError:
            return False

    def _proxy(self):
        try:
            n = content_length(self.headers, self.cfg["max_body"])
        except ValueError as e:
            return self._json(413 if "large" in str(e) else 400, {"error": {"message": str(e)}})
        body = self.rfile.read(n) if n else None
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        hdrs["Authorization"] = "Bearer " + self.cfg["upstream_key"]
        with State.lock:
            State.last_request = time.time()
        if not self._await_swap():
            return self._switching()
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.cfg["upstream_port"], timeout=600)
            c.request(self.command, self.path, body=body, headers=hdrs)
            r = c.getresponse()
        except OSError:
            return self._json(503, {"error": {"message": "No model is loaded. Start the inference server from the "
                                                         "VictorLinux Control Center (or it was unloaded while idle).",
                                              "type": "server_unavailable"}})
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        # re-frame as chunked so streaming (SSE) passes through unbuffered
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                chunk = r.read1(8192) if hasattr(r, "read1") else r.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            c.close()

    do_GET = do_POST = do_OPTIONS = do_DELETE = do_PUT = _route


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_config(args):
    secrets = tc.load_secrets()
    st = tc.load_settings()
    remote = st["remote"]
    bind = args.bind or remote["bind"]
    loopback = bind in ("127.0.0.1", "::1", "localhost")
    keys = [k for k in (secrets.get("api_keys") or []) if k]
    # Auth is mandatory whenever the listener is not loopback-only, or remote access is enabled
    # (a local reverse tunnel would otherwise make remote clients look like loopback).
    require = (not loopback) or bool(remote.get("enabled"))
    if require and not keys:
        raise SystemExit("Refusing to start: remote access needs an API key. Create one in the Control Center.")
    return {
        "bind": bind, "port": args.port or remote["port"], "tls": remote.get("tls", False),
        "require_auth": require, "keys": keys,
        "upstream_port": args.upstream_port, "upstream_key": secrets.get("upstream_key", ""),
        "model_path": st.get("model") or None, "max_body": 64 << 20,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind")
    ap.add_argument("--port", type=int)
    ap.add_argument("--upstream-port", type=int, default=18081)
    ap.add_argument("--cert")
    ap.add_argument("--key")
    args = ap.parse_args()
    cfg = build_config(args)
    Handler.cfg = cfg
    State.usage = apikeys.UsageLog(tc.data_path("logs", "api-usage.jsonl"))
    srv = Server((cfg["bind"], cfg["port"]), Handler)
    if cfg["tls"]:
        cert = args.cert or tc.data_path("server", "tls", "cert.pem")
        key = args.key or tc.data_path("server", "tls", "key.pem")
        if not (cert and key and os.path.exists(cert) and os.path.exists(key)):
            raise SystemExit("TLS enabled but certificate missing. Run: victor-tls-cert <hostname-or-ip>")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        tls_per_connection(srv, ctx)
    sys.stderr.write(f"{tc.now()} gateway listening on {cfg['bind']}:{cfg['port']} "
                     f"tls={cfg['tls']} auth={'required' if cfg['require_auth'] else 'off (loopback only)'}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
