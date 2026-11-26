#!/usr/bin/env python3
"""QEMU boot test of a built VictorLinux *Arch edition* ISO (archiso). Reuses the Debian edition's harness
(debian-edition/tests/vm_test.py: VM serial/debug-shell driver, screenshots, BIOS/UEFI bootloader check).

  arch_vm_test.py smoke ISO OUTDIR       # boot the ISO's own kernel/initramfs, check login, services, Control Center
  arch_vm_test.py bootloader ISO OUTDIR  # BIOS (syslinux) + UEFI (systemd-boot) menus drawn from the real ISO
  arch_vm_test.py full ISO MODEL.gguf OUTDIR  # offline chat, authentication and a reboot after a USB update

The smoke boot adds TEST-ONLY kernel arguments (serial console, systemd debug shell on ttyS1). They are not part of
the shipped boot entries. This is a VM boot, NOT a real-hardware boot: no Secure Boot, real GPUs, or physical drives.
Requires root, qemu-system-x86_64, xorriso, e2fsprogs, OVMF (UEFI).
"""
import hashlib, json, os, re, shlex, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "debian-edition", "tests"))
import vm_test  # noqa: E402  (shared harness)
from vm_test import VM, RESULTS, rec, accel_args, make_data_disk  # noqa: E402

CC_PORT = 8787
REPO = Path(__file__).resolve().parents[1]


class ArchVM(VM):
    """Same driver as the Debian VM, but with archiso boot arguments."""

    def __init__(self, iso, workdir, name, disks=(), mem=4096, kernel=None, boot_disk=None):
        self.w, self.name, self.iso = workdir, name, iso
        self.sock = f"{workdir}/{name}.shell.sock"; self.qmp = f"{workdir}/{name}.qmp"
        self.console = f"{workdir}/{name}.console.log"
        for p in (self.sock, self.qmp):
            if os.path.exists(p): os.unlink(p)
        k, i, install_dir = kernel
        boot_device = "/dev/vda" if boot_disk else "/dev/sr0"
        ap = (f"archisobasedir={install_dir} archisodevice={boot_device} cow_spacesize=2G "
              "console=tty0 console=ttyS0 systemd.debug_shell=/dev/ttyS1")
        cmd = ["qemu-system-x86_64", *accel_args(), "-machine", "q35", "-cpu", "max", "-smp", "4", "-m", str(mem), "-display", "none", "-vga", "std",
               "-kernel", k, "-initrd", i, "-append", ap,
               "-netdev", "user,id=n0,restrict=on", "-device", "virtio-net-pci,netdev=n0",
               "-serial", f"file:{self.console}", "-serial", f"unix:{self.sock},server=on,wait=off",
               "-qmp", f"unix:{self.qmp},server=on,wait=off", "-no-reboot", "-object", "rng-random,id=rng0,filename=/dev/urandom",
               "-device", "virtio-rng-pci,rng=rng0"]
        if boot_disk:
            cmd += ["-drive", f"file={boot_disk},format=raw,if=none,id=media", "-device", "virtio-blk-pci,drive=media"]
        else:
            cmd += ["-drive", f"file={iso},media=cdrom,readonly=on,if=ide"]
        for n, (path, ro) in enumerate(disks):
            cmd += ["-drive", f"file={path},format=raw,if=none,id=d{n}{',readonly=on' if ro else ''}", "-device", f"virtio-blk-pci,drive=d{n},serial=VICTORTEST{n}"]
        self.p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(f"{workdir}/{name}.qemu.err", "w"))
        self.s = None


def extract_kernel(iso, d):
    """archiso layout: /<install_dir>/boot/x86_64/{vmlinuz-linux,initramfs-linux.img}."""
    os.makedirs(d, exist_ok=True)
    ls = subprocess.run(["xorriso", "-indev", iso, "-find", "/", "-name", "vmlinuz-linux", "-or", "-name", "initramfs-linux.img"],
                        capture_output=True, text=True).stdout
    files = [l.split("'")[1] for l in ls.splitlines() if l.startswith("'")]
    k = next(f for f in files if f.endswith("/vmlinuz-linux")); i = next(f for f in files if f.endswith("/initramfs-linux.img"))
    for f in (k, i):
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", iso, "-extract", f, f"{d}/{os.path.basename(f)}"], capture_output=True, check=True)
    return f"{d}/vmlinuz-linux", f"{d}/initramfs-linux.img", k.strip("/").split("/")[0]


