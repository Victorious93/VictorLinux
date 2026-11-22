"""Regenerates the shared golden fixtures. Deterministic (fixed ids/timestamps). Run, review the diff, commit.
The DroidCommand worker repo carries byte-identical copies; its test checks MANIFEST.sha256."""
import hashlib, json, os
FX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "v1")
P = {"major": 1, "minor": 0}
P11 = {"major": 1, "minor": 1}        # protocol 1.1 fixtures (terminal / messaging); the 1.0 files above stay byte-identical
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from victorfleet.protocol import terminal_binding_hash, draft_content_hash
W, D = "dev-0123456789ab", "coordinator"
sha = "a" * 64
state = {"battery_percent": 77, "charging": False, "thermal": "light", "free_storage_bytes": 12345678901, "foreground_busy": False}
def env(t, sender, target, payload, mid, proto=None, **kw):
    e = {"protocol": proto or P, "msg_id": mid, "type": t, "sender": sender, "target": target, "sent_at": 1790000000000, "payload": payload}; e.update(kw); return e
def pjob(n, **kw):
    j = {"job_id": f"plan-x.hash{n}", "op": "hash_files", "args": {"shard": f"shard-{n}.tar", "algorithm": "sha256"}, "requires_root": False}; j.update(kw); return j
PLAN = {"plan_id": "plan-x", "jobs": [pjob(0), pjob(1), pjob(2)]}
valid = {
 "enroll_request": env("enroll_request", "unenrolled", D, {"device_name": "Pixel (unrooted)", "public_key_pem": "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE-fixture-not-a-real-key\n-----END PUBLIC KEY-----\n", "pairing_proof": "fixture-secret", "signature_b64": "ZmFrZQ==", "nonce": "n-0001"}, "m-enroll-1"),
 "enroll_response": env("enroll_response", D, W, {"device_id": W, "credential": "FIXTURE-CREDENTIAL", "coordinator_fingerprint_sha256": sha * 1, "protocol_major": 1, "lease_s": 90, "heartbeat_s": 15}, "m-enroll-2", in_reply_to="m-enroll-1"),
 "hello_unrooted": env("hello", W, D, {"app_version": "droidcommand-0.0.0-fixture", "capabilities": {"operations": {"hash_files": {"available": True, "granted": True}, "apk.install": {"available": True, "granted": True, "owner_interaction_required": True}, "fs.read_private": {"available": False, "granted": False}}, "root": {"available": False, "granted": False}, "device_state": state, "max_concurrent": 2}, "running": []}, "m-hello-1"),
 "hello_rooted": env("hello", "dev-ba9876543210", D, {"app_version": "droidcommand-0.0.0-fixture", "capabilities": {"operations": {"hash_files": {"available": True, "granted": True}, "fs.read_private": {"available": True, "granted": True}}, "root": {"available": True, "granted": True, "grant_expires_ms": 1790003600000}, "device_state": state, "max_concurrent": 2}, "running": [{"job_id": "plan-x.a", "attempt": 2}]}, "m-hello-2"),
 "heartbeat": env("heartbeat", W, D, {"device_state": state}, "m-hb-1"),
 "assignment": env("assignment", D, W, {"job_id": "plan-x.hash0", "attempt": 1, "fence": 1, "op": "hash_files", "args": {"shard": "shard-0.tar", "algorithm": "sha256"}, "inputs": [{"artifact_id": "art-0001", "name": "shard-0.tar", "size": 2048, "sha256": sha, "from_job": "plan-x.stage"}], "lease_expires_ms": 1790000090000, "deadline_ms": None, "requires_root": False, "ui_exclusive": False, "idempotent": True}, "m-asg-1"),
 "assignment_with_plan": env("assignment", D, W, {"job_id": "plan-x.hash0", "attempt": 1, "fence": 1, "op": "hash_files", "args": {"shard": "shard-0.tar", "algorithm": "sha256"}, "inputs": [], "lease_expires_ms": 1790000090000, "deadline_ms": None, "requires_root": False, "ui_exclusive": False, "idempotent": True, "plan": PLAN}, "m-asg-2"),
 "ack": env("ack", W, D, {"job_id": "plan-x.hash0", "attempt": 1}, "m-ack-1"),
 "progress": env("progress", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "percent": 42.5, "note": "hashing 3/7"}, "m-prog-1"),
 "approval_needed": env("approval_needed", W, D, {"job_id": "plan-x.install", "attempt": 1, "reason": "install confirmation required on device"}, "m-appr-1"),
 "artifact_offer": env("artifact_offer", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "artifact": {"name": "manifest.json", "size": 512, "sha256": sha}}, "m-offer-1"),
 "complete": env("complete", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "result": {"files": 3}, "artifacts": [{"name": "manifest.json", "size": 512, "sha256": sha}]}, "m-done-1"),
 "fail": env("fail", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "error": {"code": "executor_unavailable", "message": "storage permission revoked"}, "side_effects": "none"}, "m-fail-1"),
 "cancel": env("cancel", D, W, {"job_id": "plan-x.hash0", "attempt": 1, "reason": "owner cancelled"}, "m-cancel-1"),
 "cancel_ack": env("cancel_ack", W, D, {"job_id": "plan-x.hash0", "attempt": 1, "state": "stopped"}, "m-cack-1"),
 "node_message": env("node_message", W, D, {"to_device": "dev-ba9876543210", "topic": "hint", "body": {"note": "output ready"}}, "m-node-1"),
}
SID, NONCE = "term-0123456789abcdef", "00112233445566778899aabbccddeeff"
BIND = terminal_binding_hash(SID, W, "victorlinux:owner@host", "user", "interactive", None, NONCE)
BIND1 = terminal_binding_hash("term-fedcba9876543210", W, "victorlinux:owner@host", "user", "oneshot", "ls -l", NONCE)
SMS = {"to": ["+15550100"], "body": "Running late, call you soon."}
MAIL = {"to": ["alex@example.test"], "cc": [], "bcc": [], "subject": "Notes", "body": "Notes attached.", "account_hint": "work@example.test",
        "attachments": [{"name": "notes.txt", "mime": "text/plain", "data_b64": "bm90ZXM="}]}
