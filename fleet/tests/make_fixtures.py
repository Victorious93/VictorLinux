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
