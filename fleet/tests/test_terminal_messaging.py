"""Protocol 1.1 tests: terminal relay, one-shot, draft hand-off, capability states, revocation, no-replay, privacy.
The phone is SIMULATED (tests/simphone.py): these prove the coordinator/relay/contract, not Android or VictorSuite."""
import base64, json, os, re, sqlite3, subprocess, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from victorfleet.protocol import ProtocolError, draft_content_hash, validate_envelope, validate_worker_payload, WORKER_TYPES
from victorfleet import desktop
from simphone import SimPhone
from test_fleet import Base


def b64(s): return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


class TermBase(Base):
    def phone(self, name="p", **kw):
        w = SimPhone("127.0.0.1", self.svc.port, self.svc.fingerprint, name, **kw)
        st, body = w.enroll(self.svc.pairing_payload()["pairing_secret"])
        self.assertEqual(st, 200, body)
        self.assertEqual(w.hello()[0], 200)
        w.start()
        self.addCleanup(w.stop)
        return w

    @property
    def T(self): return self.coord.terminals

    def open(self, w, **kw):
        kw.setdefault("desktop", "victorlinux:owner@test")
        return self.T.open(w.device_id, **kw)

    def wait(self, fn, what="condition", timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def desk(self, sid, token, out=(), wait_s=0, ack=0):
        return self.T.desktop_exchange(sid, token, {"ack": ack, "chunks": list(out), "wait_s": wait_s})


class Desk:
    """Tiny desktop client over the in-process hub (the CLI uses the same Relay class over the admin socket)."""
    def __init__(self, T, sid, token):
        self.T = T
        self.rel = desktop.Relay(lambda m, p, b: T.desktop_exchange(sid, b["token"], b), sid, token)
        self.text = b""

    def pump(self, out=()):
        for c in self.rel.exchange(out):
            if c["kind"] == "data":
                self.text += base64.b64decode(c["data"])
            elif c["kind"] == "exit":
                self.rel.exit_status = c["status"]

    def send(self, s):
        self.pump([desktop.data_chunk(s if isinstance(s, bytes) else s.encode())])

    def until(self, pred, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            self.pump()
            if pred(self):
                return True
            time.sleep(0.05)
        return False


class TestCapabilityStates(TermBase):
    def test_older_peer_reports_not_supported_not_ready(self):
        w = self.worker("old")                       # ordinary 1.0 worker: no terminal/message ops
        d = self.svc.store.one("SELECT * FROM devices WHERE id=?", (w.device_id,))
        f = self.coord.features(d)
        for k in ("terminal", "shell_oneshot", "messaging_sms", "messaging_email"):
            self.assertEqual(f[k]["state"], "not_supported", k)
            self.assertIn("update", f[k]["action"])
        with self.assertRaises(ProtocolError) as cm:
            self.T.open(w.device_id, "x")
        self.assertEqual(cm.exception.code, "capability_missing")

    def test_terminal_and_messaging_are_independent(self):
        w = self.phone(ops={"message.draft.sms": {"available": False, "granted": False, "status": "permission_needed", "detail": "SMS permission not granted"},
                            "terminal.session": {"available": True, "granted": True, "status": "ready"},
                            "message.draft.email": {"available": False, "granted": False, "status": "not_installed", "detail": "VictorSMS is not installed"}})
        w.hello()
        d = self.svc.store.one("SELECT * FROM devices WHERE id=?", (w.device_id,))
        f = self.coord.features(d)
        self.assertEqual(f["terminal"]["state"], "ready")
        self.assertEqual(f["messaging_sms"]["state"], "permission_needed")
        self.assertEqual(f["messaging_email"]["state"], "not_installed")
        self.assertIn("install", f["messaging_email"]["action"])

    def test_offline_paused_and_untrusted_states(self):
        w = self.phone(ops={"shell.oneshot": {"available": False, "granted": False, "status": "untrusted", "detail": "VictorSuite signing certificate not trusted"},
                            "terminal.session": {"available": False, "granted": False, "status": "version_outdated", "detail": "VictorSuite 0.118 lacks the terminal broker"}})
        w.hello()
        d = lambda: self.svc.store.one("SELECT * FROM devices WHERE id=?", (w.device_id,))
        f = self.coord.features(d())
        self.assertEqual(f["shell_oneshot"]["state"], "untrusted")
        self.assertEqual(f["terminal"]["state"], "version_outdated")
        self.svc.store.db.execute("UPDATE devices SET paused=1 WHERE id=?", (w.device_id,))
        self.assertEqual(self.coord.features(d())["terminal"]["state"], "paused")
        self.svc.store.db.execute("UPDATE devices SET paused=0, last_seen_ms=? WHERE id=?", (1, w.device_id))
        self.assertEqual(self.coord.features(d())["terminal"]["state"], "offline")

    def test_privileged_is_a_separate_grant(self):
        w = self.phone()
        with self.assertRaises(ProtocolError) as cm:
            self.open(w, privilege="root")
        self.assertEqual(cm.exception.code, "permission_denied")
        self.assertIn("separately", str(cm.exception.message))


class TestInteractiveTerminal(TermBase):
    def test_full_session_input_output_resize_interrupt_exit(self):
        w = self.phone(marker="PHONE-A-MARKER")
        r = self.open(w, cols=100, rows=30)
        d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "open"), d.rel.state)
        d.send("ls\n")
        self.assertTrue(d.until(lambda x: b"PHONE-A-MARKER.txt" in x.text), d.text)           # output from the SELECTED host
        d.send("stty size\n")
        self.assertTrue(d.until(lambda x: b"30 100" in x.text), d.text)
        d.pump([{"kind": "resize", "cols": 120, "rows": 40}]); d.send("stty size\n")
        self.assertTrue(d.until(lambda x: b"40 120" in x.text), d.text)
        d.send("sleep 60\n"); time.sleep(0.4)
        d.send(b"\x03")                                                                       # Ctrl-C reaches the remote line discipline
        d.send("echo AFTER-INTERRUPT-$((6*7))\n")
        self.assertTrue(d.until(lambda x: b"AFTER-INTERRUPT-42" in x.text), d.text)
        d.send("exit 7\n")
        self.assertTrue(d.until(lambda x: x.rel.state == "closed"), d.rel.state)
        self.assertEqual(d.rel.exit_status, 7)

    def test_denied_never_opens_and_input_is_dropped(self):
        w = self.phone(); w.approve = False
        r = self.open(w)
        d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "denied"), d.rel.state)
        d.send("echo SHOULD-NEVER-RUN\n")
        time.sleep(0.5); d.pump()
        self.assertNotIn(b"SHOULD-NEVER-RUN", d.text)

    def test_input_typed_before_approval_is_dropped_not_queued(self):
        w = self.phone(); w.paused = True            # phone cannot see the request yet
        r = self.open(w)
        d = Desk(self.T, r["session_id"], r["attach_token"])
        d.send("echo EARLY-KEYS-$((1+1))\n")         # typed while unapproved
        w.paused = False
        self.assertTrue(d.until(lambda x: x.rel.state == "open"), d.rel.state)
        d.send("echo LATE-KEYS-$((2+2))\n")
        self.assertTrue(d.until(lambda x: b"LATE-KEYS-4" in x.text), d.text)
        self.assertNotIn(b"EARLY-KEYS-2", d.text)

    def test_approval_binding_mismatch_fails_session(self):
        w = self.phone(); w.tamper_binding = True
        r = self.open(w)
        d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "failed"), d.rel.state)
        self.assertIn("binding", d.rel.reason)

    def test_session_is_bound_to_target_device_and_attach_token(self):
        a, b = self.phone("a", marker="A-ONLY"), self.phone("b", marker="B-ONLY")
        r = self.open(a)
        with self.assertRaises(ProtocolError):
            self.T.desktop_exchange(r["session_id"], "wrong-token", {"chunks": []})
        dev_b = self.svc.store.one("SELECT * FROM devices WHERE id=?", (b.device_id,))
        with self.assertRaises(ProtocolError) as cm:
            self.T.phone_exchange(dev_b, r["session_id"], {"chunks": []})                    # another phone cannot join
        self.assertEqual(cm.exception.code, "unauthorized")
        d = Desk(self.T, r["session_id"], r["attach_token"]); self.assertTrue(d.until(lambda x: x.rel.state == "open"))
        d.send("ls\n"); self.assertTrue(d.until(lambda x: b"A-ONLY" in x.text))
        self.assertNotIn(b"B-ONLY", d.text)
        with self.assertRaises(ProtocolError) as cm:
            self.open(a)                                                                    # one live session per phone
        self.assertEqual(cm.exception.code, "device_busy")

    def test_revocation_closes_active_session(self):
        w = self.phone()
        r = self.open(w); d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "open"))
        self.coord.revoke_device(w.device_id)
        self.assertTrue(d.until(lambda x: x.rel.state == "revoked"), d.rel.state)
        d.send("echo AFTER-REVOKE\n"); time.sleep(0.4); d.pump()
        self.assertNotIn(b"AFTER-REVOKE", d.text)
        st, body, _ = w.req("POST", f"/v1/terminal/{r['session_id']}/exchange", {"ack": 0, "chunks": []})
        self.assertEqual(st, 401)                                                            # the revoked credential is dead

    def test_disconnect_ends_session_and_input_is_never_replayed(self):
        w = self.phone(marker="DISC")
        r = self.open(w); d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "open"))
        w.paused = True                                                                      # connection drops
        d.send("echo TYPED-WHILE-OFFLINE\n")
        self.clock.off += 30; self.coord.tick()                                              # grace elapsed
        d.pump(); self.assertEqual(d.rel.state, "disconnected")
        w.paused = False
        time.sleep(0.6)
        d.pump()
        self.assertNotIn(b"TYPED-WHILE-OFFLINE", d.text)                                     # never replayed on reconnect
        with self.assertRaises(ProtocolError):                                               # and the session cannot be revived or retargeted
            d2 = self.T.desktop_exchange(r["session_id"], r["attach_token"], {"chunks": [{"seq": 99, "kind": "data", "data": b64("x")}]})
            self.assertEqual(d2["state"], "disconnected"); raise ProtocolError("bad_request", "ok")

    def test_duplicate_desktop_chunk_is_delivered_once(self):
        w = self.phone(marker="DUP")
        r = self.open(w); d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "open"))
        one = {"kind": "data", "data": b64("echo DUP-$((3+4))\n"), "seq": d.rel.send_seq + 1}
        d.rel.send_seq += 1
        for _ in range(3):                                                                    # same seq re-sent (a retry)
            d.rel.call("POST", "x", {"token": r["attach_token"], "ack": d.rel.recv_seq, "chunks": [one]})
        self.assertTrue(d.until(lambda x: x.text.count(b"DUP-7") >= 1))
        time.sleep(0.5); d.pump()
        self.assertEqual(d.text.count(b"\nDUP-7"), 1, d.text)

    def test_terminal_open_is_not_redelivered(self):
        w = self.phone(); w.paused = True
        r = self.open(w)
        got = self.coord.drain(w.device_id)
        self.assertEqual([m["type"] for m in got], ["terminal_open"])
        self.assertEqual(self.coord.drain(w.device_id), [])                                   # a lost response is NOT replayed from a queue
        self.assertEqual(self.svc.store.one("SELECT COUNT(*) c FROM outbox WHERE body LIKE '%terminal_open%'")["c"], 0)

    def test_unacknowledged_request_expires(self):
        w = self.phone(); w.paused = True
        r = self.open(w)
        self.clock.off += 45; self.coord.tick()
        self.assertEqual(self.T.info(r["session_id"])["state"], "expired")

    def test_contents_never_reach_logs_or_database(self):
        w = self.phone(marker="LOGCHK")
        r = self.open(w); d = Desk(self.T, r["session_id"], r["attach_token"])
        self.assertTrue(d.until(lambda x: x.rel.state == "open"))
        d.send("echo SECRET-TERMINAL-CONTENT-$((9*9))\n")
        self.assertTrue(d.until(lambda x: b"SECRET-TERMINAL-CONTENT-81" in x.text))
        d.send("exit\n"); d.until(lambda x: x.rel.state == "closed")
        self.svc.store.db.execute("PRAGMA wal_checkpoint(FULL)")
        blob = b"".join(open(os.path.join(self.dir, f), "rb").read() for f in os.listdir(self.dir) if f.startswith("fleet.db"))
        self.assertNotIn(b"SECRET-TERMINAL-CONTENT", blob)
        self.assertNotIn(b"SECRET-TERMINAL-CONTENT", json.dumps(self.svc.store.q and [dict(x) for x in self.svc.store.q("SELECT * FROM events")]).encode())


