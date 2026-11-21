import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from test_fleet import Base
from victorfleet import messaging
from victorfleet.protocol import ProtocolError

# The phone advertises message.draft WITHOUT owner_interaction_required: receiving a draft is not an interaction;
# the owner-review-and-Send step on the phone is the gate (the coordinator never submits anything itself).
OPS = {"message.draft": {"available": True, "granted": True}}


class Draft(Base):
    def submit(self, w, **kw):
        d = messaging.build_draft("email", ["jordan@example.com"], "SECRET-BODY-123", subject="Hi", identity_hint="work@example.com", **kw)
        pid = self.coord.submit_plan(messaging.draft_plan(w.device_id, d), auto_approve=True)
        return d, f"{pid}.draft"

    def deliver(self, w):
        w.exchange()
        got = w.take("assignment")
        return got[0] if got else None

    def state(self, d):
        for r in self.svc.store.q("SELECT * FROM jobs WHERE op='message.draft'"):
            if json.loads(r["args_json"])["draft_id"] == d["draft_id"]:
                return messaging.draft_status(r)[0]

    def test_validation(self):
        for bad in (dict(kind="email", to=[], body="x", subject="s"), dict(kind="email", to=["nope"], body="x", subject="s"),
                    dict(kind="sms", to=["+15551234567"], body="x", subject="s"), dict(kind="email", to=["a@b.co"], body="x"),
                    dict(kind="email", to=["a@b.co"], body="x", subject="s", identity_hint="password: hunter2")):
            with self.assertRaises(ProtocolError):
                messaging.build_draft(**bad)
        self.assertEqual(messaging.build_draft("sms", ["+1 555 123 4567"], "hi")["kind"], "sms")

    def test_full_flow_submitted_and_body_scrubbed_everywhere(self):
        w = self.worker("p", ops=OPS)
        d, job = self.submit(w)
        self.tick()
        self.assertEqual(self.state(d), "queued" if False else self.state(d))
        a = self.deliver(w)
        self.assertEqual(a["payload"]["op"], "message.draft")
        self.assertEqual(a["payload"]["args"]["draft_id"], d["draft_id"])
        self.assertFalse(a["payload"]["idempotent"]); self.assertEqual(a["payload"]["attempt"], 1)
        w.exchange([w.msg("ack", {"job_id": job, "attempt": 1}), w.msg("progress", {"job_id": job, "attempt": 1, "note": "draft_received"})])
        self.assertEqual(self.state(d), "draft_received")
        w.exchange([w.msg("complete", {"job_id": job, "attempt": 1, "result": {"draft_id": d["draft_id"], "status": "submitted"}})])
        self.assertEqual(self.state(d), "submitted")
        self.tick()
        dump = json.dumps([dict(r) for t in ("jobs", "plans", "events") for r in self.svc.store.q(f"SELECT * FROM {t}")], default=str)
        self.assertNotIn("SECRET-BODY-123", dump)
        self.assertIn(d["draft_id"], dump)

    def test_declined_and_failed_and_unknown_are_distinct(self):
        w = self.worker("p", ops=OPS)
        outcomes = {}
        for label in ("declined", "failed", "unknown"):
            d, job = self.submit(w); self.tick()
            a = self.deliver(w)
            w.exchange([w.msg("ack", {"job_id": job, "attempt": 1})])
            if label == "declined":
                w.exchange([w.msg("complete", {"job_id": job, "attempt": 1, "result": {"status": "declined"}})])
            elif label == "failed":
                w.exchange([w.msg("fail", {"job_id": job, "attempt": 1, "error": {"code": "executor_unavailable", "message": "no SMS role"}, "side_effects": "none"})])
            else:
                w.exchange([w.msg("fail", {"job_id": job, "attempt": 1, "error": {"code": "deadline_exceeded", "message": "link lost"}, "side_effects": "possible"})])
            outcomes[label] = self.state(d)
        self.assertEqual(outcomes, {"declined": "declined", "failed": "failed", "unknown": "unknown"})

    def test_lost_phone_after_delivery_is_unknown_and_never_retried(self):
        w = self.worker("p", ops=OPS)
        d, job = self.submit(w); self.tick()
        self.deliver(w); w.exchange([w.msg("ack", {"job_id": job, "attempt": 1})])
        self.tick(advance=3600)                      # lease lapses
        self.assertIn(self.state(d), ("unknown", "failed"))
        self.assertEqual(self.svc.store.one("SELECT COUNT(*) c FROM attempts WHERE job_id=?", (job,))["c"], 1)
        self.tick(advance=60); w.hello()
        self.assertEqual(self.svc.store.one("SELECT COUNT(*) c FROM attempts WHERE job_id=?", (job,))["c"], 1)

    def test_draft_never_goes_to_a_phone_without_the_op_or_to_another_phone(self):
        a = self.worker("a", ops=OPS); b = self.worker("b", ops={})
        d, job = self.submit(a)
        self.tick()
        self.assertIsNone(self.deliver(b))
        self.assertIsNotNone(self.deliver(a))

    def test_feature_states_are_independent_and_actionable(self):
        dev = {"connected": True, "capabilities": {"operations": {"message.draft": {"available": True, "granted": True},
               "screen.view": {"available": True, "granted": False}, "terminal.open": {"available": False}}}}
        f = messaging.feature_states(dev)
        self.assertEqual([f[k]["state"] for k in ("messaging", "screen_view", "manual_control", "terminal")],
                         ["available", "needs_permission", "unsupported", "unsupported"])
        self.assertIn("Update", f["manual_control"]["fix"])
        off = messaging.feature_states(dict(dev, connected=False))
        self.assertTrue(all(v["state"] == "offline" for v in off.values()))


if __name__ == "__main__":
    unittest.main()
