"""Desktop-prepared SMS / email drafts handed to VictorSMS on a paired phone for owner review.

Rules (protocol 1.1):

* The coordinator *hands over a draft*. It never sends anything and holds no mail or SMS credentials: the phone chooses the
  sending identity, shows recipients, identity and body, and only the owner's Send action there can submit it.
* Delivery is ephemeral (`Coordinator.enqueue_ephemeral`): at most one delivery attempt per `deliver`, never redelivered from a
  durable queue. A retry re-offers the SAME draft_id and content hash; the phone de-duplicates by draft_id, so a retry can
  only ever re-show a draft, never submit it or create a second one.
* Message bodies, subjects, recipients and attachments live in memory only (until a final state or expiry) and are never
  written to the database, events or logs. The database keeps metadata: id, device, channel, recipient count, hash, state.
* States: pending (created, not yet confirmed received) -> received -> declined | submitted | failed. `unknown` means the
  coordinator cannot tell (never acknowledged, or no outcome in the allowed time); the owner checks the phone.
"""
import hmac, threading, time
from .protocol import (ProtocolError, envelope, new_id, validate_draft, draft_content_hash, DRAFT_FINAL)

DEFAULTS = {"ack_timeout_s": 90, "decision_ttl_s": 24 * 3600, "max_open": 20}

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts(
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, channel TEXT NOT NULL, content_hash TEXT NOT NULL, recipients INTEGER NOT NULL,
  state TEXT NOT NULL, detail TEXT, created_ms INTEGER NOT NULL, delivered_ms INTEGER, updated_ms INTEGER NOT NULL, deliveries INTEGER NOT NULL DEFAULT 0);
