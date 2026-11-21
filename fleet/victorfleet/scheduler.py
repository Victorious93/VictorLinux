"""Coordinator core: plans, DAG validation, assignment, leases, fencing, recovery.

This is the ONLY place that decides which phone runs what. Workers never schedule. There is no exactly-once
claim: delivery is at-least-once, results are fenced by (job_id, attempt), and uncertain external outcomes are
parked in `needs_review` instead of being replayed.
"""
import json, time
from .protocol import (ProtocolError, TERMINAL, OP_RE, ID_RE, MAX_ARGS_BYTES, MAX_PLAN_JOBS, MAX_PLAN_BYTES, envelope,
                       validate_capabilities, now_ms, new_id)

DEFAULTS = {
    "lease_s": 90,                 # assignment lease; refreshed by progress/heartbeat from the owning attempt
    "ack_grace_s": 30,             # assigned but never acked -> safe to requeue (nothing ran)
    "offline_after_s": 45,         # no heartbeat/poll -> device counted disconnected
    "max_jobs_per_plan": 64,
    "max_active_plans": 16,
    "max_deps_per_job": 16,
    "battery_floor": 15,           # never start work below this unless charging
    "blocked_thermal": ("severe", "critical", "emergency", "shutdown"),
    "plan_approval": True,         # owner must approve a plan before first dispatch (configurable)
    "share_plan_with_phones": False,  # OFF by default: send each assignment the plan's job list so a phone's owner can approve
                                      # a reviewed batch once. Phones still decide on their own; see docs/FLEET.md "Plan manifest".
    "default_concurrency": 1,
    "max_concurrency": 4,
}
JOB_KEYS = {"key", "op", "args", "deps", "priority", "deadline_s", "device", "requires_root", "ui_exclusive",
            "idempotent", "max_attempts", "inputs", "min_battery", "min_free_bytes"}
LOCAL_PREFIX = "coordinator."