ops11 = {"hash_files": {"available": True, "granted": True},
         "terminal.session": {"available": True, "granted": True, "status": "ready"},
         "shell.oneshot": {"available": True, "granted": True, "status": "ready", "owner_interaction_required": True},
         "message.draft.sms": {"available": False, "granted": False, "status": "permission_needed", "detail": "SMS permission not granted"},
         "message.draft.email": {"available": False, "granted": False, "status": "not_installed", "detail": "VictorSMS is not installed", "min_version": "0.3.0"}}
valid.update({
 "hello_terminal_capable": env("hello", W, D, {"app_version": "droidcommand-0.0.0-fixture", "capabilities": {"operations": ops11, "root": {"available": False, "granted": False}, "device_state": state, "max_concurrent": 2}, "running": []}, "m-hello-3", proto=P11),
 "terminal_open_interactive": env("terminal_open", D, W, {"session_id": SID, "desktop": "victorlinux:owner@host", "mode": "interactive", "privilege": "user", "cols": 100, "rows": 30, "nonce": NONCE, "expires_ms": 1790000120000, "binding": BIND}, "m-topen-1", proto=P11),
 "terminal_open_oneshot": env("terminal_open", D, W, {"session_id": "term-fedcba9876543210", "desktop": "victorlinux:owner@host", "mode": "oneshot", "privilege": "user", "command": "ls -l", "cols": 80, "rows": 24, "nonce": NONCE, "expires_ms": 1790000120000, "binding": BIND1}, "m-topen-2", proto=P11),
 "terminal_status_open": env("terminal_status", W, D, {"session_id": SID, "state": "open", "binding": BIND}, "m-tstat-1", proto=P11),
 "terminal_close": env("terminal_close", D, W, {"session_id": SID, "reason": "closed by the owner"}, "m-tclose-1", proto=P11),
 "draft_deliver_sms": env("draft_deliver", D, W, {"draft_id": "draft-0123456789abcdef", "channel": "sms", "draft": SMS, "content_hash": draft_content_hash("sms", SMS), "origin": "victorlinux:owner", "expires_ms": 1790086400000}, "m-draft-1", proto=P11),
 "draft_deliver_email": env("draft_deliver", D, W, {"draft_id": "draft-fedcba9876543210", "channel": "email", "draft": MAIL, "content_hash": draft_content_hash("email", MAIL), "origin": "victorlinux:owner", "expires_ms": 1790086400000}, "m-draft-2", proto=P11),
 "draft_status_submitted": env("draft_status", W, D, {"draft_id": "draft-0123456789abcdef", "state": "submitted", "content_hash": draft_content_hash("sms", SMS)}, "m-dstat-1", proto=P11),
})
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
bad.update({
 "v11_terminal_open_without_binding": ("bad_request", env("terminal_open", D, W, {k: v for k, v in valid["terminal_open_interactive"]["payload"].items() if k != "binding"}, "m-bad-11", proto=P11)),
 "v11_interactive_with_preset_command": ("bad_request", env("terminal_open", D, W, dict(valid["terminal_open_interactive"]["payload"], command="rm -rf ~"), "m-bad-12", proto=P11)),
 "v11_oneshot_without_command": ("bad_request", env("terminal_open", D, W, {k: v for k, v in valid["terminal_open_oneshot"]["payload"].items() if k != "command"}, "m-bad-13", proto=P11)),
 "v11_terminal_status_open_without_binding": ("bad_request", env("terminal_status", W, D, {"session_id": SID, "state": "open"}, "m-bad-14", proto=P11)),
 "v11_terminal_status_unknown_state": ("bad_request", env("terminal_status", W, D, {"session_id": SID, "state": "root_shell_granted"}, "m-bad-15", proto=P11)),
 "v11_draft_hash_mismatch": ("bad_request", env("draft_deliver", D, W, dict(valid["draft_deliver_sms"]["payload"], content_hash="0" * 64), "m-bad-16", proto=P11)),
 "v11_draft_with_smtp_password": ("bad_request", env("draft_deliver", D, W, dict(valid["draft_deliver_email"]["payload"], draft=dict(MAIL, smtp_password="x"), content_hash=draft_content_hash("email", dict(MAIL, smtp_password="x"))), "m-bad-17", proto=P11)),
 "v11_draft_status_unknown_state": ("bad_request", env("draft_status", W, D, {"draft_id": "draft-1", "state": "sent_twice"}, "m-bad-18", proto=P11)),
 "v11_capability_status_invalid": ("bad_request", env("hello", W, D, {"app_version": "x", "capabilities": {"operations": {"terminal.session": {"available": True, "status": "mostly_ready"}}, "device_state": state}}, "m-bad-19", proto=P11)),
})
# An assignment whose `plan` is unusable. The assignment itself is still a valid assignment and must still run: a phone
# IGNORES a manifest it cannot accept (see docs/FLEET.md "Plan manifest"), it never refuses the job because of it.
def asg_with(plan, mid):
    return env("assignment", D, W, dict(valid["assignment"]["payload"], inputs=[], plan=plan), mid)
