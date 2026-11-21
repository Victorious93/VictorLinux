"""Regenerates the shared golden fixtures. Deterministic (fixed ids/timestamps). Run, review the diff, commit.
The DroidCommand worker repo carries byte-identical copies; its test checks MANIFEST.sha256."""
import hashlib, json, os
FX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "v1")
P = {"major": 1, "minor": 0}
W, D = "dev-0123456789ab", "coordinator"
sha = "a" * 64
state = {"battery_percent": 77, "charging": False, "thermal": "light", "free_storage_bytes": 12345678901, "foreground_busy": False}
def env(t, sender, target, payload, mid, **kw):
    e = {"protocol": P, "msg_id": mid, "type": t, "sender": sender, "target": target, "sent_at": 1790000000000, "payload": payload}; e.update(kw); return e
valid = {
 "enroll_request": env("enroll_request", "unenrolled", D, {"device_name": "Pixel (unrooted)", "public_key_pem": "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE-fixture-not-a-real-key\n-----END PUBLIC KEY-----\n", "pairing_proof": "fixture-secret", "signature_b64": "ZmFrZQ==", "nonce": "n-0001"}, "m-enroll-1"),
 "enroll_response": env("enroll_response", D, W, {"device_id": W, "credential": "FIXTURE-CREDENTIAL", "coordinator_fingerprint_sha256": sha * 1, "protocol_major": 1, "lease_s": 90, "heartbeat_s": 15}, "m-enroll-2", in_reply_to="m-enroll-1"),
 "hello_unrooted": env("hello", W, D, {"app_version": "droidcommand-0.0.0-fixture", "capabilities": {"operations": {"hash_files": {"available": True, "granted": True}, "apk.install": {"available": True, "granted": True, "owner_interaction_required": True}, "fs.read_private": {"available": False, "granted": False}}, "root": {"available": False, "granted": False}, "device_state": state, "max_concurrent": 2}, "running": []}, "m-hello-1"),
 "hello_rooted": env("hello", "dev-ba9876543210", D, {"app_version": "droidcommand-0.0.0-fixture", "capabilities": {"operations": {"hash_files": {"available": True, "granted": True}, "fs.read_private": {"available": True, "granted": True}}, "root": {"available": True, "granted": True, "grant_expires_ms": 1790003600000}, "device_state": state, "max_concurrent": 2}, "running": [{"job_id": "plan-x.a", "attempt": 2}]}, "m-hello-2"),
 "heartbeat": env("heartbeat", W, D, {"device_state": state}, "m-hb-1"),
 "assignment": env("assignment", D, W, {"job_id": "plan-x.hash0", "attempt": 1, "fence": 1, "op": "hash_files", "args": {"shard": "shard-0.tar", "algorithm": "sha256"}, "inputs": [{"artifact_id": "art-0001", "name": "shard-0.tar", "size": 2048, "sha256": sha, "from_job": "plan-x.stage"}], "lease_expires_ms": 1790000090000, "deadline_ms": None, "requires_root": False, "ui_exclusive": False, "idempotent": True}, "m-asg-1"),
 "ack": env("ack", W, D, {"job_id": "plan-x.hash0", "attempt": 1}, "m-ack-1"),
 "progress": env("progress", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "percent": 42.5, "note": "hashing 3/7"}, "m-prog-1"),
 "approval_needed": env("approval_needed", W, D, {"job_id": "plan-x.install", "attempt": 1, "reason": "install confirmation required on device"}, "m-appr-1"),
 "artifact_offer": env("artifact_offer", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "artifact": {"name": "manifest.json", "size": 512, "sha256": sha}}, "m-offer-1"),
 "complete": env("complete", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "result": {"files": 3}, "artifacts": [{"name": "manifest.json", "size": 512, "sha256": sha}]}, "m-done-1"),
 "fail": env("fail", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "error": {"code": "executor_unavailable", "message": "storage permission revoked"}, "side_effects": "none"}, "m-fail-1"),
 "cancel": env("cancel", D, W, {"job_id": "plan-x.hash0", "attempt": 1, "reason": "owner cancelled"}, "m-cancel-1"),
 "cancel_ack": env("cancel_ack", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "state": "stopped"}, "m-cack-1"),
 "node_message": env("node_message", W, D, {"to_device": "dev-ba9876543210", "topic": "hint", "body": {"note": "output ready"}}, "m-node-1"),
 "hello_messaging_screen_terminal": env("hello", W, D, {"app_version": "droidcommand-0.1.0-fixture", "capabilities": {"operations": {"hash_files": {"available": True, "granted": True}, "message.draft": {"available": True, "granted": True}, "screen.view": {"available": True, "granted": False}, "screen.control": {"available": False}, "terminal.open": {"available": True, "granted": True, "owner_interaction_required": True}}, "root": {"available": False, "granted": False}, "device_state": state, "max_concurrent": 2}, "running": []}, "m-hello-3"),
 "message_draft_assignment": env("assignment", D, W, {"job_id": "plan-m.draft", "attempt": 1, "fence": 1, "op": "message.draft", "args": {"draft_id": "dr-0001", "kind": "email", "to": ["jordan@example.com"], "cc": [], "bcc": [], "subject": "Hello", "body": "fixture body", "identity_hint": "work@example.com"}, "inputs": [], "lease_expires_ms": 1790000090000, "deadline_ms": 1790003600000, "requires_root": False, "ui_exclusive": True, "idempotent": False}, "m-draft-1"),
 "message_draft_progress_received": env("progress", W, D, {"job_id": "plan-m.draft", "attempt": 1, "note": "draft_received"}, "m-draft-2"),
 "message_draft_complete_submitted": env("complete", W, D, {"job_id": "plan-m.draft", "attempt": 1, "result": {"draft_id": "dr-0001", "status": "submitted"}}, "m-draft-3"),
 "message_draft_complete_declined": env("complete", W, D, {"job_id": "plan-m.draft", "attempt": 1, "result": {"draft_id": "dr-0001", "status": "declined"}}, "m-draft-4"),
 "message_draft_fail_unknown": env("fail", W, D, {"job_id": "plan-m.draft", "attempt": 1, "error": {"code": "deadline_exceeded", "message": "link lost after submit"}, "side_effects": "possible"}, "m-draft-5"),
}
bad = {
 "wrong_protocol_major": ("unsupported_protocol", dict(valid["heartbeat"], protocol={"major": 2, "minor": 0})),
 "unknown_type": ("unknown_type", dict(valid["heartbeat"], type="shell_exec")),
 "missing_payload": ("bad_request", {k: v for k, v in valid["heartbeat"].items() if k != "payload"}),
 "bad_sender_id": ("bad_request", dict(valid["heartbeat"], sender="../etc/passwd")),
 "battery_over_100": ("bad_request", env("heartbeat", W, D, {"device_state": dict(state, battery_percent=101)}, "m-bad-1")),
 "artifact_path_traversal": ("bad_request", env("complete", W, D, {"job_id": "j", "attempt": 1, "result": {}, "artifacts": [{"name": "../x", "size": 1, "sha256": sha}]}, "m-bad-2")),
 "artifact_bad_sha": ("bad_request", env("complete", W, D, {"job_id": "j", "attempt": 1, "result": {}, "artifacts": [{"name": "x", "size": 1, "sha256": "XYZ"}]}, "m-bad-3")),
 "fail_unknown_error_code": ("bad_request", env("fail", W, D, {"job_id": "j", "attempt": 1, "error": {"code": "made_up", "message": "m"}}, "m-bad-4")),
 "capability_flag_not_bool": ("bad_request", env("hello", W, D, {"app_version": "x", "capabilities": {"operations": {"hash_files": {"available": "yes"}}, "device_state": state}}, "m-bad-5")),
}
os.makedirs(os.path.join(FX, "invalid"), exist_ok=True)
w = lambda path, obj: open(path, "w").write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
for k, v in valid.items(): w(os.path.join(FX, k + ".json"), v)
for k, (code, e) in bad.items(): w(os.path.join(FX, "invalid", k + ".json"), {"expect_error": code, "envelope": e})
lines = []
for root, _, fs in os.walk(FX):
    for f in sorted(fs):
        rel = os.path.relpath(os.path.join(root, f), FX)
        if rel != "MANIFEST.sha256": lines.append(f"{hashlib.sha256(open(os.path.join(root, f),'rb').read()).hexdigest()}  {rel}")
open(os.path.join(FX, "MANIFEST.sha256"), "w").write("\n".join(sorted(lines, key=lambda l: l.split('  ')[1])) + "\n")
print(len(valid), "valid,", len(bad), "invalid fixtures")