"""


class DraftHub:
    def __init__(self, coord, store, clock=time.time, cfg=None):
        self.coord, self.store, self.clock = coord, store, clock
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.content = {}             # draft_id -> (channel, fields)   (memory only)
        self.lock = threading.RLock()
        store.db.executescript(SCHEMA)
        # after a coordinator restart nothing in memory remains: drafts that were not final cannot be retried and are 'unknown'
        with store.tx() as db:
            db.execute("UPDATE drafts SET state='unknown', detail='coordinator restarted; check the phone', updated_ms=? "
                       "WHERE state IN ('pending','received')", (int(clock() * 1000),))

    def _ms(self):
        return int(self.clock() * 1000)

    def _row(self, did):
        r = self.store.one("SELECT * FROM drafts WHERE id=?", (did,))
        if not r:
            raise ProtocolError("unknown_job", "no such draft")
        return r

    def public(self, r):
        d = dict(r)
        d["can_retry"] = d["state"] in ("pending", "unknown") and d["id"] in self.content
        return d

    def create(self, device_id, channel, fields, origin):
        validate_draft(channel, fields)
        dev = self.store.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not dev or dev["state"] != "paired":
            raise ProtocolError("unknown_job", "target is not a paired device")
        feat = self.coord.features(dev)["messaging_" + channel]
        if feat["state"] != "ready":
            raise ProtocolError("capability_missing", f"{feat['state']}: {feat['detail']}")
        did = "draft-" + new_id()[:16]
        h = draft_content_hash(channel, fields)
        recipients = len(fields.get("to", [])) + len(fields.get("cc", [])) + len(fields.get("bcc", []))
        with self.lock:
            if sum(1 for r in self.store.q("SELECT id FROM drafts WHERE state IN ('pending','received')")) >= self.cfg["max_open"]:
                raise ProtocolError("quota_exceeded", "too many drafts awaiting review on phones")
            self.content[did] = (channel, fields, origin)
        with self.store.tx() as db:
            db.execute("INSERT INTO drafts(id,device_id,channel,content_hash,recipients,state,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?)",
                       (did, device_id, channel, h, recipients, "pending", self._ms(), self._ms()))
            self.store.event("draft_created", device_id=device_id, detail=f"{did} {channel} recipients={recipients}", db=db)
        self._deliver(did)
        return self.public(self._row(did))

    def _deliver(self, did):
        r = self._row(did)
        with self.lock:
            ch = self.content.get(did)
        if not ch:
            raise ProtocolError("bad_request", "draft content is no longer held; create a new draft")
        channel, fields, origin = ch
        env = envelope("draft_deliver", "coordinator", r["device_id"], {
            "draft_id": did, "channel": channel, "draft": fields, "content_hash": r["content_hash"], "origin": origin,
            "expires_ms": self._ms() + self.cfg["decision_ttl_s"] * 1000})
        self.coord.enqueue_ephemeral(r["device_id"], env, ttl_s=self.cfg["ack_timeout_s"])
        with self.store.tx() as db:
            db.execute("UPDATE drafts SET delivered_ms=?, deliveries=deliveries+1, updated_ms=? WHERE id=?", (self._ms(), self._ms(), did))
            self.store.event("draft_delivery_offered", device_id=r["device_id"], detail=did, db=db)

    def retry(self, did):
        """Re-offer the same draft (same id, same hash). Allowed only while nothing is known to have happened on the phone."""
        r = self._row(did)
        if r["state"] not in ("pending", "unknown"):
            raise ProtocolError("bad_request", f"draft is {r['state']}; nothing to retry")
        with self.store.tx() as db:
            db.execute("UPDATE drafts SET state='pending', detail='re-offered', updated_ms=? WHERE id=?", (self._ms(), did))
        self._deliver(did)
        return self.public(self._row(did))

    def on_status(self, dev, p):
        r = self._row(p["draft_id"])
        if r["device_id"] != dev["id"]:
            raise ProtocolError("unauthorized", "that draft belongs to another device")
        if p.get("content_hash") and not hmac.compare_digest(str(p["content_hash"]).encode(), str(r["content_hash"]).encode()):
            raise ProtocolError("bad_request", "content hash mismatch: the phone is reporting on different content")
        cur, new = r["state"], p["state"]
        if cur in DRAFT_FINAL:
            if new != cur:
                self.store.event("draft_conflicting_status_ignored", "warn", device_id=dev["id"], detail=f"{r['id']} {cur}->{new}")
            return
        if new == "received" and cur in ("pending", "unknown", "received"):
            pass
        elif new not in ("received",) and cur == "pending":
            pass                                       # phone may report a final state straight away
        with self.store.tx() as db:
            db.execute("UPDATE drafts SET state=?, detail=?, updated_ms=? WHERE id=?", (new, (p.get("detail") or "")[:200] or None, self._ms(), r["id"]))
            self.store.event("draft_" + new, device_id=dev["id"], detail=r["id"], db=db)
        if new in DRAFT_FINAL:
            with self.lock:
                self.content.pop(r["id"], None)        # content is dropped as soon as the outcome is known

    def tick(self):
        now = self._ms()
        with self.store.tx() as db:
            db.execute("UPDATE drafts SET state='unknown', detail='the phone never confirmed receipt', updated_ms=? "
                       "WHERE state='pending' AND delivered_ms IS NOT NULL AND delivered_ms<?", (now, now - self.cfg["ack_timeout_s"] * 1000))
            db.execute("UPDATE drafts SET state='unknown', detail='no decision reported within the allowed time', updated_ms=? "
                       "WHERE state='received' AND updated_ms<?", (now, now - self.cfg["decision_ttl_s"] * 1000))
        with self.lock:
            live = {r["id"] for r in self.store.q("SELECT id FROM drafts WHERE state IN ('pending','received','unknown')")}
            for k in [k for k in self.content if k not in live]:
                self.content.pop(k, None)

    def list(self, limit=50):
        self.tick()
        return [self.public(r) for r in self.store.q("SELECT * FROM drafts ORDER BY created_ms DESC LIMIT ?", (limit,))]

    def get(self, did):
        self.tick()
        return self.public(self._row(did))
