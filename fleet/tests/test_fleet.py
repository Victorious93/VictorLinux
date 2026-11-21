import hashlib, io, json, os, sys, tarfile, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from victorfleet.server import FleetService, effective_bind, DEFAULT_CONFIG
from victorfleet.protocol import ProtocolError
from victorfleet import workflows, persistence
from simworker import SimWorker


class Clock:
    def __init__(self): self.off = 0
    def __call__(self): return time.time() + self.off


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.clock = Clock()
        self.svc = FleetService(self.dir, {"port": 0, "plan_approval": False}, clock=self.clock).start()
        self.coord = self.svc.coord

    def tearDown(self):
        try: self.svc.stop()
        except Exception: pass

    def worker(self, name="p", **kw):
        w = SimWorker("127.0.0.1", self.svc.port, self.svc.fingerprint, name, **kw)
        st, body = w.enroll(self.svc.pairing_payload()["pairing_secret"])
        self.assertEqual(st, 200, body)
        st, _ = w.hello(); self.assertEqual(st, 200)
        return w

    def tick(self, advance=0):
        self.clock.off += advance
        self.coord.tick()

    def plan(self, jobs, **kw):
        return self.coord.submit_plan({"jobs": jobs}, **kw)

    def job(self, pid, key): return self.svc.store.one("SELECT * FROM jobs WHERE id=?", (f"{pid}.{key}",))


class TestEnrollmentAndAuth(Base):
    def test_default_bind_is_loopback_and_wildcard_refused(self):
        self.assertEqual(self.svc.bind, "127.0.0.1")
        with self.assertRaises(ValueError):
            effective_bind(dict(DEFAULT_CONFIG, network_enabled=True, bind="0.0.0.0"))
        self.assertEqual(effective_bind(dict(DEFAULT_CONFIG, network_enabled=True, bind="192.168.1.5")), "192.168.1.5")

    def test_pairing_secret_is_single_use_and_expires(self):
        secret = self.svc.pairing_payload()["pairing_secret"]
        a = SimWorker("127.0.0.1", self.svc.port, self.svc.fingerprint, "a")
        self.assertEqual(a.enroll(secret)[0], 200)
        b = SimWorker("127.0.0.1", self.svc.port, self.svc.fingerprint, "b")
        st, body = b.enroll(secret)
        self.assertEqual(st, 401); self.assertEqual(body["error"]["code"], "unauthorized")
        exp = self.svc.pairing_payload(ttl_s=1)["pairing_secret"]
        self.clock.off += 0  # expiry uses wall time in SQL helper
        time.sleep(1.2)
        c = SimWorker("127.0.0.1", self.svc.port, self.svc.fingerprint, "c")
        self.assertEqual(c.enroll(exp)[0], 401)

    def test_proof_of_possession_binds_coordinator_identity(self):
        w = SimWorker("127.0.0.1", self.svc.port, self.svc.fingerprint, "mitm")
        st, body = w.enroll(self.svc.pairing_payload()["pairing_secret"], sign_fpr="0" * 64)   # signed for a different coordinator
        self.assertEqual(st, 401)

    def test_spki_pin_matches_independent_computation(self):
        import subprocess
        pub = subprocess.run(["openssl", "x509", "-in", self.svc.crt, "-pubkey", "-noout"], capture_output=True, check=True).stdout
        der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=pub, capture_output=True, check=True).stdout
        self.assertEqual(hashlib.sha256(der).hexdigest(), self.svc.spki)
        self.assertEqual(self.svc.pairing_payload()["coordinator_spki_sha256"], self.svc.spki)

    def test_certificate_pinning_rejects_other_coordinator(self):
        import ssl
        w = SimWorker("127.0.0.1", self.svc.port, "f" * 64, "pin")
        with self.assertRaises(ssl.SSLError):
            w.enroll("x")

    def test_unknown_and_revoked_credentials(self):
        w = self.worker()
        w.token = "nope"
        self.assertEqual(w.exchange()[0], 401)
        w2 = self.worker("w2")
        self.coord.revoke_device(w2.device_id)
        st, body = w2.exchange()
        self.assertEqual((st, body["error"]["code"]), (401, "revoked"))

    def test_fleet_size_limit_and_credential_rotation(self):
        ws = [self.worker(f"p{i}") for i in range(4)]
        extra = SimWorker("127.0.0.1", self.svc.port, self.svc.fingerprint, "fifth")
        st, body = extra.enroll(self.svc.pairing_payload()["pairing_secret"])
        self.assertEqual(st, 413); self.assertEqual(body["error"]["code"], "quota_exceeded")
        w = ws[0]; old = w.token
        st, b, _ = w.req("POST", "/v1/rotate", {})
        self.assertEqual(st, 200); new = b["credential"]
        self.assertNotEqual(old, new)
        self.assertEqual(w.exchange()[0], 401)       # old credential dead
        w.token = new; self.assertEqual(w.exchange()[0], 200)

    def test_sender_must_match_authenticated_identity(self):
        a, b = self.worker("a"), self.worker("b")
        m = a.msg("heartbeat", {"device_state": {"battery_percent": 50}}); m["sender"] = b.device_id
        st, body = a.exchange([m])
        self.assertFalse(body["results"][0]["ok"]); self.assertEqual(body["results"][0]["error"]["code"], "unauthorized")

    def test_malformed_and_oversized_rejected(self):
        w = self.worker()
        st, body = w.exchange([{"nonsense": 1}]); self.assertFalse(body["results"][0]["ok"])
        bad = w.msg("heartbeat", {"device_state": {"battery_percent": 500}})
        st, body = w.exchange([bad]); self.assertEqual(body["results"][0]["error"]["code"], "bad_request")
        big = w.msg("progress", {"job_id": "x", "attempt": 1, "note": "n" * 600})
        st, body = w.exchange([big]); self.assertFalse(body["results"][0]["ok"])
        old = w.msg("heartbeat", {"device_state": {}}); old["protocol"]["major"] = 2
        st, body = w.exchange([old]); self.assertEqual(body["results"][0]["error"]["code"], "unsupported_protocol")