class Coordinator:
    def __init__(self, store, cfg=None, clock=time.time):
        self.s = store
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.clock = clock
        self.local_handlers = {}

    def now(self):
        return int(self.clock() * 1000)

    # ------------------------------------------------------------------ plans
    def validate_plan(self, spec):
        """Pure validation; returns normalized job list or raises ProtocolError(bad_request)."""
        if not isinstance(spec, dict) or not isinstance(spec.get("jobs"), list) or not spec["jobs"]:
            raise ProtocolError("bad_request", "plan needs a non-empty 'jobs' list")
        jobs = spec["jobs"]
        if len(jobs) > self.cfg["max_jobs_per_plan"]:
            raise ProtocolError("too_large", f"plan has more than {self.cfg['max_jobs_per_plan']} jobs")
        keys, norm = set(), []
        for j in jobs:
            if not isinstance(j, dict) or set(j) - JOB_KEYS:
                raise ProtocolError("bad_request", f"job has unknown fields: {sorted(set(j) - JOB_KEYS) if isinstance(j, dict) else j}")
            k = j.get("key")
            if not isinstance(k, str) or not ID_RE.match(k) or k in keys:
                raise ProtocolError("bad_request", f"job key {k!r} missing, invalid or duplicate")
            keys.add(k)
            op = j.get("op")
            if not isinstance(op, str) or not (OP_RE.match(op) or (op.startswith(LOCAL_PREFIX) and OP_RE.match(op[len(LOCAL_PREFIX):]))):
                raise ProtocolError("bad_request", f"job {k}: invalid op")
            args = j.get("args", {})
            if not isinstance(args, dict) or len(json.dumps(args)) > MAX_ARGS_BYTES:
                raise ProtocolError("too_large", f"job {k}: args must be an object under {MAX_ARGS_BYTES} bytes")
            deps = j.get("deps", [])
            if not isinstance(deps, list) or len(deps) > self.cfg["max_deps_per_job"] or not all(isinstance(d, str) for d in deps):
                raise ProtocolError("bad_request", f"job {k}: bad deps")
            pr = j.get("priority", 5)
            if not isinstance(pr, int) or isinstance(pr, bool) or not 0 <= pr <= 9:
                raise ProtocolError("bad_request", f"job {k}: priority must be 0..9 (0 highest)")
            ma = j.get("max_attempts", 1)
            if not isinstance(ma, int) or isinstance(ma, bool) or not 1 <= ma <= 5:
                raise ProtocolError("bad_request", f"job {k}: max_attempts 1..5")
            dl = j.get("deadline_s")
            if dl is not None and (not isinstance(dl, (int, float)) or dl <= 0 or dl > 7 * 86400):
                raise ProtocolError("bad_request", f"job {k}: deadline_s must be 0..7 days")
            for b in ("requires_root", "ui_exclusive", "idempotent"):
                if b in j and not isinstance(j[b], bool):
                    raise ProtocolError("bad_request", f"job {k}: {b} must be boolean")
            if j.get("requires_root") and op.startswith(LOCAL_PREFIX):
                raise ProtocolError("bad_request", f"job {k}: coordinator jobs cannot require device root")
            norm.append({"key": k, "op": op, "args": args, "deps": list(deps), "priority": pr, "max_attempts": ma,
                         "deadline_s": dl, "device": j.get("device"), "requires_root": bool(j.get("requires_root")),
                         "ui_exclusive": bool(j.get("ui_exclusive")), "idempotent": bool(j.get("idempotent")),
                         "inputs": j.get("inputs", []), "min_battery": int(j.get("min_battery", 0)),
                         "min_free_bytes": int(j.get("min_free_bytes", 0))})
        for j in norm:
            for d in j["deps"]:
                if d not in keys:
                    raise ProtocolError("bad_request", f"job {j['key']}: unknown dependency '{d}'")
                if d == j["key"]:
                    raise ProtocolError("bad_request", f"job {j['key']} depends on itself")
            for inp in j["inputs"]:
                if not isinstance(inp, dict) or inp.get("job") not in j["deps"] or not isinstance(inp.get("name"), str):
                    raise ProtocolError("bad_request", f"job {j['key']}: each input must name a dependency job and an artifact name")
        self._cycle_check(norm)
        return norm

    @staticmethod
    def _cycle_check(norm):
        indeg = {j["key"]: len(j["deps"]) for j in norm}
        users = {}
        for j in norm:
            for d in j["deps"]:
                users.setdefault(d, []).append(j["key"])
        ready = [k for k, n in indeg.items() if n == 0]
        seen = 0
        while ready:
            k = ready.pop()
            seen += 1
            for u in users.get(k, []):
                indeg[u] -= 1
                if indeg[u] == 0:
                    ready.append(u)
        if seen != len(norm):
            stuck = sorted(k for k, n in indeg.items() if n > 0)
            raise ProtocolError("bad_request", f"dependency cycle among jobs: {', '.join(stuck)}")

    def submit_plan(self, spec, title=None, auto_approve=None):
        norm = self.validate_plan(spec)
        active = self.s.one("SELECT COUNT(*) c FROM plans WHERE state IN ('proposed','approved')")["c"]
        if active >= self.cfg["max_active_plans"]:
            raise ProtocolError("quota_exceeded", "too many active plans")
        pid = "plan-" + new_id()[:12]
        approve = (not self.cfg["plan_approval"]) if auto_approve is None else auto_approve
        t = self.now()
        with self.s.tx() as db:
            db.execute("INSERT INTO plans(id,title,state,created_ms,approved_ms,spec_json) VALUES(?,?,?,?,?,?)",
                       (pid, title or spec.get("title") or pid, "approved" if approve else "proposed", t, t if approve else None,
                        json.dumps({"jobs": norm})))
            ids = {j["key"]: f"{pid}.{j['key']}" for j in norm}
            for j in norm:
                inputs = [{"job": ids[i["job"]], "name": i["name"]} for i in j["inputs"]]
                dl = int(t + j["deadline_s"] * 1000) if j["deadline_s"] else None
                db.execute("""INSERT INTO jobs(id,plan_id,op,args_json,priority,deadline_ms,state,deps_json,inputs_json,device_pin,
                    requires_root,ui_exclusive,idempotent,max_attempts,min_battery,min_free_bytes,coordinator_local,created_ms,updated_ms)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (ids[j["key"]], pid, j["op"], json.dumps(j["args"]), j["priority"], dl, "queued",
                            json.dumps([ids[d] for d in j["deps"]]), json.dumps(inputs), j["device"],
                            int(j["requires_root"]), int(j["ui_exclusive"]), int(j["idempotent"]), j["max_attempts"],
                            j["min_battery"], j["min_free_bytes"], int(j["op"].startswith(LOCAL_PREFIX)), t, t))
            self.s.event("plan_submitted", detail=f"{pid} jobs={len(norm)} approved={approve}", db=db)
        return pid

    def approve_plan(self, pid):
        with self.s.tx() as db:
            cur = db.execute("UPDATE plans SET state='approved', approved_ms=? WHERE id=? AND state='proposed'", (self.now(), pid))
            if cur.rowcount != 1:
                raise ProtocolError("unknown_job", "plan not found or not awaiting approval")
            self.s.event("plan_approved", detail=pid, db=db)

    def plan_review(self, pid):
        """Reviewable summary shown before first dispatch: who could run each job right now."""
        p = self.s.one("SELECT * FROM plans WHERE id=?", (pid,))
        if not p:
            raise ProtocolError("unknown_job", "no such plan")
        out = []
        for j in self.s.q("SELECT * FROM jobs WHERE plan_id=? ORDER BY priority, id", (pid,)):
            cands = [d["id"] for d in self._devices() if self._eligible(d, j, probe=True)[0]]
            out.append({"job": j["id"], "op": j["op"], "deps": json.loads(j["deps_json"]), "priority": j["priority"],
                        "candidates_now": cands if not j["coordinator_local"] else ["coordinator"],
                        "unassignable_now": (not cands) and not j["coordinator_local"]})
        return {"plan": pid, "state": p["state"], "title": p["title"], "jobs": out}

    # ---------------------------------------------------------------- devices
    def _devices(self):
        return self.s.q("SELECT * FROM devices WHERE state='paired'")

    def online(self, d):
        return d["last_seen_ms"] is not None and self.now() - d["last_seen_ms"] <= self.cfg["offline_after_s"] * 1000

    def _active_attempts(self, device_id):
        return self.s.q("""SELECT a.*, j.ui_exclusive FROM attempts a JOIN jobs j ON j.id=a.job_id
                           WHERE a.device_id=? AND a.state IN ('assigned','running','awaiting_approval','cancelling')""", (device_id,))

    def _eligible(self, d, j, probe=False):
        """Return (ok, reason). Uses the device's LIVE capability report, never a static flag."""
        if not d["enabled"] or d["paused"]:
            return False, "device disabled or paused"
        if not self.online(d):
            return False, "device offline"
        if j["device_pin"] and j["device_pin"] != d["id"]:
            return False, "pinned to another device"
        caps = json.loads(d["caps_json"] or "{}")
        ops = caps.get("operations", {})
        o = ops.get(j["op"])
        if not o or not o.get("available"):
            return False, "operation unsupported or executor unavailable"
        if not o.get("granted", True):
            return False, "permission not granted"
        if o.get("owner_interaction_required") and not probe:
            return False, "owner interaction required on device"
        if j["requires_root"]:
            r = caps.get("root", {})
            if not r.get("available") or not r.get("granted"):
                return False, "root not available/granted"
            exp = r.get("grant_expires_ms")
            if exp is not None and exp <= self.now() + 60_000:
                return False, "root grant expires too soon"
        ds = json.loads(d["device_state_json"] or "{}")
        if ds.get("thermal") in self.cfg["blocked_thermal"]:
            return False, f"thermal pressure ({ds['thermal']})"
        bat = ds.get("battery_percent")
        if bat is not None and not ds.get("charging"):
            if bat < max(self.cfg["battery_floor"], j["min_battery"]):
                return False, "battery below policy"
        if j["min_free_bytes"] and ds.get("free_storage_bytes", 1 << 62) < j["min_free_bytes"]:
            return False, "insufficient free storage"
        act = self._active_attempts(d["id"])
        cap = min(int(caps.get("max_concurrent", self.cfg["default_concurrency"])), self.cfg["max_concurrency"])
        if len(act) >= max(cap, 1):
            return False, "device at capacity"
        if j["ui_exclusive"] and (any(a["ui_exclusive"] for a in act) or ds.get("foreground_busy")):
            return False, "foreground UI control already in use on this device"
        return True, ""

    # ------------------------------------------------------------------- tick
    def tick(self):
        t = self.now()
        self._expire_leases(t)
        self._resolve_dependencies(t)
        self._expire_deadlines(t)
        self._run_local(t)
        self._assign(t)

    def _expire_leases(self, t):
        for a in self.s.q("SELECT * FROM attempts WHERE state IN ('assigned','running','awaiting_approval','cancelling') AND lease_expires_ms<?", (t,)):
            self._lose_attempt(a, "lease expired (worker silent); the original worker may still be running")

    def _lose_attempt(self, a, why, device_revoked=False):
        """Attempt's worker is gone or untrustworthy. Never claim the work did or did not happen when unknown."""
        with self.s.tx() as db:
            j = db.execute("SELECT * FROM jobs WHERE id=?", (a["job_id"],)).fetchone()
            if j["attempts"] != a["n"] or a["state"] in ("done", "lost", "stale"):
                return
            db.execute("UPDATE attempts SET state='lost', finished_ms=?, note=? WHERE job_id=? AND n=?", (self.now(), why, a["job_id"], a["n"]))
            never_started = a["state"] == "assigned"
            if j["state"] == "cancelling":
                new, note = "cancelled_unconfirmed", "cancel requested but the worker never confirmed it stopped"
            elif never_started or (j["idempotent"] and j["attempts"] < j["max_attempts"]):
                if j["attempts"] < j["max_attempts"] or never_started:
                    new, note = "queued", f"requeued after lost attempt {a['n']}: {why}"
                    if never_started:
                        db.execute("UPDATE jobs SET attempts=attempts-1 WHERE id=?", (a["job_id"],))   # nothing ran; do not burn a retry
                else:
                    new, note = "failed", f"attempts exhausted: {why}"
            else:
                new, note = "needs_review", f"outcome uncertain, not replayed automatically: {why}"
            db.execute("UPDATE jobs SET state=?, note=?, updated_ms=? WHERE id=?", (new, note, self.now(), a["job_id"]))
            self.s.event("attempt_lost", "warn", a["job_id"], a["device_id"], f"n={a['n']} -> {new}: {why}", db=db)

    def _resolve_dependencies(self, t):
        # a queued job whose dependency ended unsuccessfully can never run
        for j in self.s.q("SELECT * FROM jobs WHERE state='queued'"):
            deps = json.loads(j["deps_json"])
            if not deps:
                continue
            st = [r["state"] for r in self.s.q(f"SELECT state FROM jobs WHERE id IN ({','.join('?'*len(deps))})", deps)]
            bad = [s for s in st if s in TERMINAL and s != "succeeded"]
            if bad:
                with self.s.tx() as db:
                    db.execute("UPDATE jobs SET state='dep_failed', note=?, updated_ms=? WHERE id=? AND state='queued'",
                               (f"dependency ended as {bad[0]}", t, j["id"]))
                    self.s.event("dep_failed", "warn", j["id"], db=db)

    def _expire_deadlines(self, t):
        for j in self.s.q("SELECT id,state FROM jobs WHERE deadline_ms IS NOT NULL AND deadline_ms<? AND state IN ('queued','assigned','running','awaiting_approval')", (t,)):
            if j["state"] == "queued":
                with self.s.tx() as db:
                    db.execute("UPDATE jobs SET state='expired', note='deadline passed before dispatch', updated_ms=? WHERE id=? AND state='queued'", (t, j["id"]))
                    self.s.event("deadline_expired", "warn", j["id"], db=db)
            else:
                self.cancel_job(j["id"], reason="deadline_exceeded")

    def _deps_met(self, j):
        deps = json.loads(j["deps_json"])
        if not deps:
            return True
        rows = self.s.q(f"SELECT state FROM jobs WHERE id IN ({','.join('?'*len(deps))})", deps)
        return len(rows) == len(deps) and all(r["state"] == "succeeded" for r in rows)

    def _plan_ok(self, j):
        r = self.s.one("SELECT state FROM plans WHERE id=?", (j["plan_id"],))
        return bool(r) and r["state"] == "approved"

    def _run_local(self, t):
        for j in self.s.q("SELECT * FROM jobs WHERE state='queued' AND coordinator_local=1 ORDER BY priority, created_ms"):
            if not (self._plan_ok(j) and self._deps_met(j)):
                continue
            h = self.local_handlers.get(j["op"])
            with self.s.tx() as db:
                db.execute("UPDATE jobs SET state='running', attempts=attempts+1, updated_ms=? WHERE id=?", (t, j["id"]))
            try:
                if not h:
                    raise RuntimeError(f"no coordinator handler for {j['op']}")
                res = h(self, j)
                with self.s.tx() as db:
                    db.execute("UPDATE jobs SET state='succeeded', result_json=?, updated_ms=? WHERE id=?", (json.dumps(res), self.now(), j["id"]))
                    self.s.event("job_succeeded", job_id=j["id"], detail="coordinator-local", db=db)
            except Exception as e:                                   # a genuine failure is recorded, never hidden
                with self.s.tx() as db:
                    db.execute("UPDATE jobs SET state='failed', error_json=?, updated_ms=? WHERE id=?",
                               (json.dumps({"code": "internal", "message": str(e)[:400]}), self.now(), j["id"]))
                    self.s.event("job_failed", "error", j["id"], detail=str(e)[:200], db=db)

    def _input_refs(self, j):
        refs = []
        for inp in json.loads(j["inputs_json"]):
            a = self.s.one("SELECT * FROM artifacts WHERE job_id=? AND name=? AND owner_job_final=1", (inp["job"], inp["name"]))
            if not a:
                return None
            refs.append({"artifact_id": a["id"], "name": a["name"], "size": a["size"], "sha256": a["sha256"], "from_job": a["job_id"]})
        return refs

    def _assign(self, t):
        devs = self._devices()
        for j in self.s.q("SELECT * FROM jobs WHERE state='queued' AND coordinator_local=0 ORDER BY priority, COALESCE(deadline_ms, 9e18), created_ms"):
            if not (self._plan_ok(j) and self._deps_met(j)):
                continue
            refs = self._input_refs(j)
            if refs is None:
                continue
            best, load = None, None
            for d in devs:
                ok, _ = self._eligible(d, j)
                if ok:
                    l = len(self._active_attempts(d["id"]))
                    if best is None or l < load:
                        best, load = d, l
            if not best:
                continue
            n = j["attempts"] + 1
            lease = t + self.cfg["lease_s"] * 1000
            payload = {
                "job_id": j["id"], "attempt": n, "fence": n, "op": j["op"], "args": json.loads(j["args_json"]),
                "inputs": refs, "lease_expires_ms": lease, "deadline_ms": j["deadline_ms"],
                "requires_root": bool(j["requires_root"]), "ui_exclusive": bool(j["ui_exclusive"]),
                "idempotent": bool(j["idempotent"])}
            plan = self._plan_manifest(j["plan_id"])
            if plan is not None:
                payload["plan"] = plan
            msg = envelope("assignment", "coordinator", best["id"], payload)
            with self.s.tx() as db:
                cur = db.execute("UPDATE jobs SET state='assigned', attempts=?, updated_ms=? WHERE id=? AND state='queued'", (n, t, j["id"]))
                if cur.rowcount != 1:
                    continue
                db.execute("INSERT INTO attempts(job_id,n,device_id,state,lease_expires_ms,started_ms) VALUES(?,?,?,?,?,?)",
                           (j["id"], n, best["id"], "assigned", lease, t))
                self.enqueue(best["id"], msg, db)
                self.s.event("assigned", job_id=j["id"], device_id=best["id"], detail=f"attempt {n}", db=db)

    def _plan_manifest(self, plan_id):
        """The optional `plan` object for an assignment, or None. Only when the owner switched `share_plan_with_phones` on,
        the plan has 2..MAX_PLAN_JOBS phone jobs (a single job gains nothing from a batch) and the whole thing fits.
        Jobs the coordinator runs itself are not listed: they never reach a phone. The same args are sent here as in each
        assignment, because a phone binds its owner's approval to exactly the args it was shown."""
        if not self.cfg["share_plan_with_phones"]:
            return None
        rows = [r for r in self.s.q("SELECT id, op, args_json, requires_root, coordinator_local FROM jobs WHERE plan_id=? ORDER BY priority, id",
                                    (plan_id,)) if not r["coordinator_local"]]
        if not 2 <= len(rows) <= MAX_PLAN_JOBS:
            return None
        plan = {"plan_id": plan_id, "jobs": [{"job_id": r["id"], "op": r["op"], "args": json.loads(r["args_json"]),
                                              "requires_root": bool(r["requires_root"])} for r in rows]}
        return plan if len(json.dumps(plan)) <= MAX_PLAN_BYTES else None

    # ---------------------------------------------------------------- outbox
    def enqueue(self, device_id, msg, db=None):
        d = db or self.s.db
        d.execute("INSERT INTO outbox(device_id,msg_id,body,created_ms) VALUES(?,?,?,?)", (device_id, msg["msg_id"], json.dumps(msg), self.now()))

    def drain(self, device_id, limit=32):
        """Messages not yet acked are redelivered (at-least-once); worker dedups by msg_id."""
        rows = self.s.q("SELECT * FROM outbox WHERE device_id=? AND acked=0 ORDER BY seq LIMIT ?", (device_id, limit))
        if rows:
            with self.s.tx() as db:
                for r in rows:
                    db.execute("UPDATE outbox SET delivered_ms=? WHERE seq=?", (self.now(), r["seq"]))
        return [json.loads(r["body"]) for r in rows]

    def ack_outbox(self, device_id, msg_ids):
        if msg_ids:
            with self.s.tx() as db:
                db.executemany("UPDATE outbox SET acked=1 WHERE device_id=? AND msg_id=?", [(device_id, m) for m in msg_ids])

    # --------------------------------------------------------------- cancel
    def cancel_job(self, job_id, reason="cancelled"):
        j = self.s.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise ProtocolError("unknown_job", "no such job")
        if j["state"] in TERMINAL:
            return j["state"]
        if j["state"] == "queued":
            with self.s.tx() as db:
                db.execute("UPDATE jobs SET state='cancelled', cancel_requested=1, note=?, updated_ms=? WHERE id=?", (reason, self.now(), job_id))
                self.s.event("cancelled", job_id=job_id, detail="before dispatch", db=db)
            return "cancelled"
        a = self.s.one("SELECT * FROM attempts WHERE job_id=? AND n=?", (job_id, j["attempts"]))
        with self.s.tx() as db:
            db.execute("UPDATE jobs SET state='cancelling', cancel_requested=1, note=?, updated_ms=? WHERE id=?", (reason, self.now(), job_id))
            if a:
                db.execute("UPDATE attempts SET state='cancelling' WHERE job_id=? AND n=?", (job_id, a["n"]))
                self.enqueue(a["device_id"], envelope("cancel", "coordinator", a["device_id"], {"job_id": job_id, "attempt": a["n"], "reason": reason}), db)
            self.s.event("cancel_requested", job_id=job_id, device_id=a["device_id"] if a else None, detail=reason, db=db)
        return "cancelling"

    def cancel_plan(self, pid):
        for j in self.s.q("SELECT id FROM jobs WHERE plan_id=?", (pid,)):
            self.cancel_job(j["id"], "plan cancelled")
        with self.s.tx() as db:
            db.execute("UPDATE plans SET state='cancelled' WHERE id=?", (pid,))

    # ------------------------------------------------------ worker messages
    def handle(self, dev, env):
        """Process one validated worker envelope. Idempotent per (sender, msg_id)."""
        t = env["type"]
        p = env["payload"]
        with self.s.tx() as db:
            first = self.s.mark_seen(dev["id"], env["msg_id"], db)
        if not first:
            return {"duplicate": True}
        fn = getattr(self, "_on_" + t, None)
        if not fn:
            raise ProtocolError("unknown_type", f"{t} not accepted from workers")
        return fn(dev, env, p) or {}

    def _attempt_for(self, dev, p):
        """Fencing: result is accepted only from the current attempt of the job on the same device."""
        j = self.s.one("SELECT * FROM jobs WHERE id=?", (p["job_id"],))
        if not j:
            raise ProtocolError("unknown_job", "no such job")
        a = self.s.one("SELECT * FROM attempts WHERE job_id=? AND n=?", (p["job_id"], p["attempt"]))
        if (not a or a["device_id"] != dev["id"] or j["attempts"] != p["attempt"] or a["state"] in ("done", "lost", "stale")):
            with self.s.tx() as db:
                self.s.event("stale_result_rejected", "warn", p["job_id"], dev["id"], f"attempt {p['attempt']}", db=db)
                self.enqueue(dev["id"], envelope("cancel", "coordinator", dev["id"],
                             {"job_id": p["job_id"], "attempt": p["attempt"], "reason": "stale_attempt"}), db)
            raise ProtocolError("stale_attempt", "attempt is obsolete; stop work on it")
        return j, a

    def _on_hello(self, dev, env, p):
        validate_capabilities(p["capabilities"])
        with self.s.tx() as db:
            db.execute("UPDATE devices SET caps_json=?, caps_ms=?, device_state_json=?, last_seen_ms=? WHERE id=?",
                       (json.dumps(p["capabilities"]), self.now(), json.dumps(p["capabilities"].get("device_state", {})), self.now(), dev["id"]))
            self.s.event("hello", device_id=dev["id"], detail=p.get("app_version"), db=db)
        # reconnect reconciliation
        claimed = {(r["job_id"], r["attempt"]) for r in p.get("running", [])}
        stale = []
        for (jid, n) in claimed:
            a = self.s.one("SELECT * FROM attempts WHERE job_id=? AND n=?", (jid, n))
            j = self.s.one("SELECT attempts FROM jobs WHERE id=?", (jid,))
            if a and j and a["device_id"] == dev["id"] and j["attempts"] == n and a["state"] in ("assigned", "running", "awaiting_approval", "cancelling"):
                with self.s.tx() as db:
                    db.execute("UPDATE attempts SET lease_expires_ms=? WHERE job_id=? AND n=?", (self.now() + self.cfg["lease_s"] * 1000, jid, n))
            else:
                stale.append((jid, n))
                with self.s.tx() as db:
                    self.enqueue(dev["id"], envelope("cancel", "coordinator", dev["id"], {"job_id": jid, "attempt": n, "reason": "stale_attempt"}), db)
        for a in self._active_attempts(dev["id"]):
            if (a["job_id"], a["n"]) not in claimed and a["state"] != "assigned":
                self._lose_attempt(a, "worker reconnected without this job (restart or reboot)")
        return {"reconciled": True, "stale_cancelled": len(stale)}

    def _on_heartbeat(self, dev, env, p):
        with self.s.tx() as db:
            db.execute("UPDATE devices SET last_seen_ms=?, device_state_json=? WHERE id=?", (self.now(), json.dumps(p["device_state"]), dev["id"]))
            for a in self._active_attempts(dev["id"]):    # a live worker keeps its own leases alive
                db.execute("UPDATE attempts SET lease_expires_ms=? WHERE job_id=? AND n=?", (self.now() + self.cfg["lease_s"] * 1000, a["job_id"], a["n"]))

    def _on_ack(self, dev, env, p):
        j, a = self._attempt_for(dev, p)
        with self.s.tx() as db:
            if a["state"] == "assigned":
                db.execute("UPDATE attempts SET state='running' WHERE job_id=? AND n=?", (p["job_id"], p["attempt"]))
                db.execute("UPDATE jobs SET state='running', updated_ms=? WHERE id=? AND state='assigned'", (self.now(), p["job_id"]))

    def _on_progress(self, dev, env, p):
        j, a = self._attempt_for(dev, p)
        with self.s.tx() as db:
            db.execute("UPDATE attempts SET progress=?, note=?, lease_expires_ms=? WHERE job_id=? AND n=?",
                       (p.get("percent"), p.get("note"), self.now() + self.cfg["lease_s"] * 1000, p["job_id"], p["attempt"]))
            # a progress report after an owner decision means the phone is executing again
            db.execute("UPDATE attempts SET state='running' WHERE job_id=? AND n=? AND state='awaiting_approval'", (p["job_id"], p["attempt"]))
            db.execute("UPDATE jobs SET state='running', updated_ms=? WHERE id=? AND state='awaiting_approval'", (self.now(), p["job_id"]))

    def _on_approval_needed(self, dev, env, p):
        j, a = self._attempt_for(dev, p)
        with self.s.tx() as db:
            db.execute("UPDATE attempts SET state='awaiting_approval', note=? WHERE job_id=? AND n=?", (p["reason"], p["job_id"], p["attempt"]))
            db.execute("UPDATE jobs SET state='awaiting_approval', note=?, updated_ms=? WHERE id=?", (p["reason"], self.now(), p["job_id"]))
            self.s.event("approval_needed", "warn", p["job_id"], dev["id"], p["reason"], db=db)

    def _on_complete(self, dev, env, p):
        j, a = self._attempt_for(dev, p)
        refs = p.get("artifacts", [])
        with self.s.tx() as db:
            have = {r["name"]: r for r in db.execute("SELECT * FROM artifacts WHERE job_id=? AND attempt=?", (p["job_id"], p["attempt"])).fetchall()}
            for r in refs:                                       # every claimed artifact must have been uploaded and verified
                x = have.get(r["name"])
                if not x or x["sha256"] != r["sha256"] or x["size"] != r["size"]:
                    raise ProtocolError("bad_request", f"artifact '{r['name']}' not uploaded or integrity mismatch")
            if j["state"] == "cancelling":
                final = "succeeded"      # it finished before the stop landed; result is real, report honestly
                note = "completed before cancellation took effect"
            else:
                final, note = "succeeded", None
            db.execute("UPDATE jobs SET state=?, result_json=?, note=?, updated_ms=? WHERE id=?", (final, json.dumps(p["result"]), note, self.now(), p["job_id"]))
            db.execute("UPDATE attempts SET state='done', finished_ms=? WHERE job_id=? AND n=?", (self.now(), p["job_id"], p["attempt"]))
            db.execute("UPDATE artifacts SET owner_job_final=1 WHERE job_id=? AND attempt=?", (p["job_id"], p["attempt"]))
            self.s.event("job_succeeded", job_id=p["job_id"], device_id=dev["id"], detail=f"attempt {p['attempt']}", db=db)

    def _on_fail(self, dev, env, p):
        j, a = self._attempt_for(dev, p)
        err = p["error"]
        side = p.get("side_effects", "possible")
        with self.s.tx() as db:
            db.execute("UPDATE attempts SET state='done', finished_ms=?, note=? WHERE job_id=? AND n=?", (self.now(), err["message"], p["job_id"], p["attempt"]))
            retry_ok = j["attempts"] < j["max_attempts"] and (side == "none" or j["idempotent"]) and j["state"] != "cancelling" and err["code"] not in ("permission_denied", "capability_missing", "cancelled")
            if j["state"] == "cancelling":
                new = "cancelled"
            elif retry_ok:
                new = "queued"
            elif side in ("possible",) and not j["idempotent"] and err["code"] == "internal":
                new = "needs_review"
            else:
                new = "failed"
            db.execute("UPDATE jobs SET state=?, error_json=?, updated_ms=? WHERE id=?", (new, json.dumps(err), self.now(), p["job_id"]))
            self.s.event("job_failed", "warn", p["job_id"], dev["id"], f"{err['code']} -> {new}", db=db)

    def _on_cancel_ack(self, dev, env, p):
        j, a = self._attempt_for(dev, p)
        with self.s.tx() as db:
            if p["state"] == "completed_before_cancel":
                return    # the real `complete` message decides the outcome; stay in cancelling until it arrives or the lease lapses
            db.execute("UPDATE attempts SET state='done', finished_ms=? WHERE job_id=? AND n=?", (self.now(), p["job_id"], p["attempt"]))
            db.execute("UPDATE jobs SET state='cancelled', updated_ms=? WHERE id=?", (self.now(), p["job_id"]))
            self.s.event("cancelled", job_id=p["job_id"], device_id=dev["id"], detail=p["state"], db=db)

    def _on_artifact_offer(self, dev, env, p):
        self._attempt_for(dev, p)    # authorization only; bytes arrive via the artifact endpoint

    def _on_node_message(self, dev, env, p):
        """Coordinator-relayed node messaging. Peers cannot expand another phone's permissions: the body is
        delivered as data under a topic; only job assignments from the coordinator can cause execution."""
        to = self.s.one("SELECT id,state FROM devices WHERE id=?", (p["to_device"],))
        if not to or to["state"] != "paired":
            raise ProtocolError("unknown_job", "target device is not a paired node")
        if len(json.dumps(p["body"])) > 16 * 1024:
            raise ProtocolError("too_large", "node message body over 16 KiB")
        with self.s.tx() as db:
            self.enqueue(to["id"], envelope("node_message", "coordinator", to["id"],
                         {"from_device": dev["id"], "topic": p["topic"], "body": p["body"], "original_msg_id": env["msg_id"]}), db)
            self.s.event("node_message", device_id=dev["id"], detail=f"{p['topic']} -> {to['id']}", db=db)

    # ------------------------------------------------------------- device ops
    def revoke_device(self, device_id, reason="owner revoked"):
        d = self.s.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not d:
            raise ProtocolError("unknown_job", "no such device")
        with self.s.tx() as db:
            db.execute("UPDATE devices SET state='revoked', enabled=0 WHERE id=?", (device_id,))
            self.s.event("device_revoked", "warn", device_id=device_id, detail=reason, db=db)
        for a in self._active_attempts(device_id):
            self._lose_attempt(a, "device revoked", device_revoked=True)

    # ----------------------------------------------------------------- report
    def report(self, pid):
        p = self.s.one("SELECT * FROM plans WHERE id=?", (pid,))
        if not p:
            raise ProtocolError("unknown_job", "no such plan")
        jobs = []
        for j in self.s.q("SELECT * FROM jobs WHERE plan_id=? ORDER BY created_ms, id", (pid,)):
            a = self.s.q("SELECT device_id,n,state,started_ms,finished_ms FROM attempts WHERE job_id=? ORDER BY n", (j["id"],))
            jobs.append({"job": j["id"], "op": j["op"], "state": j["state"], "note": j["note"], "attempts": [dict(x) for x in a],
                         "result": json.loads(j["result_json"]) if j["result_json"] else None,
                         "error": json.loads(j["error_json"]) if j["error_json"] else None,
                         "artifacts": [dict(r) for r in self.s.q("SELECT id,name,size,sha256,device_id FROM artifacts WHERE job_id=? AND owner_job_final=1", (j["id"],))]})
        states = {x["state"] for x in jobs}
        overall = ("succeeded" if states == {"succeeded"} else
                   "in_progress" if states & {"queued", "assigned", "running", "awaiting_approval", "cancelling"} else
                   "needs_review" if "needs_review" in states else "incomplete")
        return {"plan": pid, "title": p["title"], "plan_state": p["state"], "overall": overall, "jobs": jobs,
                "unverified": "Worker-side results are as reported by the phone; the coordinator verified artifact sizes and SHA-256 only."}
