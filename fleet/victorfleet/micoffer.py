"""Phone-microphone offers delivered over fleet messaging (protocol 1.2). Work by Victorious.

`victor-mic serve` creates a `vmic1;…` offer that contains a one-time secret. Instead of showing it for copy and paste, the
owner can hand it to a paired phone:

* The offer travels only in an ephemeral `mic_offer` message to the authenticated DroidCommand worker on that phone
  (`Coordinator.enqueue_ephemeral`): handed over at most once, never in the durable outbox, a job, the database, events
  or logs. A coordinator restart forgets it.
* The database keeps metadata only: offer id, device, provider, PC name, state, timestamps. Never the offer string.
* On the phone DroidCommand keeps it in memory for its own Microphone screen, or passes it to Victorious Keyboard over
  that app's signature-checked provider channel. Either way the owner still has to open the screen and tap Start;
  receiving an offer never starts the microphone.
* States: offered -> received -> accepted | declined | failed | expired; `unknown` when the phone never confirmed.
"""
import threading, time
from .protocol import ProtocolError, envelope, new_id, validate_mic_offer, MIC_OPS, MIC_FINAL

DEFAULTS = {"ack_timeout_s": 60}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mic_offers(
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, provider TEXT NOT NULL, pc_name TEXT NOT NULL, state TEXT NOT NULL,
  detail TEXT, created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL);
"""


class MicOfferHub:
    def __init__(self, coord, store, clock=time.time, cfg=None):
        self.coord, self.store, self.clock = coord, store, clock
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.lock = threading.RLock()
        store.db.executescript(SCHEMA)
        with store.tx() as db:
            db.execute("UPDATE mic_offers SET state='unknown', detail='coordinator restarted; check the phone', updated_ms=? "
                       "WHERE state IN ('offered','received')", (self._ms(),))

    def _ms(self):
        return int(self.clock() * 1000)

    def _row(self, oid):
        r = self.store.one("SELECT * FROM mic_offers WHERE id=?", (oid,))
        if not r:
            raise ProtocolError("unknown_job", "no such microphone offer")
        return r

    def offer(self, device_id, provider, offer, pc_name, expires_ms, read=False):
        """Hand one offer to a paired phone. `offer` is used here and in the outgoing message only."""
        payload = {"offer_id": "mic-" + new_id()[:16], "provider": provider, "offer": offer, "pc_name": pc_name,
                   "expires_ms": int(expires_ms), "read": bool(read)}
        validate_mic_offer(payload)
        dev = self.store.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not dev or dev["state"] != "paired":
            raise ProtocolError("unknown_job", "target is not a paired device")
        feat = self.coord.features(dev)["mic_" + provider]
        if feat["state"] != "ready":
            raise ProtocolError("capability_missing", f"{feat['state']}: {feat['detail']}")
        if payload["expires_ms"] <= self._ms():
            raise ProtocolError("bad_request", "the offer has already expired")
        oid = payload["offer_id"]
        with self.store.tx() as db:
            db.execute("INSERT INTO mic_offers(id,device_id,provider,pc_name,state,created_ms,expires_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?)",
                       (oid, device_id, provider, pc_name[:80], "offered", self._ms(), payload["expires_ms"], self._ms()))
            self.store.event("mic_offer_sent", device_id=device_id, detail=f"{oid} {provider}", db=db)
        self.coord.enqueue_ephemeral(device_id, envelope("mic_offer", "coordinator", device_id, payload),
                                     ttl_s=self.cfg["ack_timeout_s"])
        return dict(self._row(oid))

    def on_status(self, dev, p):
        r = self._row(p["offer_id"])
        if r["device_id"] != dev["id"]:
            raise ProtocolError("unauthorized", "that offer belongs to another device")
        if r["state"] in MIC_FINAL:
            if p["state"] != r["state"]:
                self.store.event("mic_offer_conflicting_status_ignored", "warn", device_id=dev["id"], detail=f"{r['id']} {r['state']}->{p['state']}")
            return
        with self.store.tx() as db:
            db.execute("UPDATE mic_offers SET state=?, detail=?, updated_ms=? WHERE id=?",
                       (p["state"], (p.get("detail") or "")[:200] or None, self._ms(), r["id"]))
            self.store.event("mic_offer_" + p["state"], device_id=dev["id"], detail=r["id"], db=db)

    def tick(self):
        now = self._ms()
        with self.store.tx() as db:
            db.execute("UPDATE mic_offers SET state='unknown', detail='the phone never confirmed receipt', updated_ms=? "
                       "WHERE state='offered' AND created_ms<?", (now, now - self.cfg["ack_timeout_s"] * 1000))
            db.execute("UPDATE mic_offers SET state='expired', updated_ms=? WHERE state IN ('offered','received','unknown') AND expires_ms<?",
                       (now, now))

    def list(self, limit=50):
        self.tick()
        return [dict(r) for r in self.store.q("SELECT * FROM mic_offers ORDER BY created_ms DESC LIMIT ?", (limit,))]

    def get(self, oid):
        self.tick()
        return dict(self._row(oid))