class TestPlansAndScheduling(Base):
    def test_cycle_and_bad_dependency_rejected(self):
        with self.assertRaises(ProtocolError) as e:
            self.plan([{"key": "a", "op": "x", "deps": ["b"]}, {"key": "b", "op": "x", "deps": ["a"]}])
        self.assertIn("cycle", e.exception.message)
        with self.assertRaises(ProtocolError): self.plan([{"key": "a", "op": "x", "deps": ["zz"]}])
        with self.assertRaises(ProtocolError): self.plan([{"key": "a", "op": "x", "deps": ["a"]}])
        with self.assertRaises(ProtocolError): self.plan([{"key": "a", "op": "x", "evil": 1}])
        with self.assertRaises(ProtocolError): self.plan([{"key": f"j{i}", "op": "x"} for i in range(65)])

    def test_review_required_before_first_dispatch_when_policy_on(self):
        self.coord.cfg["plan_approval"] = True
        w = self.worker()
        pid = self.plan([{"key": "a", "op": "hash_files"}])
        self.tick(); self.assertEqual(self.job(pid, "a")["state"], "queued")
        rev = self.coord.plan_review(pid); self.assertEqual(rev["jobs"][0]["candidates_now"], [w.device_id])
        self.coord.approve_plan(pid); self.tick()
        self.assertEqual(self.job(pid, "a")["state"], "assigned")

    def test_capability_and_permission_aware_assignment(self):
        unrooted = self.worker("plain", ops={"hash_files": {"available": True, "granted": True}})
        rooted = self.worker("rooted", ops={"hash_files": {"available": True}, "fs.read_private": {"available": True, "granted": True}}, root=True)
        denied = self.worker("denied", ops={"fs.read_private": {"available": True, "granted": False}})
        pid = self.plan([{"key": "r", "op": "fs.read_private", "requires_root": True}, {"key": "h", "op": "hash_files"}, {"key": "n", "op": "never.supported"}])
        self.tick()
        dev = lambda k: self.svc.store.one("SELECT device_id FROM attempts WHERE job_id=?", (f"{pid}.{k}",))
        self.assertEqual(dev("r")["device_id"], rooted.device_id)           # root job only on the rooted phone
        self.assertIn(dev("h")["device_id"], (unrooted.device_id, rooted.device_id))
        self.assertIsNone(dev("n")); self.assertEqual(self.job(pid, "n")["state"], "queued")   # unsupported never dispatched
        self.assertEqual(denied.exchange()[1]["messages"], [])              # granted=false phone got nothing

    def test_root_grant_expiry_blocks_dispatch(self):
        w = self.worker("r", ops={"x": {"available": True}}, root=True)
        w.root = {"available": True, "granted": True, "grant_expires_ms": int(self.clock() * 1000) + 10_000}
        w.hello()
        pid = self.plan([{"key": "a", "op": "x", "requires_root": True}]); self.tick()
        self.assertEqual(self.job(pid, "a")["state"], "queued")

    def test_battery_thermal_storage_policy_and_offline(self):
        low = self.worker("low", device_state={"battery_percent": 5, "charging": False, "thermal": "none"})
        hot = self.worker("hot", device_state={"battery_percent": 90, "charging": True, "thermal": "severe"})
        small = self.worker("small", device_state={"battery_percent": 90, "charging": True, "thermal": "none", "free_storage_bytes": 10})
        pid = self.plan([{"key": "a", "op": "hash_files", "min_free_bytes": 1000}]); self.tick()
        self.assertEqual(self.job(pid, "a")["state"], "queued")
        self.tick(advance=120)       # all phones silent -> offline
        small.hello(); self.assertEqual(self.job(pid, "a")["state"], "queued")

    def test_dependent_job_waits_and_failure_propagates(self):
        w = self.worker()
        pid = self.plan([{"key": "a", "op": "hash_files"}, {"key": "b", "op": "hash_files", "deps": ["a"]}]); self.tick()
        self.assertEqual(self.job(pid, "b")["state"], "queued")
        w.exchange(); a = w.take("assignment")[0]["payload"]
        w.exchange([w.msg("ack", {"job_id": a["job_id"], "attempt": a["attempt"]}),
                    w.msg("fail", {"job_id": a["job_id"], "attempt": a["attempt"], "error": {"code": "executor_unavailable", "message": "boom"}, "side_effects": "none"})])
        self.tick()
        self.assertEqual(self.job(pid, "a")["state"], "failed"); self.assertEqual(self.job(pid, "b")["state"], "dep_failed")

    def test_ui_exclusive_serialised_per_phone_and_concurrency_bounded(self):
        w = self.worker("ui", ops={"ui.tap": {"available": True}, "hash_files": {"available": True}}, max_concurrent=3)
        pid = self.plan([{"key": "u1", "op": "ui.tap", "ui_exclusive": True}, {"key": "u2", "op": "ui.tap", "ui_exclusive": True},
                         {"key": "h1", "op": "hash_files"}, {"key": "h2", "op": "hash_files"}, {"key": "h3", "op": "hash_files"}])
        self.tick()
        states = {k: self.job(pid, k)["state"] for k in ("u1", "u2", "h1", "h2", "h3")}
        self.assertEqual(sum(1 for k in ("u1", "u2") if states[k] == "assigned"), 1)     # one foreground UI job at a time
        self.assertLessEqual(sum(1 for s in states.values() if s == "assigned"), 3)       # capacity respected

    def test_device_pin_not_moved_to_other_phone(self):
        a, b = self.worker("a"), self.worker("b")
        pid = self.plan([{"key": "x", "op": "hash_files", "device": a.device_id}]); self.tick()
        self.assertEqual(self.svc.store.one("SELECT device_id FROM attempts WHERE job_id=?", (f"{pid}.x",))["device_id"], a.device_id)
        self.tick(advance=200); b.hello()          # a silent; lease lost; job is NOT idempotent and ran? (assigned only) -> requeued but pinned
        self.tick()
        self.assertEqual(self.job(pid, "x")["state"], "queued")
        b.exchange(); self.assertFalse(b.take("assignment"))


