"""Protocol 1.2: phone-microphone offer over the authenticated fleet channel. Simulated phone (tests/simphone.py).
Work by Victorious."""
import json, os, sqlite3, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from victorfleet.protocol import ProtocolError, validate_envelope
from victorfleet import protocol as P
from test_terminal_messaging import TermBase

SECRET = "ab" * 32


def offer_line(provider="keyboard", exp=None, device="nord1"):
    exp = exp or int(time.time()) + 3600
    return ";".join(["vmic1", "192.168.1.20", "47111", "s1d2", SECRET, "pc1", device, provider, "f" * 64, str(exp)])


class TestMicOffer(TermBase):
    @property
    def M(self): return self.coord.mics

    def _all_db_text(self):
        con = sqlite3.connect(self.svc.store.path) if hasattr(self.svc.store, "path") else self.svc.store.db
        rows = []
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for r in con.execute(f"SELECT * FROM {name}"):
                rows.append(json.dumps([str(x) for x in r]))
        return "\n".join(rows)

    def test_offer_reaches_phone_once_and_secret_never_persists(self):
        w = self.phone()
        line = offer_line("keyboard")
        r = self.M.offer(w.device_id, "keyboard", line, "Studio PC", (int(time.time()) + 3600) * 1000)
        self.assertEqual(r["state"], "offered")
        self.wait(lambda: self.M.get(r["id"])["state"] == "accepted", "accepted")
        self.assertEqual(list(w.mic_offers.values())[0]["offer"], line)          # delivered intact to the authenticated phone
        self.assertNotIn(SECRET, self._all_db_text())                             # not in the DB: offers, events, outbox
        self.assertEqual(self.coord.ephemeral.get(w.device_id, []), [])           # handed over, not kept for redelivery

    def test_declined_and_never_confirmed(self):
        w = self.phone(); w.decide_mic = "declined"
        r = self.M.offer(w.device_id, "droidcommand", offer_line("droidcommand"), "PC", (int(time.time()) + 3600) * 1000)
        self.wait(lambda: self.M.get(r["id"])["state"] == "declined", "declined")
        w.paused = True
        r2 = self.M.offer(w.device_id, "droidcommand", offer_line("droidcommand"), "PC", (int(time.time()) + 3600) * 1000)
        self.M.cfg["ack_timeout_s"] = 0
        self.coord.ephemeral.clear()                                               # the live hand-over lapsed
        time.sleep(0.01)
        self.assertEqual(self.M.get(r2["id"])["state"], "unknown")

    def test_older_phone_is_refused_not_degraded(self):
        w = self.worker("old")
        with self.assertRaises(ProtocolError) as cm:
            self.M.offer(w.device_id, "keyboard", offer_line(), "PC", (int(time.time()) + 3600) * 1000)
        self.assertEqual(cm.exception.code, "capability_missing")

    def test_provider_must_match_offer_and_be_unexpired(self):
        w = self.phone()
        with self.assertRaises(ProtocolError):
            self.M.offer(w.device_id, "droidcommand", offer_line("keyboard"), "PC", (int(time.time()) + 3600) * 1000)
        with self.assertRaises(ProtocolError):
            self.M.offer(w.device_id, "keyboard", offer_line(), "PC", int(time.time() * 1000) - 1)
        with self.assertRaises(ProtocolError):
            self.M.offer(w.device_id, "keyboard", "not an offer", "PC", (int(time.time()) + 3600) * 1000)

    def test_status_from_another_device_is_rejected(self):
        a, b = self.phone("a"), self.phone("b")
        b.decide_mic = "ignore"
        r = self.M.offer(a.device_id, "keyboard", offer_line(), "PC", (int(time.time()) + 3600) * 1000)
        devb = self.svc.store.one("SELECT * FROM devices WHERE id=?", (b.device_id,))
        with self.assertRaises(ProtocolError) as cm:
            self.M.on_status(devb, {"offer_id": r["id"], "state": "accepted"})
        self.assertEqual(cm.exception.code, "unauthorized")

    def test_fixtures_validate(self):
        fx = os.path.join(os.path.dirname(__file__), "..", "fixtures", "v1")
        for n in ("mic_offer_keyboard", "mic_status_accepted", "hello_mic_capable"):
            e = json.load(open(os.path.join(fx, n + ".json")))
            validate_envelope(e); P.validate_any_payload(e["type"], e["payload"])
            self.assertEqual(e["protocol"]["minor"], 2)
        for f in sorted(os.listdir(os.path.join(fx, "invalid"))):
            if f.startswith("v12_"):
                case = json.load(open(os.path.join(fx, "invalid", f)))
                with self.assertRaises(ProtocolError, msg=f) as cm:
                    validate_envelope(case["envelope"]); P.validate_any_payload(case["envelope"]["type"], case["envelope"]["payload"])
                self.assertEqual(cm.exception.code, case["expect_error"], f)


if __name__ == "__main__":
    unittest.main()