def smoke(iso, out):
    os.makedirs(out, exist_ok=True); w = f"{out}/vm"; os.makedirs(w, exist_ok=True)
    kern = extract_kernel(iso, f"{w}/k")
    disk = f"{w}/data.img"; make_data_disk(disk, 1024)
    vm = ArchVM(iso, w, "smoke", disks=[(disk, False)], kernel=kern)
    try:
        t = vm.connect(); rec("boot to a root debug shell", True, f"{t:.0f}s")
        rc, o = vm.sh(". /etc/os-release; echo $PRETTY_NAME"); rec("VictorLinux Arch edition release", "VictorLinux (Arch edition)" in o, o)
        rc, o = vm.sh("systemctl is-system-running --wait 2>&1 | tail -1", 300)
        state = o.strip().splitlines()[-1] if o.strip() else ""
        rec("system reaches running state", state == "running", o)
        rc, o = vm.sh("systemctl --failed --no-legend --plain | cut -c1-100"); rec("no failed units", o.strip() == "", o)
        rc, o = vm.sh("systemctl is-active victor-boot-setup.service systemd-networkd.service")
        rec("key services active (victor-boot-setup, systemd-networkd)", o.split() == ["active", "active"], o)
        rc, o = vm.sh("for i in $(seq 60); do loginctl list-sessions --no-legend | grep -q 'victor.*tty1' && break; sleep 2; done; loginctl list-sessions --no-legend", 180)
        rec("tty1 autologin as victor", re.search(r"victor.*tty1", o) is not None, o)
        rc, o = vm.sh("test -x /usr/bin/openbox && echo IS_GUI || echo IS_HEADLESS"); gui = "IS_GUI" in o
        if gui:
            rc, o = vm.sh("for i in $(seq 90); do pgrep -u victor -f control_center.py >/dev/null && break; sleep 2; done; "
                          "pgrep -a Xorg | head -1; pgrep -a openbox | head -1; pgrep -u victor -af control_center.py | head -1", 240)
            rec("desktop (Xorg + Openbox) up, Control Center autostarted as victor", "Xorg" in o and "openbox" in o and "control_center.py" in o, o)
        else:
            vm.sh("runuser -u victor -- sh -c 'cd ~ && nohup victor-control-center >/tmp/cc.log 2>&1 &'")
        rc, o = vm.sh(f"for i in $(seq 60); do c=$(curl -s -o /dev/null -m 3 -w '%{{http_code}}' http://127.0.0.1:{CC_PORT}/); "
                      f"[ \"$c\" = 200 ] && break; sleep 2; done; echo code=$c", 200)
        rec(f"Control Center answers HTTP 200 on 127.0.0.1:{CC_PORT}", "code=200" in o, o)
        rc, o = vm.sh(f"curl -s -o /dev/null -m 3 -w '%{{http_code}}' http://127.0.0.1:{CC_PORT}/api/status; echo")
        rec("Control Center API refuses a call without the session token (401)", "401" in o, o)
        rc, o = vm.sh("passwd -S victor; passwd -S root; grep -rc 'NOPASSWD: ALL' /etc/sudoers /etc/sudoers.d | grep -v ':0' || echo no-nopasswd-all")
        rec("victor and root locked, no NOPASSWD:ALL",
            bool(re.search(r"^victor L ", o, re.M) and re.search(r"^root L ", o, re.M)) and "no-nopasswd-all" in o, o)
        rc, o = vm.sh("systemctl is-active sshd.service 2>&1; ss -ltnH | awk '{print $4}'")
        lines = o.splitlines()
        rec("sshd off and nothing listening on a non-loopback address",
            bool(lines) and lines[0].strip() != "active" and not re.search(r"(0\.0\.0\.0|\*|\[::\]):\d+", "\n".join(lines[1:])), o)
        rc, o = vm.sh("pgrep -a llama-server || echo no-llama-running"); rec("no AI service started at boot", "no-llama-running" in o, o)
        vm.screenshot(f"{out}/smoke-boot.png")
        rec(f"screen is not blank ({getattr(vm, 'last_colors', 0)} colours; inspect smoke-boot.png)", getattr(vm, "last_colors", 0) > 1, "")
    except Exception as e:
        rec("smoke run completed", False, e)
    finally:
        try:
            _, o = vm.sh("journalctl -b -p warning --no-pager | tail -n 300", 60)
            with open(f"{w}/journal-warnings.log", "w") as f:
                f.write(o)
        except Exception:
            pass
        vm.poweroff()
    json.dump(RESULTS, open(f"{out}/smoke-report.json", "w"), indent=1)
    print(f"RESULT pass={sum(r['ok'] for r in RESULTS)} fail={sum(not r['ok'] for r in RESULTS)}")
    return 0 if RESULTS and all(r["ok"] for r in RESULTS) else 1