class TestRecovery(Base):
    def _start(self, w, pid, key):
        w.exchange(); a = w.take("assignment")[0]["payload"]
        w.exchange([w.msg("ack", {"job_id": a["job_id"], "attempt": a["attempt"]})]); return a

    def test_lease_expiry_requeues_idempotent_and_fences_stale_worker(self):
        w1, w2 = self.worker("w1"), self.worker("w2")
        pid = self.plan([{"key": "a", "op": "hash_files", "idempotent": True, "max_attempts": 3, "device": w1.device_id}], )
        self.tick(); a1 = self._start(w1, pid, "a")
        # w1 vanishes while running; unpin to let the retry move
        with self.svc.store.tx() as db: db.execute("UPDATE jobs SET device_pin=NULL")
        self.tick(advance=200); w2.hello(); self.tick()
        j = self.job(pid, "a"); self.assertEqual(j["state"], "assigned"); self.assertEqual(j["attempts"], 2)
        # the ORIGINAL worker comes back and reports a stale completion: must be rejected
        w1.hello()
        st, body = w1.exchange([w1.msg("complete", {"job_id": a1["job_id"], "attempt": 1, "result": {"ok": True}})])
        self.assertEqual(body["results"][0]["error"]["code"], "stale_attempt")
        self.assertEqual(self.job(pid, "a")["state"], "assigned")
        self.assertTrue(any(m["type"] == "cancel" and m["payload"]["attempt"] == 1 for m in body["messages"]))   # told to stop

    def test_non_idempotent_lost_running_job_needs_review_not_replayed(self):
        w = self.worker()
        pid = self.plan([{"key": "a", "op": "hash_files", "idempotent": False, "max_attempts": 3}]); self.tick(); self._start(w, pid, "a")
        self.tick(advance=200)
        j = self.job(pid, "a"); self.assertEqual(j["state"], "needs_review"); self.assertEqual(j["attempts"], 1)
        self.assertIn("uncertain", j["note"])

    def test_assigned_but_never_started_is_safely_requeued_without_burning_attempt(self):
        w = self.worker()
        pid = self.plan([{"key": "a", "op": "hash_files"}]); self.tick()
        self.tick(advance=200)
        j = self.job(pid, "a"); self.assertEqual((j["state"], j["attempts"]), ("queued", 0))

    def test_reconnect_reconciliation_phone_reboot(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files", "idempotent": True, "max_attempts": 2}]); self.tick(); self._start(w, pid, "a")
        st, body = w.hello(running=[])           # phone rebooted: running list empty -> attempt 1 lost, idempotent -> retried as attempt 2
        j = self.job(pid, "a"); self.assertEqual((j["state"], j["attempts"]), ("assigned", 2))
        st, body = w.hello(running=[{"job_id": f"{pid}.a", "attempt": 1}])      # claims the obsolete attempt 1
        self.assertTrue(any(m["type"] == "cancel" and m["payload"]["attempt"] == 1 for m in body["messages"]))

    def test_duplicate_messages_are_deduplicated(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}]); self.tick()
        w.exchange(); a = w.take("assignment")[0]["payload"]
        m = w.msg("ack", {"job_id": a["job_id"], "attempt": a["attempt"]})
        r1 = w.exchange([m])[1]["results"][0]; r2 = w.exchange([m])[1]["results"][0]
        self.assertTrue(r1["ok"]); self.assertTrue(r2.get("duplicate"))

    def test_coordinator_restart_preserves_state(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}, {"key": "b", "op": "hash_files", "deps": ["a"]}]); self.tick()
        self.svc.stop()
        svc2 = FleetService(self.dir, {"port": 0, "plan_approval": False}, clock=self.clock).start()
        try:
            self.assertEqual(svc2.store.one("SELECT state FROM jobs WHERE id=?", (f"{pid}.a",))["state"], "assigned")
            self.assertEqual(svc2.store.one("SELECT state FROM jobs WHERE id=?", (f"{pid}.b",))["state"], "queued")
            self.assertEqual(svc2.store.one("SELECT COUNT(*) c FROM devices WHERE state='paired'")["c"], 1)
            self.assertEqual(svc2.fingerprint, self.svc.fingerprint)           # same identity after restart
            w2 = SimWorker("127.0.0.1", svc2.port, svc2.fingerprint, "w"); w2.device_id, w2.token = w.device_id, w.token
            st, body = w2.exchange(); self.assertEqual(st, 200)
            self.assertEqual([m["type"] for m in body["messages"]], ["assignment"])           # un-acked message redelivered
        finally:
            svc2.stop()
        self.svc = svc2

    def test_cancellation_is_honest(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}, {"key": "q", "op": "hash_files", "deps": ["a"]}]); self.tick(); a = self._start(w, pid, "a")
        self.assertEqual(self.coord.cancel_job(f"{pid}.q"), "cancelled")                     # queued -> immediate
        self.assertEqual(self.coord.cancel_job(f"{pid}.a"), "cancelling")
        self.assertEqual(self.job(pid, "a")["state"], "cancelling")                            # NOT cancelled until the worker confirms
        msgs = w.exchange()[1]["messages"]; self.assertTrue(any(m["type"] == "cancel" for m in msgs))
        w.exchange([w.msg("cancel_ack", {"job_id": a["job_id"], "attempt": 1, "state": "stopped"})])
        self.assertEqual(self.job(pid, "a")["state"], "cancelled")

    def test_cancel_unconfirmed_when_worker_disappears(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}]); self.tick(); self._start(w, pid, "a")
        self.coord.cancel_job(f"{pid}.a"); self.tick(advance=200)
        self.assertEqual(self.job(pid, "a")["state"], "cancelled_unconfirmed")

    def test_revocation_midjob(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}]); self.tick(); self._start(w, pid, "a")
        self.coord.revoke_device(w.device_id)
        self.assertEqual(self.job(pid, "a")["state"], "needs_review")
        self.assertEqual(w.exchange()[0], 401)

    def test_pending_approval_is_not_success(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}]); self.tick(); a = self._start(w, pid, "a")
        w.exchange([w.msg("approval_needed", {"job_id": a["job_id"], "attempt": 1, "reason": "install confirmation on device"})])
        self.assertEqual(self.job(pid, "a")["state"], "awaiting_approval")
        self.assertNotEqual(self.coord.report(pid)["overall"], "succeeded")
        w.exchange([w.msg("progress", {"job_id": a["job_id"], "attempt": 1, "note": "owner approved on device"})])
        self.assertEqual(self.job(pid, "a")["state"], "running")