plan_ignored = {
 "plan_not_an_object": asg_with(["plan-x"], "m-plan-1"),
 "plan_no_jobs": asg_with({"plan_id": "plan-x", "jobs": []}, "m-plan-2"),
 "plan_too_many_jobs": asg_with({"plan_id": "plan-x", "jobs": [pjob(n) for n in range(26)]}, "m-plan-3"),
 "plan_duplicate_job": asg_with({"plan_id": "plan-x", "jobs": [pjob(0), pjob(0)]}, "m-plan-4"),
 "plan_args_not_object": asg_with({"plan_id": "plan-x", "jobs": [pjob(0), pjob(1, args=["sha256"])]}, "m-plan-5"),
 "plan_bad_op": asg_with({"plan_id": "plan-x", "jobs": [pjob(0), pjob(1, op="Hash Files")]}, "m-plan-6"),
 "plan_bad_job_id": asg_with({"plan_id": "plan-x", "jobs": [pjob(0), pjob(1, job_id="../x")]}, "m-plan-7"),
 "plan_bad_plan_id": asg_with({"plan_id": "x y", "jobs": [pjob(0), pjob(1)]}, "m-plan-8"),
}
os.makedirs(os.path.join(FX, "invalid"), exist_ok=True)
os.makedirs(os.path.join(FX, "plan_ignored"), exist_ok=True)
w = lambda path, obj: open(path, "w").write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
for k, v in valid.items(): w(os.path.join(FX, k + ".json"), v)
for k, e in plan_ignored.items(): w(os.path.join(FX, "plan_ignored", k + ".json"), e)
for k, (code, e) in bad.items(): w(os.path.join(FX, "invalid", k + ".json"), {"expect_error": code, "envelope": e})
lines = []
for root, _, fs in os.walk(FX):
    for f in sorted(fs):
        rel = os.path.relpath(os.path.join(root, f), FX)
        if rel != "MANIFEST.sha256": lines.append(f"{hashlib.sha256(open(os.path.join(root, f),'rb').read()).hexdigest()}  {rel}")
open(os.path.join(FX, "MANIFEST.sha256"), "w").write("\n".join(sorted(lines, key=lambda l: l.split('  ')[1])) + "\n")
print(len(valid), "valid,", len(bad), "invalid,", len(plan_ignored), "plan_ignored fixtures")