def usb_action(action, iso, disk):
    """Invoke the real writer only on a loop device backed by this test's file."""
    dev = subprocess.check_output(["losetup", "--find", "--show", "--partscan", str(disk)], text=True).strip()
    if not re.fullmatch(r"/dev/loop[0-9]+", dev):
        raise RuntimeError("Refusing to write to anything other than a loop device")
    try:
        backing = subprocess.check_output(["losetup", "-lnO", "BACK-FILE", dev], text=True).strip()
        if Path(backing).resolve() != disk.resolve():
            raise RuntimeError("Unexpected loop backing file")
        args = ["bash", str(REPO / "scripts/make-usb.sh"), action, "--iso", iso, "--device", dev, "--confirm-serial", "LOOPTEST"]
        if action == "write":
            args += ["--data-gib", "2", "--headroom-mib", "256"]
        subprocess.run(args, env=dict(os.environ, VICTOR_USB_TEST_LOOP="1"), check=True)
    finally:
        subprocess.run(["losetup", "--detach", dev], check=True)


def cc_json(vm, route, payload=None):
    cmd = 'curl -fsS --max-time 90 -H "X-Victor-Token: $(cat /home/victor/.cache/victorlinux/cc.token)"'
    if payload is not None:
        cmd += " -H 'Content-Type: application/json' --data " + shlex.quote(json.dumps(payload))
    rc, output = vm.sh(f"{cmd} {shlex.quote(f'http://127.0.0.1:{CC_PORT}{route}')}", 120)
    if rc != 0:
        raise RuntimeError(f"Control Center request failed at {route}: {output}")
    return json.loads(output)


def start_model(vm):
    started = cc_json(vm, "/api/llm/start", {})
    if not started.get("ok"):
        raise RuntimeError("Control Center could not start the fixture model: " + str(started.get("error")))
    rc, out = vm.sh("curl -fsS --max-time 3 --retry 30 --retry-all-errors --retry-delay 2 http://127.0.0.1:8080/victor/health", 90)
    if rc != 0:
        raise RuntimeError("Inference gateway did not become ready: " + out)


def verify_local_completion(vm):
    request = json.dumps({"model": "victor-local", "messages": [{"role": "user", "content": "Once upon a time"}], "max_tokens": 8})
    rc, out = vm.sh("curl -fsS --max-time 45 -H 'Content-Type: application/json' "
                   f"--data {shlex.quote(request)} http://127.0.0.1:8080/v1/chat/completions", 60)
    response = json.loads(out) if rc == 0 else {}
    choices = response.get("choices", [])
    rec("CPU llama-server produces a chat completion offline", bool(choices and choices[0].get("message", {}).get("content")), out if not choices else "")