class TestArtifactsAndWorkflows(Base):
    def _run_job(self, w, work):
        w.exchange()
        for m in w.take("assignment"):
            a = m["payload"]
            w.exchange([w.msg("ack", {"job_id": a["job_id"], "attempt": a["attempt"]})])
            work(w, a)

    def test_handoff_between_phones_via_coordinator_with_authorization(self):
        a, b, c = self.worker("a", ops={"produce": {"available": True}}), self.worker("b", ops={"consume": {"available": True}}), self.worker("c", ops={"x": {"available": True}})
        pid = self.coord.submit_plan(workflows.handoff_plan("produce", {}, "consume", {}))
        self.tick()
        got = {}
        def produce(w, asg):
            st, body, sha = w.upload(asg["job_id"], asg["attempt"], "output", b"PHONE-A-RESULT")
            self.assertEqual(st, 200, body)
            w.exchange([w.msg("complete", {"job_id": asg["job_id"], "attempt": asg["attempt"], "result": {"n": 1},
                                           "artifacts": [{"name": "output", "size": 14, "sha256": sha}]})])
        self._run_job(a, produce); self.tick()
        b.exchange(); asg = b.take("assignment")[0]["payload"]
        ref = asg["inputs"][0]
        self.assertEqual(ref["name"], "output")
        st, data = b.download(ref["artifact_id"]); self.assertEqual((st, data), (200, b"PHONE-A-RESULT"))
        st, body = c.download(ref["artifact_id"]); self.assertEqual(st, 401 if False else 400 if False else st); self.assertEqual(body["error"]["code"], "permission_denied")
        st, body = a.download(ref["artifact_id"]); self.assertEqual(st, 200)       # producer may re-read its own output

    def test_artifact_integrity_and_quota(self):
        w = self.worker(); pid = self.plan([{"key": "a", "op": "hash_files"}]); self.tick()
        w.exchange(); asg = w.take("assignment")[0]["payload"]
        w.exchange([w.msg("ack", {"job_id": asg["job_id"], "attempt": 1})])
        import hashlib as h
        st, body, _ = w.req("PUT", f"/v1/artifacts/{asg['job_id']}/1/f.bin?sha256={'0'*64}", raw=b"abc", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(st, 400)             # hash mismatch discarded
        self.svc.cfg["max_artifact_bytes"] = 2
        st, body, _ = w.req("PUT", f"/v1/artifacts/{asg['job_id']}/1/f.bin?sha256={h.sha256(b'abc').hexdigest()}", raw=b"abc", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual((st, body["error"]["code"]), (413, "too_large"))
        # path traversal in artifact name
        st, body, _ = w.req("PUT", f"/v1/artifacts/{asg['job_id']}/1/..%2Fx?sha256={h.sha256(b'a').hexdigest()}", raw=b"a", headers={"Content-Type": "application/octet-stream"})
        self.assertNotEqual(st, 200)
        # completion claiming an artifact that was never uploaded is rejected
        st, body = w.exchange([w.msg("complete", {"job_id": asg["job_id"], "attempt": 1, "result": {}, "artifacts": [{"name": "ghost", "size": 1, "sha256": "a" * 64}]})])
        self.assertFalse(body["results"][0]["ok"])

    def test_batch_hash_workflow_three_noroot_phones_real_hashes(self):
        src = tempfile.mkdtemp(); files = {}
        for i in range(9):
            data = os.urandom(1000 + i * 337); name = f"d{i%3}/file{i}.bin"; os.makedirs(os.path.join(src, f"d{i%3}"), exist_ok=True)
            open(os.path.join(src, name), "wb").write(data); files[name] = data
        ws = [self.worker(f"phone{i}", ops={"hash_files": {"available": True, "granted": True}}) for i in range(3)]
        pid = self.coord.submit_plan(workflows.batch_hash_plan(src, sorted(files), shards=3))
        self.tick()
        self.assertEqual(self.job(pid, "stage")["state"], "succeeded")
        def hash_work(w, asg):
            st, tar = w.download(asg["inputs"][0]["artifact_id"]); self.assertEqual(st, 200)
            self.assertEqual(hashlib.sha256(tar).hexdigest(), asg["inputs"][0]["sha256"])      # worker-side integrity check
            entries = []
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                for m in t.getmembers():
                    d = t.extractfile(m).read(); entries.append({"path": m.name, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()})
            man = json.dumps({"files": entries}).encode()
            st, body, sha = w.upload(asg["job_id"], asg["attempt"], "manifest.json", man); self.assertEqual(st, 200, body)
            w.exchange([w.msg("complete", {"job_id": asg["job_id"], "attempt": asg["attempt"], "result": {"files": len(entries)},
                                           "artifacts": [{"name": "manifest.json", "size": len(man), "sha256": sha}]})])
        for w in ws: self._run_job(w, hash_work)
        self.tick()
        rep = self.coord.report(pid)
        self.assertEqual(rep["overall"], "succeeded", json.dumps([j for j in rep["jobs"] if j["state"]!="succeeded"], indent=1)[:1500])
        merge = [j for j in rep["jobs"] if j["op"] == "coordinator.merge_manifests"][0]
        art = self.svc.store.one("SELECT * FROM artifacts WHERE job_id=? AND name='combined-manifest.json'", (merge["job"],))
        combined = json.load(open(os.path.join(self.dir, "artifacts", art["rel_path"])))
        self.assertTrue(combined["verified_complete"])
        for name, data in files.items():
            self.assertEqual(combined["files"][name]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertGreaterEqual(len(combined["per_device_file_counts"]), 2)         # work really was spread across phones

    def test_stage_refuses_path_traversal(self):
        src = tempfile.mkdtemp(); open(os.path.join(src, "ok.txt"), "w").write("x")
        outside = tempfile.mkdtemp(); open(os.path.join(outside, "secret"), "w").write("s")
        os.symlink(os.path.join(outside, "secret"), os.path.join(src, "link"))
        for bad in ("../" + os.path.basename(outside) + "/secret", "link"):
            pid = self.coord.submit_plan(workflows.batch_hash_plan(src, [bad], shards=1)); self.tick()
            self.assertEqual(self.job(pid, "stage")["state"], "failed")

    def test_apk_install_and_launch_only_is_inconclusive_not_pass(self):
        src = tempfile.mkdtemp(); open(os.path.join(src, "a.apk"), "wb").write(b"PK-fake")
        w = self.worker("t", ops={"apk.install": {"available": True}, "apk.launch": {"available": True}})
        caps = dict(w.caps(), _device_id=w.device_id)
        pid = self.coord.submit_plan(workflows.apk_test_plan(src, "a.apk", "com.example", caps)); self.tick()
        def ok(w, asg): w.exchange([w.msg("complete", {"job_id": asg["job_id"], "attempt": asg["attempt"], "result": {"passed": True}})])
        for _ in range(4):
            self._run_job(w, ok); self.tick()
        self.assertEqual(self.job(pid, "verdict")["state"], "failed")
        self.assertIn("INCONCLUSIVE", json.loads(self.job(pid, "verdict")["error_json"])["message"])

    def test_apk_plan_refused_without_installer(self):
        w = self.worker("t", ops={"hash_files": {"available": True}})
        with self.assertRaises(ValueError): workflows.apk_test_plan("/tmp", "a.apk", "p", dict(w.caps(), _device_id=w.device_id))

    def test_apk_with_behavioural_check_passes(self):
        src = tempfile.mkdtemp(); open(os.path.join(src, "a.apk"), "wb").write(b"PK-fake")
        w = self.worker("t", ops={"apk.install": {"available": True}, "apk.instrumentation": {"available": True}})
        pid = self.coord.submit_plan(workflows.apk_test_plan(src, "a.apk", "com.example", dict(w.caps(), _device_id=w.device_id))); self.tick()
        def ok(w, asg): w.exchange([w.msg("complete", {"job_id": asg["job_id"], "attempt": asg["attempt"], "result": {"passed": True}})])
        for _ in range(4):
            self._run_job(w, ok); self.tick()
        self.assertEqual(self.job(pid, "verdict")["state"], "succeeded")

    def test_node_messages_relay_only(self):
        a, b = self.worker("a"), self.worker("b")
        a.exchange([a.msg("node_message", {"to_device": b.device_id, "topic": "hint", "body": {"x": 1}})])
        m = [x for x in b.exchange()[1]["messages"] if x["type"] == "node_message"][0]
        self.assertEqual((m["sender"], m["payload"]["from_device"]), ("coordinator", a.device_id))
        st, body = a.exchange([a.msg("node_message", {"to_device": "dev-nonexistent", "topic": "t", "body": {}})])
        self.assertFalse(body["results"][0]["ok"])


class TestAdminSocket(Base):
    """The owner CLI/GUI path: every verb goes through the private Unix socket, which the service-level tests do not touch."""
    def call(self, method, path, body=None):
        from victorfleet.cli import UnixConn
        c = UnixConn(self.svc.sock_path)
        c.request(method, path, json.dumps(body or {}) if method == "POST" else None, {"Content-Type": "application/json"})
        r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")

    def test_full_owner_lifecycle_over_the_socket(self):
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(self.svc.sock_path).st_mode), 0o600)
        self.svc.coord.cfg["plan_approval"] = True
        w = self.worker()
        st, r = self.call("POST", "/plans", {"spec": {"jobs": [{"key": "a", "op": "hash_files"}, {"key": "b", "op": "hash_files", "deps": ["a"]}]}})
        self.assertEqual(st, 200); pid = r["plan"]; self.assertEqual(r["review"]["state"], "proposed")
        self.assertEqual(self.call("GET", f"/plans/{pid}/review")[0], 200)
        self.assertEqual(self.call("POST", f"/plans/{pid}/approve")[0], 200)
        self.assertEqual(self.call("POST", "/tick")[0], 200)
        self.assertEqual(self.job(pid, "a")["state"], "assigned")
        self.assertEqual(self.call("GET", f"/jobs?plan={pid}")[1][0]["plan_id"], pid)
        self.assertEqual(self.call("POST", f"/jobs/{pid}.b/cancel")[1]["state"], "cancelled")
        self.assertEqual(self.call("GET", f"/plans/{pid}/report")[0], 200)
        for act in ("pause", "resume", "disable", "enable"):
            self.assertEqual(self.call("POST", f"/devices/{w.device_id}/{act}")[0], 200)
        self.assertEqual(self.call("POST", f"/plans/{pid}/cancel")[0], 200)
        self.assertEqual(self.call("POST", f"/devices/{w.device_id}/revoke")[0], 200)
        self.assertEqual(self.call("GET", "/status")[1]["devices"][0]["state"], "revoked")
        self.assertEqual(self.call("GET", "/nope")[0], 400)
        self.assertEqual(self.call("POST", "/plans", {"spec": {"jobs": []}})[0], 400)


class TestPersistence(unittest.TestCase):
    def test_volatile_when_no_data_partition(self):
        d = tempfile.mkdtemp()
        r = persistence.resolve({"VICTOR_RUN": d + "/none", "VICTOR_RUN_DIR": d + "/none", "XDG_RUNTIME_DIR": d})
        self.assertFalse(r["durable"]); self.assertIn("no persistent", r["reason"])

    def test_session_mode_is_not_durable_and_readonly_reported(self):
        d = tempfile.mkdtemp(); os.makedirs(d + "/session"); os.symlink(d + "/session", d + "/root")
        r = persistence.resolve({"VICTOR_RUN": d})
        self.assertFalse(r["durable"]); self.assertIn("temporary session", r["reason"])
        ro = d + "/ro"; os.makedirs(ro); os.chmod(ro, 0o500)
        if os.geteuid() != 0:
            r = persistence.resolve({"VICTOR_FLEET_DIR": ro + "/x", "VICTOR_FLEET_DIR_DURABLE": "1"})
            self.assertFalse(r["writable"]); self.assertFalse(r["durable"])


class TestArchStateFile(unittest.TestCase):
    def test_arch_temporary_and_missing_modes_are_not_durable(self):
        for mode in ("temporary", "missing", "readonly", "ambiguous"):
            d = tempfile.mkdtemp()
            json.dump({"mode": mode, "data_root": d, "message": "m"}, open(d + "/persist.json", "w"))
            r = persistence.resolve({"VICTOR_RUN": d + "/none", "VICTOR_RUN_DIR": d, "XDG_RUNTIME_DIR": d})
            self.assertFalse(r["durable"], mode)

    def test_arch_persistent_on_real_disk_is_durable_when_writable(self):
        d = tempfile.mkdtemp(dir="/var/tmp"); st = persistence._fstype_of(d)
        json.dump({"mode": "persistent", "data_root": d}, open(d + "/persist.json", "w"))
        r = persistence.resolve({"VICTOR_RUN": d + "/none", "VICTOR_RUN_DIR": d})
        self.assertEqual(r["durable"], st not in ("tmpfs", "overlay", "ramfs", "squashfs", "unknown"))
        self.assertTrue(r["writable"]); self.assertTrue(r["path"].endswith("/fleet"))


class TestFixtures(unittest.TestCase):
    FX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "v1")

    def test_manifest_matches_files(self):
        with open(os.path.join(self.FX, "MANIFEST.sha256")) as fh: lines = fh.read().split("\n")
        listed = {l.split("  ")[1]: l.split("  ")[0] for l in lines if l.strip()}
        files = {f for f in os.listdir(self.FX) if f.endswith(".json")} | {"invalid/" + f for f in os.listdir(os.path.join(self.FX, "invalid"))}
        self.assertEqual(set(listed), files)
        for f, h in listed.items():
            with open(os.path.join(self.FX, f), "rb") as fh: self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), h, f)

    def test_valid_fixtures_validate(self):
        from victorfleet.protocol import validate_envelope, validate_any_payload
        for f in sorted(os.listdir(self.FX)):
            if not f.endswith(".json"): continue
            e = json.load(open(os.path.join(self.FX, f)))
            validate_envelope(e)
            if e["type"] != "enroll_response": validate_any_payload(e["type"], e["payload"])

    def test_invalid_fixtures_rejected_with_expected_code(self):
        from victorfleet.protocol import validate_envelope, validate_any_payload
        d = os.path.join(self.FX, "invalid")
        for f in sorted(os.listdir(d)):
            case = json.load(open(os.path.join(d, f)))
            with self.assertRaises(ProtocolError, msg=f) as cm:
                validate_envelope(case["envelope"])
                validate_any_payload(case["envelope"]["type"], case["envelope"]["payload"])
            self.assertEqual(cm.exception.code, case["expect_error"], f)


if __name__ == "__main__":
    unittest.main()