class TestOneShot(TermBase):
    def test_oneshot_returns_real_output_and_exit_status_from_selected_host(self):
        a, b = self.phone("a", marker="ONLY-ON-A"), self.phone("b", marker="ONLY-ON-B")
        r = self.T.open(b.device_id, "victorlinux:owner@test", mode="oneshot", command="ls; echo err >&2; exit 3")
        out, err = [], []
        class Buf:
            def __init__(s, l): s.l = l
            def write(s, x): s.l.append(x)
            def flush(s): pass
        rel = desktop.Relay(lambda m, p, bd: self.T.desktop_exchange(r["session_id"], bd["token"], bd), r["session_id"], r["attach_token"])
        self.assertEqual(desktop.wait_open(rel), "open")
        status = desktop.run_oneshot(rel, Buf(out), Buf(err))
        self.assertEqual(status, 3)
        self.assertIn(b"ONLY-ON-B.txt", b"".join(out)); self.assertNotIn(b"ONLY-ON-A", b"".join(out))
        self.assertIn(b"err", b"".join(err))

    def test_oneshot_denied_runs_nothing(self):
        w = self.phone(); w.approve = False
        marker = os.path.join(w.home, "MUST-NOT-EXIST")
        r = self.T.open(w.device_id, "d", mode="oneshot", command=f"touch '{marker}'")
        rel = desktop.Relay(lambda m, p, bd: self.T.desktop_exchange(r["session_id"], bd["token"], bd), r["session_id"], r["attach_token"])
        self.assertEqual(desktop.wait_open(rel), "denied")
        time.sleep(0.4)
        self.assertFalse(os.path.exists(marker))

    def test_oneshot_takes_no_live_input_and_no_job_is_created(self):
        w = self.phone()
        before = self.svc.store.one("SELECT COUNT(*) c FROM jobs")["c"]
        r = self.T.open(w.device_id, "d", mode="oneshot", command="cat; echo done")
        self.assertEqual(self.svc.store.one("SELECT COUNT(*) c FROM jobs")["c"], before)    # not an unattended/queued job
        self.assertEqual(self.svc.store.one("SELECT COUNT(*) c FROM outbox")["c"], 0)       # nor a durable retry-queue entry


