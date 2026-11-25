#!/usr/bin/env python3
"""QEMU boot test of a built VictorLinux *Arch edition* ISO (archiso). Reuses the Debian edition's harness
(debian-edition/tests/vm_test.py: VM serial/debug-shell driver, screenshots, BIOS/UEFI bootloader check).

  arch_vm_test.py smoke ISO OUTDIR       # boot the ISO's own kernel/initramfs, check login, services, Control Center
  arch_vm_test.py bootloader ISO OUTDIR  # BIOS (syslinux) + UEFI (systemd-boot) menus drawn from the real ISO

The smoke boot adds TEST-ONLY kernel arguments (serial console, systemd debug shell on ttyS1). They are not part of
the shipped boot entries. This is a VM boot, NOT a real-hardware boot: no Secure Boot, real GPUs, or physical drives.
Requires root, qemu-system-x86_64, xorriso, e2fsprogs, OVMF (UEFI).
"""
import json, os, re, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "debian-edition", "tests"))
import vm_test  # noqa: E402  (shared harness)
from vm_test import VM, RESULTS, rec, accel_args, make_data_disk  # noqa: E402

CC_PORT = 8787


class ArchVM(VM):
    """Same driver as the Debian VM, but with archiso boot arguments."""

    def __init__(self, iso, workdir, name, disks=(), mem=4096, kernel=None):
        self.w, self.name, self.iso = workdir, name, iso
        self.sock = f"{workdir}/{name}.shell.sock"; self.qmp = f"{workdir}/{name}.qmp"
        self.console = f"{workdir}/{name}.console.log"
        for p in (self.sock, self.qmp):
            if os.path.exists(p): os.unlink(p)
        k, i, install_dir = kernel
        # archisodevice=/dev/sr0: the ISO is the only optical drive in this VM.
        ap = (f"archisobasedir={install_dir} archisodevice=/dev/sr0 cow_spacesize=2G "
              "console=tty0 console=ttyS0 systemd.debug_shell=/dev/ttyS1")
        cmd = ["qemu-system-x86_64", *accel_args(), "-machine", "q35", "-cpu", "max", "-smp", "4", "-m", str(mem), "-display", "none", "-vga", "std",
               "-kernel", k, "-initrd", i, "-append", ap, "-drive", f"file={iso},media=cdrom,readonly=on,if=ide",
               "-netdev", "user,id=n0,restrict=on", "-device", "virtio-net-pci,netdev=n0",
               "-serial", f"file:{self.console}", "-serial", f"unix:{self.sock},server=on,wait=off",
               "-qmp", f"unix:{self.qmp},server=on,wait=off", "-no-reboot", "-object", "rng-random,id=rng0,filename=/dev/urandom",
               "-device", "virtio-rng-pci,rng=rng0"]
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
        rec(f"screen is not blank ({getattr(vm, 'last_colors', 0)} colours; inspect smoke-boot.png)", getattr(vm, "last_colors", 0) >= 4, "")
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


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "smoke":
        sys.exit(smoke(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 4 and sys.argv[1] == "bootloader":
        sys.exit(vm_test.bootloader(sys.argv[2], sys.argv[3]))
    sys.exit(__doc__)
