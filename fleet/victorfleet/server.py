"""HTTPS worker API + local admin API (Unix socket) for the coordinator. One process = one authority."""
import hashlib, http.server, json, os, socket, socketserver, ssl, threading, time, urllib.parse
from . import persistence, tlsutil
from .protocol import (ProtocolError, envelope, new_id, validate_envelope, validate_worker_payload, MAX_MESSAGE_BYTES, MAX_BATCH)
from .scheduler import Coordinator
from .store import Store, sha256_hex

DEFAULT_CONFIG = {"bind": "127.0.0.1", "port": 19970, "network_enabled": False, "allow_wildcard": False,
                  "max_artifact_bytes": 256 << 20, "artifact_quota_bytes": 2 << 30, "plan_approval": True,
                  "share_plan_with_phones": False, "max_devices": 4, "long_poll_max_s": 25}
ARTIFACT_LOCK = threading.Lock()


def load_config(directory):
    p = os.path.join(directory, "config.json")
    c = dict(DEFAULT_CONFIG)
    try:
        c.update(json.load(open(p)))
    except (OSError, ValueError):
        pass
    return c


def save_config(directory, cfg):
    p = os.path.join(directory, "config.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)


def effective_bind(cfg):
    """Local-only until the owner enables fleet networking; wildcard only by explicit opt-in."""
    if not cfg["network_enabled"]:
        return "127.0.0.1"
    if cfg["bind"] in ("0.0.0.0", "::") and not cfg["allow_wildcard"]:
        raise ValueError("refusing wildcard bind; set an explicit LAN/VPN address or allow_wildcard=true deliberately")
    return cfg["bind"]


class FleetService:
    def __init__(self, directory=None, cfg_override=None, clock=time.time):
        info = persistence.resolve() if directory is None else {"path": directory, "durable": None, "writable": True, "reason": "explicit directory"}
        self.dir = info["path"]
        self.persistence = info
        self.cfg = load_config(self.dir)
        self.cfg.update(cfg_override or {})
        self.store = Store(self.dir)
        self.coord = Coordinator(self.store, {"plan_approval": self.cfg["plan_approval"],
                                           "share_plan_with_phones": self.cfg["share_plan_with_phones"]}, clock=clock)
        from . import workflows
        workflows.register(self.coord)
        self.bind = effective_bind(self.cfg)
        names = {self.bind} if self.bind not in ("0.0.0.0", "::") else set()
        self.crt, self.key = tlsutil.ensure_cert(os.path.join(self.dir, "tls"), names | {socket.gethostname()})
        self.fingerprint = tlsutil.fingerprint(self.crt)
        self.spki = tlsutil.spki_fingerprint(self.crt)
        self.sock_path = os.path.join(self.dir, "admin.sock")
        self._stop = threading.Event()
        self.httpd = self.admin = None
        self.threads = []
        self.started_ms = int(time.time() * 1000)

    # ------------------------------------------------------------------ run
    def start(self):
        svc = self
        class H(WorkerHandler):
            service = svc
        self.httpd = ThreadedHTTPS((self.bind, self.cfg["port"]), H)
        tls_per_connection(self.httpd, tlsutil.server_context(self.crt, self.key))
        self.port = self.httpd.server_address[1]
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        class A(AdminHandler):
            service = svc
        old = os.umask(0o177)
        try:
            self.admin = ThreadedUnix(self.sock_path, A)
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)
        for target in (self.httpd.serve_forever, self.admin.serve_forever, self._tick_loop):
            t = threading.Thread(target=target, daemon=True); t.start(); self.threads.append(t)
        self.store.event("coordinator_started", detail=f"{self.bind}:{self.port} durable={self.persistence['durable']}")
        return self

    def _tick_loop(self):
        n = 0
        while not self._stop.wait(1.0):
            try:
                self.coord.tick()
                n += 1
                if n % 3600 == 0:
                    self.store.prune_seen()
            except Exception as e:     # keep the authority alive; surface the fault
                try: self.store.event("tick_error", "error", detail=str(e)[:300])
                except Exception: pass

    def stop(self):
        self._stop.set()
        for s in (self.httpd, self.admin):
            if s:
                s.shutdown(); s.server_close()
        self.store.event("coordinator_stopped")
        self.store.close()
        try: os.unlink(self.sock_path)
        except OSError: pass

    # ------------------------------------------------------------- operations
    def pairing_payload(self, ttl_s=600, label=None):
        secret = self.store.create_pairing(ttl_s, label)
        host = self.bind if self.bind not in ("0.0.0.0", "::") else socket.gethostname()
        return {"coordinator_url": f"https://{host}:{self.port}", "coordinator_fingerprint_sha256": self.fingerprint,
                "coordinator_spki_sha256": self.spki, "pairing_secret": secret, "expires_in_s": ttl_s, "protocol_major": 1,
                "verify": "Confirm the fingerprint shown on this screen equals the one the phone displays before approving."}

    def enroll(self, env):
        validate_envelope(env)
        if env["type"] != "enroll_request":
            raise ProtocolError("bad_request", "expected enroll_request")
        p = env["payload"]
        validate_worker_payload("enroll_request", p)
        fpr = tlsutil.key_fingerprint(p["public_key_pem"])
        if not fpr:
            raise ProtocolError("bad_request", "public_key_pem is not a valid public key")
        msg = f"victorfleet-enroll-v1\n{self.fingerprint}\n{sha256_hex(p['pairing_proof'])}\n{p['nonce']}".encode()
        if not tlsutil.verify_ecdsa(p["public_key_pem"], msg, p["signature_b64"]):
            raise ProtocolError("unauthorized", "proof-of-possession of the device key failed")
        count = self.store.one("SELECT COUNT(*) c FROM devices WHERE state='paired'")["c"]
        if count >= self.cfg["max_devices"]:
            raise ProtocolError("quota_exceeded", f"fleet already has {count} paired devices (limit {self.cfg['max_devices']})")
        did = "dev-" + new_id()[:12]
        token, thash = self.store.new_credential()
        with self.store.tx() as db:
            if not self.store.consume_pairing(p["pairing_proof"], db):
                raise ProtocolError("unauthorized", "pairing secret invalid, expired or already used")
            db.execute("INSERT INTO devices(id,name,pubkey_pem,key_fpr,token_hash,created_ms) VALUES(?,?,?,?,?,?)",
                       (did, p["device_name"], p["public_key_pem"], fpr, thash, int(time.time() * 1000)))
            self.store.event("device_paired", device_id=did, detail=p["device_name"], db=db)
        return envelope("enroll_response", "coordinator", did, {"device_id": did, "credential": token, "coordinator_fingerprint_sha256": self.fingerprint,
                                                              "protocol_major": 1, "lease_s": self.coord.cfg["lease_s"], "heartbeat_s": 15}, in_reply_to=env["msg_id"])

    def rotate(self, dev):
        token, thash = self.store.new_credential()
        with self.store.tx() as db:
            db.execute("UPDATE devices SET token_hash=?, cred_version=cred_version+1 WHERE id=?", (thash, dev["id"]))
            self.store.event("credential_rotated", device_id=dev["id"], db=db)
        return token

    def status(self):
        s = self.store
        devs = []
        for d in s.q("SELECT * FROM devices ORDER BY created_ms"):
            act = self.coord._active_attempts(d["id"]) if d["state"] == "paired" else []
            devs.append({"id": d["id"], "name": d["name"], "state": d["state"], "enabled": bool(d["enabled"]), "paused": bool(d["paused"]),
                         "connected": self.coord.online(d) if d["state"] == "paired" else False, "last_seen_ms": d["last_seen_ms"],
                         "key_fingerprint": d["key_fpr"], "device_state": json.loads(d["device_state_json"] or "{}"),
                         "capabilities": json.loads(d["caps_json"] or "{}"), "active_jobs": [a["job_id"] for a in act],
                         "features": self.coord.features(d)})
        counts = {r["state"]: r["c"] for r in s.q("SELECT state, COUNT(*) c FROM jobs GROUP BY state")}
        return {"running": True, "bind": f"{self.bind}:{self.port}", "network_enabled": self.cfg["network_enabled"],
                "fingerprint_sha256": self.fingerprint, "spki_sha256": self.spki, "persistence": self.persistence, "data_dir": self.dir,
                "devices": devs, "job_counts": counts, "plan_approval_required": self.cfg["plan_approval"],
                "share_plan_with_phones": self.cfg["share_plan_with_phones"],
                "durability_warning": None if self.persistence.get("durable") in (True, None) else
                    "State is NOT durable: " + self.persistence["reason"]}

    def artifact_dir(self):
        return os.path.join(self.dir, "artifacts")


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