class TestDrafts(TermBase):
    def mk(self, w, channel="sms", **extra):
        f = {"to": ["+15550100"], "body": "hello from the desktop"} if channel == "sms" else {"to": ["a@example.test"], "subject": "Hi", "body": "mail body"}
        f.update(extra)
        return self.coord.drafts.create(w.device_id, channel, f, "victorlinux:owner")

    def state(self, did): return self.coord.drafts.get(did)["state"]

    def test_draft_received_but_not_sent_without_owner_action(self):
        w = self.phone(); w.decide_draft = "received"
        r = self.mk(w)
        self.wait(lambda: self.state(r["id"]) == "received", "received")
        time.sleep(0.4)
        self.assertEqual(w.submitted, [])                                                  # nothing sent by delivery alone

    def test_owner_send_submits_exactly_once_even_if_handoff_is_retried(self):
        w = self.phone(); w.decide_draft = "submitted"
        r = self.mk(w, "email")
        self.wait(lambda: self.state(r["id"]) == "submitted", "submitted")
        self.assertEqual(len(w.submitted), 1)
        with self.assertRaises(ProtocolError):
            self.coord.drafts.retry(r["id"])                                              # final: nothing to retry
        # a retry on an unknown draft re-offers the same id; the phone de-duplicates and cannot submit twice
        r2 = self.mk(w, "sms"); self.wait(lambda: self.state(r2["id"]) == "submitted")
        self.svc.store.db.execute("UPDATE drafts SET state='unknown' WHERE id=?", (r2["id"],))
        self.coord.drafts.content[r2["id"]] = ("sms", {"to": ["+15550100"], "body": "hello from the desktop"}, "o")
        self.coord.drafts.retry(r2["id"]); time.sleep(0.8)
        self.assertEqual([x[0] for x in w.submitted].count(r2["id"]), 1)

    def test_declined_sends_nothing(self):
        w = self.phone(); w.decide_draft = "declined"
        r = self.mk(w)
        self.wait(lambda: self.state(r["id"]) == "declined", "declined")
        self.assertEqual(w.submitted, [])

    def test_failed_and_unknown_are_distinct(self):
        w = self.phone(); w.decide_draft = "failed"
        r = self.mk(w, "email"); self.wait(lambda: self.state(r["id"]) == "failed", "failed")
        self.assertEqual(self.coord.drafts.get(r["id"])["detail"], "SMTP refused")
        w.paused = True
        r2 = self.mk(w)
        self.clock.off += 200; self.coord.tick()
        self.assertEqual(self.state(r2["id"]), "unknown")                                  # never acknowledged: honestly unknown
        self.assertTrue(self.coord.drafts.get(r2["id"])["can_retry"])

    def test_final_state_is_immutable(self):
        w = self.phone(); w.decide_draft = "declined"
        r = self.mk(w); self.wait(lambda: self.state(r["id"]) == "declined")
        dev = self.svc.store.one("SELECT * FROM devices WHERE id=?", (w.device_id,))
        self.coord.drafts.on_status(dev, {"draft_id": r["id"], "state": "submitted"})
        self.assertEqual(self.state(r["id"]), "declined")

    def test_body_and_recipients_are_not_stored_or_logged(self):
        w = self.phone(); w.decide_draft = "submitted"
        r = self.mk(w, "email", body="UNIQUE-BODY-TEXT-777", to=["unique-recipient@example.test"], subject="UNIQUE-SUBJECT-888")
        self.wait(lambda: self.state(r["id"]) == "submitted")
        self.svc.store.db.execute("PRAGMA wal_checkpoint(FULL)")
        blob = b"".join(open(os.path.join(self.dir, f), "rb").read() for f in os.listdir(self.dir) if f.startswith("fleet.db"))
        for needle in (b"UNIQUE-BODY-TEXT-777", b"unique-recipient@example.test", b"UNIQUE-SUBJECT-888"):
            self.assertNotIn(needle, blob)
        self.assertEqual(self.svc.store.one("SELECT COUNT(*) c FROM outbox")["c"], 0)       # not in the durable outbox either

    def test_older_phone_or_missing_app_refused_up_front(self):
        w = self.worker("old")
        with self.assertRaises(ProtocolError) as cm:
            self.coord.drafts.create(w.device_id, "sms", {"to": ["+1555"], "body": "x"}, "o")
        self.assertEqual(cm.exception.code, "capability_missing")

    def test_validation_rejects_credentials_header_injection_and_oversize(self):
        w = self.phone()
        for ch, f in (("email", {"to": ["a@b.c\r\nBcc: evil@x"], "body": "x"}), ("email", {"to": ["a@b.c"], "subject": "x\nBcc: e@x", "body": "x"}),
                      ("email", {"to": ["a@b.c"], "body": "x", "smtp_password": "hunter2"}), ("sms", {"to": [], "body": "x"}),
                      ("email", {"to": ["a@b.c"], "body": "x", "attachments": [{"name": "../x", "mime": "a/b", "data_b64": "eA=="}]})):
            with self.assertRaises(ProtocolError, msg=str(f)):
                self.coord.drafts.create(w.device_id, ch, f, "o")

    def test_restart_marks_unconfirmed_drafts_unknown(self):
        w = self.phone(); w.paused = True
        r = self.mk(w)
        self.svc.stop()
        from victorfleet.server import FleetService
        svc2 = FleetService(self.dir, {"port": 0, "plan_approval": False}, clock=self.clock)
        try:
            self.assertEqual(svc2.coord.drafts.get(r["id"])["state"], "unknown")
            with self.assertRaises(ProtocolError):
                svc2.coord.drafts.retry(r["id"])                                           # content is gone: a new draft must be created
        finally:
            svc2.store.close()
            self.svc = type("X", (), {"stop": lambda s: None})()