def lifecycle_boot(iso, w, disk, kernel, fixture, digest, after_update):
    name = "after-update" if after_update else "first-boot"
    vm = ArchVM(iso, w, name, disks=[(str(fixture), True)], kernel=kernel, boot_disk=disk)
    prompt = "VictorLinux USB persistence check"
    vault = {"passphrase": "test fixture passphrase"}
    try:
        vm.connect()
        rc, out = vm.sh(f"curl -fsS --max-time 3 --retry 45 --retry-all-errors --retry-delay 2 http://127.0.0.1:{CC_PORT}/ >/dev/null", 150)
        if rc != 0:
            raise RuntimeError("Control Center did not open on the booted GUI image: " + out)
        status = cc_json(vm, "/api/status")
        root = status.get("data_root")
        if status.get("persist", {}).get("mode") != "persistent" or not root:
            raise RuntimeError("Expected VICTORDATA on the VM boot disk")
        rec(f"{name}: Control Center opened with persistent storage", True)
        model = f"{root}/models/test-model.gguf"
        settings = {"model": model, "ctx": 512, "ctx_auto": False, "backend": "auto"}
        marker = shlex.quote(f"{root}/documents/vm-update-check.txt")
        if after_update:
            rec("model path and inference settings survived update", all(status["settings"].get(k) == v for k, v in settings.items()))
            rc, out = vm.sh(f"sha256sum {shlex.quote(model)} && cat {marker}")
            rec("model bytes and document survived update", rc == 0 and out.startswith(digest + " ") and out.splitlines()[-1] == prompt)
            cc_json(vm, "/api/chats/unlock", vault)
            exported = cc_json(vm, "/api/chats/export?fmt=md")
            rec("encrypted chat history survived update", prompt in exported.get("text", ""))
            start_model(vm)
            verify_local_completion(vm)
            return

        rc, out = vm.sh("mkdir -p /mnt/victor-vm-fixture && mount -o ro /dev/disk/by-label/VICTORVM /mnt/victor-vm-fixture"
                        " && runuser -u victor -- victor-model-import /mnt/victor-vm-fixture/models/test-model.gguf"
                        f" --sha256 {shlex.quote(digest)}")
        if rc != 0:
            raise RuntimeError("Fixture model import failed: " + out)
        rec("GGUF import verifies the source checksum", "matches expected" in out)
        cc_json(vm, "/api/settings", settings)
        start_model(vm)
        rec("VM selects the CPU backend", cc_json(vm, "/api/status")["backend"]["selected"] == "cpu")
        verify_local_completion(vm)
        cc_json(vm, "/api/chats/setup", {**vault, "confirm": vault["passphrase"]})
        cc_json(vm, "/api/chats/settings", {"settings": {"max_tokens": 8}})
        chat = cc_json(vm, "/api/chats/send", {"message": prompt})
        rec("Control Center saves the chat and the local reply", any(m.get("role") == "assistant" and m.get("content") for m in chat.get("messages", [])))
        write_document = f"printf '%s\\n' {shlex.quote(prompt)} > {marker}"
        rc, out = vm.sh(f"runuser -u victor -- sh -c {shlex.quote(write_document)}")
        rec("write a document on the persistent partition", rc == 0, out)

        key = cc_json(vm, "/api/remote/new-key", {})["api_key"]
        remote = cc_json(vm, "/api/remote", {"enabled": True, "bind": "0.0.0.0", "tls": False})
        rc, out = vm.sh("curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8080/v1/models")
        rec("remote inference rejects missing authentication", remote.get("ok") and rc == 0 and out == "401", out)
        rc, out = vm.sh(f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 10 -H {shlex.quote('Authorization: Bearer ' + key)} http://127.0.0.1:8080/v1/models")
        rec("remote inference accepts the generated API key", rc == 0 and out == "200", out)
        cc_json(vm, "/api/remote/revoke", {})
    finally:
        vm.poweroff()


def full(iso, model, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="victor-vm-") as tmp:
            w = str(out / "vm")
            kern = extract_kernel(iso, f"{w}/k")
            disk = Path(tmp) / "usb.img"
            with disk.open("xb") as stream:
                stream.truncate(Path(iso).stat().st_size + (4 << 30))
            fixture = Path(tmp) / "fixture.img"
            make_data_disk(str(fixture), 128, model, label="VICTORVM")
            with open(model, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            usb_action("write", iso, disk)
            lifecycle_boot(iso, w, disk, kern, fixture, digest, after_update=False)
            usb_action("update", iso, disk)
            rec("the in-place USB writer verified the ISO payload and kept the data partition", True)
            lifecycle_boot(iso, w, disk, kern, fixture, digest, after_update=True)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        rec("Arch lifecycle completed", False, exc)
    (out / "lifecycle-report.json").write_text(json.dumps(RESULTS, indent=2) + "\n")
    return 0 if RESULTS and all(r["ok"] for r in RESULTS) else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "smoke":
        sys.exit(smoke(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 4 and sys.argv[1] == "bootloader":
        sys.exit(vm_test.bootloader(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 5 and sys.argv[1] == "full":
        sys.exit(full(sys.argv[2], sys.argv[3], sys.argv[4]))
    sys.exit(__doc__)