class ThreadedHTTPS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def handle_error(self, request, client_address):   # TLS handshake failures from scanners are routine
        pass


class UnixHTTP(http.server.HTTPServer):
    address_family = socket.AF_UNIX
    def server_bind(self):
        self.socket.bind(self.server_address); self.server_name = "unix"; self.server_port = 0
    def get_request(self):
        c, _ = super().get_request(); return c, ("local", 0)


class ThreadedUnix(socketserver.ThreadingMixIn, UnixHTTP):
    daemon_threads = True


class Base(http.server.BaseHTTPRequestHandler):
    service = None
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def _err(self, e):
        code = {"unauthorized": 401, "revoked": 401, "too_large": 413, "quota_exceeded": 413, "stale_attempt": 409,
                "unknown_job": 404, "unsupported_protocol": 426}.get(e.code, 400)
        self._json(code, {"error": e.to_dict()})

    def _body(self, limit):
        raw = (self.headers.get("Content-Length") or "0").strip()
        if not raw.isascii() or not raw.isdigit():
            raise ProtocolError("bad_request", "bad Content-Length")
        n = int(raw)
        if n > limit:
            raise ProtocolError("too_large", f"body over {limit} bytes")
        return self.rfile.read(n)


class WorkerHandler(Base):
    def _auth(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            raise ProtocolError("unauthorized", "missing bearer credential")
        d = self.service.store.device_by_token(h[7:].strip())
        if not d:
            raise ProtocolError("unauthorized", "unknown credential")
        if d["state"] != "paired":
            raise ProtocolError("revoked", "this device has been revoked; re-pair it")
        return d

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/v1/enroll":
                body = json.loads(self._body(MAX_MESSAGE_BYTES) or b"{}")
                self._json(200, self.service.enroll(body))
            elif path == "/v1/exchange":
                self._exchange(self._auth())
            elif path.startswith("/v1/terminal/") and path.endswith("/exchange"):
                dev = self._auth()
                sid = path.split("/")[3]
                body = json.loads(self._body(MAX_MESSAGE_BYTES) or b"{}")
                wait = min(float(body.get("wait_s", 0) or 0), self.service.cfg["long_poll_max_s"])
                end = time.time() + wait
                while True:
                    out = self.service.coord.terminals.phone_exchange(dev, sid, body)
                    if out["chunks"] or time.time() >= end or out["state"] in ("denied", "closed", "failed", "revoked", "expired", "disconnected"):
                        break
                    body = dict(body, chunks=[])             # already consumed; only poll for more output from now on
                    time.sleep(0.1)
                self._json(200, out)
            elif path == "/v1/rotate":
                d = self._auth(); self._json(200, {"credential": self.service.rotate(d)})
            else:
                self._json(404, {"error": {"code": "bad_request", "message": "no such endpoint"}})
        except ProtocolError as e:
            self._err(e)
        except (ValueError, UnicodeDecodeError):
            self._err(ProtocolError("bad_request", "malformed JSON"))

    def _exchange(self, dev):
        s = self.service
        body = json.loads(self._body(MAX_MESSAGE_BYTES * 4) or b"{}")
        msgs = body.get("messages", [])
        if not isinstance(msgs, list) or len(msgs) > MAX_BATCH:
            raise ProtocolError("too_large", f"at most {MAX_BATCH} messages per exchange")
        with s.store.tx() as db:
            db.execute("UPDATE devices SET last_seen_ms=? WHERE id=?", (s.coord.now(), dev["id"]))
        s.coord.ack_outbox(dev["id"], [m for m in body.get("acks", []) if isinstance(m, str)])
        results = []
        for m in msgs:
            try:
                if len(json.dumps(m)) > MAX_MESSAGE_BYTES:
                    raise ProtocolError("too_large", "message too large")
                validate_envelope(m, expect_sender=dev["id"])
                if m["target"] != "coordinator" and m["type"] != "node_message":
                    raise ProtocolError("bad_request", "target must be 'coordinator'")
                validate_worker_payload(m["type"], m["payload"])
                r = s.coord.handle(dev, m)
                results.append({"msg_id": m.get("msg_id"), "ok": True, **({"duplicate": True} if r.get("duplicate") else {})})
            except ProtocolError as e:
                results.append({"msg_id": m.get("msg_id") if isinstance(m, dict) else None, "ok": False, "error": e.to_dict()})
        if msgs:
            s.coord.tick()
        wait = min(float(body.get("wait_s", 0) or 0), s.cfg["long_poll_max_s"])
        end = time.time() + wait
        out = s.coord.drain(dev["id"])
        while not out and time.time() < end:
            time.sleep(0.25)
            out = s.coord.drain(dev["id"])
        self._json(200, {"messages": out, "results": results, "server_ms": int(time.time() * 1000)})

    def do_PUT(self):
        """PUT /v1/artifacts/<job>/<attempt>/<name>?sha256=<hex> : worker uploads a result artifact."""
        s = self.service
        try:
            dev = self._auth()
            u = urllib.parse.urlparse(self.path)
            parts = u.path.strip("/").split("/")
            if len(parts) != 5 or parts[:2] != ["v1", "artifacts"]:
                raise ProtocolError("bad_request", "bad artifact path")
            job_id, attempt, name = parts[2], int(parts[3]), parts[4]
            want = (urllib.parse.parse_qs(u.query).get("sha256") or [""])[0]
            from .protocol import validate_artifact_ref, SHA256_RE
            n = int(self.headers.get("Content-Length") or 0)
            validate_artifact_ref({"name": name, "size": n, "sha256": want})
            j = s.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            a = s.store.one("SELECT * FROM attempts WHERE job_id=? AND n=?", (job_id, attempt))
            if not j or not a or a["device_id"] != dev["id"] or j["attempts"] != attempt or a["state"] not in ("running", "assigned", "awaiting_approval", "cancelling"):
                raise ProtocolError("stale_attempt", "upload not authorized for this attempt")
            if n > s.cfg["max_artifact_bytes"]:
                raise ProtocolError("too_large", "artifact over per-file limit")
            with ARTIFACT_LOCK:
                used = s.store.one("SELECT COALESCE(SUM(size),0) u FROM artifacts")["u"]
                if used + n > s.cfg["artifact_quota_bytes"]:
                    raise ProtocolError("quota_exceeded", "artifact store quota reached; purge old plans")
            aid = "art-" + new_id()[:16]
            rel = os.path.join(aid)
            full = os.path.join(s.artifact_dir(), rel)
            h, got = hashlib.sha256(), 0
            with open(full, "wb") as f:
                while got < n:
                    chunk = self.rfile.read(min(1 << 20, n - got))
                    if not chunk:
                        break
                    f.write(chunk); h.update(chunk); got += len(chunk)
                f.flush(); os.fsync(f.fileno())
            if got != n or h.hexdigest() != want:
                os.unlink(full)
                raise ProtocolError("bad_request", "artifact size/SHA-256 mismatch; discarded")
            with s.store.tx() as db:
                db.execute("DELETE FROM artifacts WHERE job_id=? AND attempt=? AND name=?", (job_id, attempt, name))
                db.execute("INSERT INTO artifacts(id,job_id,attempt,device_id,name,size,sha256,rel_path,created_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                           (aid, job_id, attempt, dev["id"], name, n, want, rel, int(time.time() * 1000)))
            self._json(200, {"artifact_id": aid, "sha256": want, "size": n})
        except ProtocolError as e:
            self._err(e)
        except ValueError:
            self._err(ProtocolError("bad_request", "bad request"))

    def do_GET(self):
        s = self.service
        try:
            dev = self._auth()
            parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["v1", "artifacts"]:
                raise ProtocolError("bad_request", "bad path")
            a = s.store.one("SELECT * FROM artifacts WHERE id=? AND owner_job_final=1", (parts[2],))
            if not a:
                raise ProtocolError("unknown_job", "no such artifact")
            allowed = a["device_id"] == dev["id"]
            if not allowed:
                for r in s.store.q("""SELECT j.inputs_json FROM attempts t JOIN jobs j ON j.id=t.job_id
                                      WHERE t.device_id=? AND t.state IN ('assigned','running','awaiting_approval') AND j.attempts=t.n""", (dev["id"],)):
                    if any(i["job"] == a["job_id"] and i["name"] == a["name"] for i in json.loads(r["inputs_json"])):
                        allowed = True
            if not allowed:
                raise ProtocolError("permission_denied", "no active assignment grants this artifact to your device")
            full = os.path.join(s.artifact_dir(), a["rel_path"])
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(a["size"])); self.send_header("X-Artifact-SHA256", a["sha256"]); self.end_headers()
            with open(full, "rb") as f:
                while True:
                    c = f.read(1 << 20)
                    if not c: break
                    self.wfile.write(c)
        except ProtocolError as e:
            self._err(e)


class AdminHandler(Base):
    """Owner-only control surface (Unix socket, mode 0600 in the private data dir)."""
    def _route(self, method):
        s, c = self.service, self.service.coord
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        parts = u.path.strip("/").split("/")
        body = json.loads(self._body(4 << 20) or b"{}") if method == "POST" else {}
        k = (method, parts[0] if parts else "")
        if k == ("GET", "status"): return s.status()
        if k == ("POST", "pair"): return s.pairing_payload(int(body.get("ttl_s", 600)), body.get("label"))
        if k == ("POST", "plans") and len(parts) == 1:
            pid = c.submit_plan(body["spec"], body.get("title"), body.get("auto_approve")); return {"plan": pid, "review": c.plan_review(pid)}
        if k == ("GET", "plans") and len(parts) == 1:
            return [dict(r) | {"spec_json": None} for r in s.store.q("SELECT * FROM plans ORDER BY created_ms DESC LIMIT 100")]
        if k == ("GET", "plans") and len(parts) == 3 and parts[2] == "review": return c.plan_review(parts[1])
        if k == ("GET", "plans") and len(parts) == 3 and parts[2] == "report": return c.report(parts[1])
        if k == ("POST", "plans") and len(parts) == 3 and parts[2] == "approve": c.approve_plan(parts[1]); return {"ok": True}
        if k == ("POST", "plans") and len(parts) == 3 and parts[2] == "cancel": c.cancel_plan(parts[1]); return {"ok": True}
        if k == ("GET", "jobs"):
            return [{x: r[x] for x in ("id", "plan_id", "op", "state", "priority", "attempts", "note")} for r in
                    s.store.q("SELECT * FROM jobs " + ("WHERE plan_id=? " if "plan" in q else "") + "ORDER BY created_ms DESC LIMIT 500", tuple(q.get("plan", [])))]
        if k == ("POST", "jobs") and len(parts) == 3 and parts[2] == "cancel": return {"state": c.cancel_job(parts[1])}
        if k == ("POST", "devices") and len(parts) == 3:
            did, act = parts[1], parts[2]
            if act == "revoke": c.revoke_device(did, body.get("reason", "owner revoked")); return {"ok": True}
            if act in ("pause", "resume", "enable", "disable"):
                col, val = {"pause": ("paused", 1), "resume": ("paused", 0), "enable": ("enabled", 1), "disable": ("enabled", 0)}[act]
                with s.store.tx() as db:
                    db.execute(f"UPDATE devices SET {col}=? WHERE id=? AND state='paired'", (val, did)); s.store.event(f"device_{act}", device_id=did, db=db)
                return {"ok": True}
            if act == "unpair":
                c.revoke_device(did, "owner unpaired"); return {"ok": True}
        T, Dh = c.terminals, c.drafts
        if k == ("POST", "terminal") and len(parts) == 1:
            return T.open(body["device"], body.get("desktop") or "victorlinux-local", body.get("mode", "interactive"), body.get("privilege", "user"),
                          body.get("command"), int(body.get("cols", 80)), int(body.get("rows", 24)))
        if k == ("GET", "terminal") and len(parts) == 1: return T.list()
        if k == ("GET", "terminal") and len(parts) == 2: return T.info(parts[1])
        if k == ("POST", "terminal") and len(parts) == 3 and parts[2] == "close": return T.close(parts[1], body.get("reason", "closed by the owner"))
        if k == ("POST", "terminal") and len(parts) == 3 and parts[2] == "exchange":
            wait = min(float(body.get("wait_s", 0) or 0), s.cfg["long_poll_max_s"])
            end = time.time() + wait
            while True:
                out = T.desktop_exchange(parts[1], body.get("token"), body)
                if out["chunks"] or time.time() >= end or out["state"] in ("denied", "closed", "failed", "revoked", "expired", "disconnected"):
                    return out
                body = dict(body, chunks=[])
                time.sleep(0.1)
        if k == ("POST", "drafts") and len(parts) == 1:
            return Dh.create(body["device"], body["channel"], body["draft"], body.get("origin") or "victorlinux-local")
        if k == ("GET", "drafts") and len(parts) == 1: return Dh.list()
        if k == ("GET", "drafts") and len(parts) == 2: return Dh.get(parts[1])
        if k == ("POST", "drafts") and len(parts) == 3 and parts[2] == "retry": return Dh.retry(parts[1])
        if k == ("GET", "events"):
            return [dict(r) for r in s.store.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(q.get("n", ["200"])[0]),))]
        if k == ("POST", "tick"): c.tick(); return {"ok": True}
        raise ProtocolError("bad_request", "no such admin route")

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")

    def _dispatch(self, m):
        try:
            self._json(200, self._route(m))
        except ProtocolError as e:
            self._err(e)
        except (KeyError, ValueError, TypeError) as e:
            self._err(ProtocolError("bad_request", f"bad request: {e}"))