class TestFixtures11(unittest.TestCase):
    FX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "v1")

    def test_new_fixtures_validate_and_old_ones_still_do(self):
        names = ["terminal_open_interactive", "terminal_open_oneshot", "terminal_status_open", "terminal_close", "draft_deliver_sms",
                 "draft_deliver_email", "draft_status_submitted", "hello_terminal_capable"]
        for n in names:
            e = json.load(open(os.path.join(self.FX, n + ".json")))
            validate_envelope(e)
            from victorfleet import protocol as P
            P.validate_any_payload(e["type"], e["payload"])
            self.assertEqual(e["protocol"]["minor"], 1, n)
        for old in ("hello_unrooted", "hello_rooted", "heartbeat", "assignment"):          # unchanged 1.0 fixtures remain valid
            self.assertEqual(json.load(open(os.path.join(self.FX, old + ".json")))["protocol"]["minor"], 0)

    def test_new_invalid_cases_are_rejected(self):
        d = os.path.join(self.FX, "invalid")
        from victorfleet import protocol as P
        for f in sorted(os.listdir(d)):
            if not f.startswith("v11_"): continue
            case = json.load(open(os.path.join(d, f)))
            e = case["envelope"]
            with self.assertRaises(ProtocolError, msg=f) as cm:
                validate_envelope(e)
                P.validate_any_payload(e["type"], e["payload"])
            self.assertEqual(cm.exception.code, case["expect_error"], f)


class TestCli(TermBase):
    def run_cli(self, *args, input=None, timeout=60):
        env = dict(os.environ, VICTOR_FLEET_DIR=self.dir, PYTHONPATH=os.path.join(os.path.dirname(__file__), ".."))
        return subprocess.run([sys.executable, "-m", "victorfleet.cli", *args], capture_output=True, text=True, env=env, input=input, timeout=timeout)

    def test_cli_exec_prints_real_output_and_returns_exit_status(self):
        w = self.phone(marker="CLI-MARK")
        r = self.run_cli("exec", w.device_id, "--shell", "ls; exit 5")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("CLI-MARK.txt", r.stdout)

    def test_cli_exec_denied_exits_nonzero_and_runs_nothing(self):
        w = self.phone(); w.approve = False
        r = self.run_cli("exec", w.device_id, "--", "touch", os.path.join(w.home, "NOPE"))
        self.assertEqual(r.returncode, 4); self.assertIn("denied", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(w.home, "NOPE")))

    def test_cli_message_draft_hands_over_without_sending(self):
        w = self.phone(); w.decide_draft = "received"
        r = self.run_cli("message", "draft-email", w.device_id, "--to", "x@example.test", "--subject", "S", "--body", "B")
        self.assertEqual(r.returncode, 0, r.stderr)
        did = json.loads(r.stdout)["id"]
        self.wait(lambda: json.loads(self.run_cli("message", "status", did).stdout)["state"] == "received")
        self.assertEqual(w.submitted, [])

    def test_cli_devices_show_features(self):
        w = self.phone()
        st = json.loads(self.run_cli("devices").stdout)
        self.assertEqual(st[0]["features"]["terminal"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
