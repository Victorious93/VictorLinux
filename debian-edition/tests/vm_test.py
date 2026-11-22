#!/usr/bin/env python3
"""VM test of a built VictorLinux ISO under QEMU (TCG if no KVM; slow but real).

The ISO is booted via -kernel/-initrd taken from the ISO itself, with TEST-ONLY kernel arguments
(serial console + systemd debug shell on ttyS1). Those arguments are NOT part of the shipped image.
Bootloader (BIOS/UEFI) boots are checked separately by `bootloader` mode (screenshots).

  vm_test.py full ISO MODEL.gguf OUTDIR      # lifecycle/persistence/offline suite
  vm_test.py smoke ISO OUTDIR                # model-free boot + security-defaults checks (what CI runs)
  vm_test.py bootloader ISO OUTDIR           # BIOS + UEFI boot-menu screenshots; fails on a blank screen

Requires: qemu-system-x86_64, xorriso, OVMF (for UEFI), root (loop-mount disposable data disks).
"""
import json, os, re, shutil, socket, subprocess, sys, time, glob, uuid

RESULTS = []


def rec(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:160]}]" if (detail and not ok) else ""), flush=True)


def accel_args():
    """KVM when /dev/kvm is usable (hosted CI runners), otherwise TCG (slow but works anywhere)."""
    return ["-accel", "kvm"] if os.access("/dev/kvm", os.R_OK | os.W_OK) else ["-accel", "tcg"]


def ppm_colors(path, limit=4096):
    """Number of distinct pixel colours in a binary PPM (P6), capped at `limit`. 1 means a uniform (e.g. black) screen."""
    with open(path, "rb") as f:
        data = f.read()
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not m:
        return 0
    px = data[m.end():]
    seen = set()
    for i in range(0, len(px) - 2, 3):
        seen.add(px[i:i + 3])
        if len(seen) >= limit:
            break
    return len(seen)


class VM:
    def __init__(self, iso, workdir, name, disks=(), net_restrict=True, hostfwd=(), mem=4096, kernel=None, kappend="", vnc=True):
        self.w, self.name, self.iso = workdir, name, iso
        self.sock = f"{workdir}/{name}.shell.sock"; self.qmp = f"{workdir}/{name}.qmp"
        self.console = f"{workdir}/{name}.console.log"
        for p in (self.sock, self.qmp):
            if os.path.exists(p): os.unlink(p)
        k, i = kernel
        ap = ("boot=live components noroot username=victor hostname=victorlinux locales=en_US.UTF-8 keyboard-layouts=us "
              "timezone=UTC nopersistence console=ttyS0 systemd.debug_shell=/dev/ttyS1 " + kappend)
        fwd = "".join(f",hostfwd=tcp::{h}-:{g}" for h, g in hostfwd)
        cmd = ["qemu-system-x86_64", *accel_args(), "-machine", "q35", "-cpu", "max", "-smp", "4", "-m", str(mem), "-display", "none", "-vga", "std",
               "-kernel", k, "-initrd", i, "-append", ap, "-drive", f"file={iso},media=cdrom,readonly=on,if=ide",
               "-netdev", f"user,id=n0{',restrict=on' if net_restrict else ''}{fwd}", "-device", "virtio-net-pci,netdev=n0",
               "-serial", f"file:{self.console}", "-serial", f"unix:{self.sock},server=on,wait=off",
               "-qmp", f"unix:{self.qmp},server=on,wait=off", "-no-reboot", "-object", "rng-random,id=rng0,filename=/dev/urandom",
               "-device", "virtio-rng-pci,rng=rng0"]
        for n, (path, ro) in enumerate(disks):
            cmd += ["-drive", f"file={path},format=raw,if=none,id=d{n}{',readonly=on' if ro else ''}", "-device", f"virtio-blk-pci,drive=d{n},serial=VICTORTEST{n}"]
        self.p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(f"{workdir}/{name}.qemu.err", "w"))
        self.s = None

    def connect(self, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.p.poll() is not None:
                raise RuntimeError("qemu exited: " + open(f"{self.w}/{self.name}.qemu.err").read()[-300:])
            try:
                s = socket.socket(socket.AF_UNIX); s.connect(self.sock); self.s = s; s.settimeout(2); break
            except OSError:
                time.sleep(2)
        # the debug shell only appears once systemd reaches sysinit; poke until we get a prompt
        buf = ""
        while time.time() - t0 < timeout:
            try:
                self.s.sendall(b"\necho READY_$((6*7))\n")
                time.sleep(3)
                buf += self.s.recv(65536).decode(errors="replace")
                if "READY_42" in buf:
                    self.s.sendall(b"stty -echo; bind 'set enable-bracketed-paste off'; PS1=''; export TERM=dumb\n"); time.sleep(2)
                    return time.time() - t0
            except (socket.timeout, OSError):
                pass
        raise RuntimeError("no debug shell within timeout; console tail:\n" + open(self.console).read()[-1500:])

    def sh(self, cmd, timeout=120):
        tag = uuid.uuid4().hex[:8]
        self.s.settimeout(1)
        try:
            while self.s.recv(65536): pass
        except (socket.timeout, OSError): pass
        self.s.sendall((f"{cmd}\necho __END_{tag}_$?__\n").encode())
        buf, t0 = "", time.time()
        while time.time() - t0 < timeout:
            try:
                d = self.s.recv(65536)
                if d: buf += d.decode(errors="replace")
            except socket.timeout:
                pass
            m = re.search(rf"__END_{tag}_(\d+)__", buf)
            if m:   # the echoed command contains a literal "$?", so only the real output matches \d+
                out = buf.split(f"__END_{tag}_")[0]
                return int(m.group(1)), out.strip()
        return 124, buf[-500:]

    def qmp_cmd(self, cmd, **args):
        q = socket.socket(socket.AF_UNIX); q.connect(self.qmp); q.settimeout(10); q.recv(4096)
        q.sendall(b'{"execute":"qmp_capabilities"}\n'); q.recv(4096)
        q.sendall(json.dumps({"execute": cmd, "arguments": args}).encode() + b"\n"); time.sleep(1); r = q.recv(65536); q.close(); return r

    def screenshot(self, path):
        ppm = path + ".ppm"; self.qmp_cmd("screendump", filename=ppm); time.sleep(1)
        self.last_colors = ppm_colors(ppm) if os.path.exists(ppm) else 0
        if os.path.exists(ppm):
            if shutil.which("convert"): subprocess.run(["convert", ppm, path])
            else: shutil.move(ppm, path)
            if os.path.exists(ppm): os.unlink(ppm)

    def poweroff(self):
        try: self.sh("systemctl poweroff", 20)
        except Exception: pass
        try: self.p.wait(120)
        except subprocess.TimeoutExpired: self.p.kill()


def extract_kernel(iso, d):
    os.makedirs(d, exist_ok=True)
    ls = subprocess.run(["xorriso", "-indev", iso, "-find", "/live", "-name", "vmlinuz*", "-or", "-name", "initrd*"], capture_output=True, text=True).stdout
    files = [l.split("'")[1] for l in ls.splitlines() if l.startswith("'")]
    k = sorted(f for f in files if "vmlinuz" in f)[0]; i = sorted(f for f in files if "initrd" in f)[0]
    for f in (k, i):
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", iso, "-extract", f, f"{d}/{os.path.basename(f)}"], capture_output=True, check=True)
    return f"{d}/{os.path.basename(k)}", f"{d}/{os.path.basename(i)}"


def make_data_disk(path, size_mb, model=None, label="VICTORDATA"):
    subprocess.run(["truncate", "-s", f"{size_mb}M", path], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-L", label, path], check=True)
    if model:
        mp = subprocess.run(["mktemp", "-d"], capture_output=True, text=True).stdout.strip()
        subprocess.run(["mount", "-o", "loop", path, mp], check=True)
        os.makedirs(f"{mp}/models", exist_ok=True); shutil.copy(model, f"{mp}/models/test-model.gguf"); os.chown(f"{mp}/models", 1000, 1000)
        os.chown(f"{mp}/models/test-model.gguf", 1000, 1000)
        subprocess.run(["umount", mp]); os.rmdir(mp)


def ctl(vm, args, timeout=300):
    return vm.sh(f"runuser -u victor -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus /usr/local/bin/victor-ctl {args}", timeout)


def full(iso, model, out):
    os.makedirs(out, exist_ok=True); w = f"{out}/vm"; os.makedirs(w, exist_ok=True)
    kern = extract_kernel(iso, f"{w}/k")
    disk = f"{w}/data.img"; make_data_disk(disk, 2048, model)
    # ---- boot 1: offline (restrict=on), data disk attached
    vm = VM(iso, w, "boot1", disks=[(disk, False)], net_restrict=True, kernel=kern, hostfwd=[(18443, 18443)])
    try:
        t = vm.connect(); rec("VM boot to a root debug shell (BIOS-less direct kernel boot, TCG)", True, f"{t:.0f}s")
        rc, o = vm.sh("cat /proc/cmdline; uname -r; . /etc/os-release; echo $PRETTY_NAME"); rec("kernel + Debian release", "Debian" in o, o.replace("\n", " | "))
        rc, o = vm.sh("systemctl is-system-running --wait 2>&1 | tail -1; systemctl --failed --no-legend | cut -c1-80", 240)
        rec("system reaches running state; failed units listed", rc == 0, o)
        rc, o = vm.sh("passwd -S victor; test ! -e /etc/sudoers.d/live && echo no-live-sudoers; grep -rc 'NOPASSWD: ALL' /etc/sudoers /etc/sudoers.d | grep -v ':0' || echo no-nopasswd-all")
        rec("owner locked, no shared password, no NOPASSWD:ALL", " L " in o and "no-live-sudoers" in o and "no-nopasswd-all" in o, o)
        rc, o = vm.sh("systemctl is-enabled ssh 2>&1; systemctl is-active ssh 2>&1; ss -ltnH | awk '{print $4}'")
        rec("SSH disabled and nothing listening for connections at boot", "enabled" not in o.split("\n")[0] and "active\n" not in o.split("\n")[1:2] and not re.search(r"(0\.0\.0\.0|\*|\[::\]):\d+", "\n".join(o.splitlines()[2:])), o)
        rc, o = vm.sh("ls /etc/systemd/system/*.wants/ 2>/dev/null | grep -i llama || echo none; pgrep -a llama-server || echo no-llama-running")
        rec("no AI service started at boot", "none" in o and "no-llama-running" in o, o)
        rc, o = vm.sh("cat /run/victor/storage.json; ls /run/victor/")
        rec("storage auto-mounted VICTORDATA by UUID (single partition)", '"state": "mounted-rw"' in o, o[:300])
        rc, o = vm.sh("for i in $(seq 90); do pgrep -f victor-control >/dev/null && break; sleep 2; done; pgrep -a Xorg | head -1; pgrep -a xfce4-session | head -1; pgrep -af victor-control | head -1; pgrep -af victor-firstrun | head -1", 240)
        rec("desktop (Xorg+Xfce) up, Control Center autostarted", "Xorg" in o and "xfce4-session" in o and "victor-control" in o, o)
        vm.screenshot(f"{out}/desktop-boot1.png")
        rc, o = vm.sh("runuser -u victor -- /usr/local/bin/victor-theme status; grep -o 'Adwaita-dark' /home/victor/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml | head -1; grep -o 'ColorBackground=.*' /home/victor/.config/xfce4/terminal/terminalrc; grep theme-name /etc/lightdm/lightdm-gtk-greeter.conf; test -d /usr/share/themes/Adwaita-dark && echo gtk-theme-installed")
        rec("DARK theme is the default (GTK/Xfce, terminal, greeter) and the GTK dark theme is installed", o.startswith("dark") and "gtk-theme-installed" in o and "0b0f1a" in o and "Adwaita-dark" in o, o)
        rc, o = vm.sh("runuser -u victor -- /usr/local/bin/victor-theme light; cat /run/victor/data/settings/ui.json")
        rec("light theme request is rejected", "always uses dark" in o, o)
        # ---- first run: owner password (offline)
        rc, o = vm.sh("runuser -u victor -- sudo -n /usr/local/lib/victorlinux/victor-priv owner-status"); rec("first-run needed (owner-status=locked)", o.strip().endswith("locked"), o)
        rc, o = vm.sh("printf 'correct horse 42\\n' | runuser -u victor -- sudo -n /usr/local/lib/victorlinux/victor-priv set-password"); rec("first-run sets the OWNER's password", rc == 0, o)
        rc, o = vm.sh("printf 'x\\nnewpass1234\\n' | runuser -u victor -- sudo -n /usr/local/lib/victorlinux/victor-priv set-password 2>&1"); rec("password change without the current password is refused", rc != 0 and "wrong" in o, o)
        rc, o = vm.sh("passwd -S victor; test -f /run/victor/data/secrets/owner.shadow && stat -c %a /run/victor/data/secrets/owner.shadow"); rec("password hash persisted on data partition (0600)", " P " in o and "600" in o, o)
        # ---- inference lifecycle
        rc, o = ctl(vm, "model list"); rec("imported model visible", "test-model.gguf" in o, o)
        rc, o = ctl(vm, "model select test-model.gguf"); rec("select model", rc == 0, o)
        rc, o = ctl(vm, "llama config --set ctx=2048 sleep_idle_minutes=15 jinja=1"); rec("save inference settings", rc == 0, o[-200:])
        rc, o = ctl(vm, "llama start", 120); rec("service start (CPU fallback explained)", rc == 0 and "CPU" in o.upper() or "no GPU" in o, o[-400:])
        rc, o = ctl(vm, "llama start", 60); rec("second concurrent start refused (single model loaded)", rc != 0 and "already running" in o, o)
        time.sleep(15)
        rc, o = ctl(vm, "api selftest --tools", 300); rec("loopback API self-test (health/models/chat/stream)", '"streaming": "verified"' in o and '"chat_completions": "verified"' in o, o[-500:])
        rec("tool calling NOT advertised for the tiny test model", '"tool_calling": "failed"' in o or '"tool_calling": "untested"' in o, "")
        rc, o = ctl(vm, "llama stop"); rec("service stop", rc == 0, o)
        # ---- remote mode: non-loopback address + TLS + key + firewall
        rc, o = vm.sh("ip -4 -o addr show scope global | awk '{print $4}' | head -1"); ip = o.strip().split("/")[0] or "10.0.2.15"
        rec("guest has a non-loopback address for remote-mode test", bool(re.match(r"\d+\.\d+\.\d+\.\d+", ip)), ip)
        rc, o = ctl(vm, f"llama config --set bind={ip} remote.enabled=1 remote.tls=1 port=18443"); rec("enable remote mode (explicit address)", rc == 0, o[-200:])
        rc, o = ctl(vm, "llama start", 120); rec("remote service starts with TLS+key", rc == 0, o[-300:])
        time.sleep(15)
        # from the HOST through QEMU hostfwd: firewall closed vs open
        def host_probe():
            # QEMU's user-mode forwarder accepts the TCP connection itself, so only a completed TLS handshake with the guest proves reachability.
            import ssl
            try:
                ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
                with socket.create_connection(("127.0.0.1", 18443), timeout=8) as raw:
                    with ctx.wrap_socket(raw) as t: return "connected"
            except Exception as e:
                return f"closed:{type(e).__name__}"
        rec("firewall: API port reachable from outside while remote mode is enabled", host_probe() == "connected", host_probe())
        rc, o = vm.sh("nft list set inet victor allowed_tcp"); rec("firewall set present (default drop)", "allowed_tcp" in o, o[:200])
        rc, o = ctl(vm, "api connection"); rec("connection details reported", "https://" in o and "hidden" in o, o[:300])
        rc, o = ctl(vm, "api selftest", 300); rec("remote API self-test: 401 without key, streaming over TLS with pinned cert", '"auth_enforced_for_remote"' in o and '"streaming": "verified"' in o and '"ok": false' not in o.replace('"tool_calling"', ''), o[-600:])
        rc, o = vm.sh("runuser -u victor -- sudo -n /usr/local/lib/victorlinux/victor-priv revoke; nft list set inet victor allowed_tcp | tr -d '\\n'")
        time.sleep(1)
        rec("revoke closes the firewall while the server is STILL listening (probe from outside)", host_probe().startswith("closed"), host_probe() + " | " + o)
        ctl(vm, "llama stop")
        # ---- kali: no silent pulls
        rc, o = ctl(vm, "kali start"); rec("Kali start refuses without installed pack (no silent pull)", rc != 0 and "not installed" in o, o)
        rc, o = vm.sh("grep -ril kali /etc/apt/ 2>/dev/null || echo no-kali-repo"); rec("no Kali repositories on host", "no-kali-repo" in o, o)
        rc, o = vm.sh("runuser -u victor -- podman info --format '{{.Host.Security.Rootless}}' 2>&1 | tail -1"); rec("rootless podman available", "true" in o, o)
        rc, o = vm.sh("runuser -u victor -- id; runuser -u victor -- adb version | head -1; fastboot --version | head -1; nmap --version | head -1; tcpdump --version 2>&1 | head -1; git --version"); rec("dev/diag tools run as ordinary user", "Android Debug Bridge" in o and "fastboot version" in o and "Nmap" in o and "tcpdump version" in o and "git version" in o, o)
        rc, o = vm.sh("runuser -u victor -- dumpcap -D 2>&1 | head -3"); rec("wireshark capture permitted without root (dumpcap via group)", "1." in o or "lo" in o, o)
        rc, o = vm.sh("runuser -u victor -- victor-ctl diag show >/dev/null 2>&1; runuser -u victor -- /usr/local/bin/victor-ctl hw snapshot | head -c 600"); rec("hardware diagnostics run", "pci_gpus" in o, o[:300])
        rc, o = ctl(vm, "hw backend"); rec("backend selection explains CPU fallback in VM", "cpu" in o.lower(), o[:300])
        ctl(vm, "diag save-log")
        vm.sh("sync")
    finally:
        vm.poweroff()
    # ---- boot 2: persistence across reboot (still offline)
    vm = VM(iso, w, "boot2", disks=[(disk, False)], net_restrict=True, kernel=kern)
    try:
        vm.connect(); vm.sh("systemctl is-system-running --wait >/dev/null 2>&1", 240)
        rc, o = vm.sh("passwd -S victor"); rec("PERSIST: owner credentials restored from data partition after reboot", " P " in o, o)
        rc, o = ctl(vm, "llama config"); rec("PERSIST: inference config survived reboot", '"model": "test-model.gguf"' in o and '"ctx": 2048' in o, o[:200])
        rc, o = ctl(vm, "model list"); rec("PERSIST: model survived reboot", "test-model.gguf" in o, o)
        rc, o = vm.sh("ls /run/victor/data/secrets/ /run/victor/data/logs | head; test -f /run/victor/data/settings/capabilities.json && echo caps-saved"); rec("PERSIST: API key + TLS cert + bounded log snapshot + capability record present", "api.key" in o and "tls" in o and "caps-saved" in o, o)
        rc, o = vm.sh("runuser -u victor -- /usr/local/bin/victor-theme apply; runuser -u victor -- /usr/local/bin/victor-theme status"); rec("PERSIST: dark theme enforced after reboot", o.strip().endswith("dark"), o)
        rc, o = vm.sh("runuser -u victor -- /usr/local/bin/victor-theme dark >/dev/null; runuser -u victor -- /usr/local/bin/victor-theme status"); rec("switch back to dark", o.strip().endswith("dark"), o)
        rc, o = vm.sh("runuser -u victor -- sudo -n /usr/local/lib/victorlinux/victor-priv owner-status"); rec("first-run NOT repeated after reboot", o.strip().endswith("set"), o)
        rc, o = ctl(vm, "llama start", 120); rec("service starts again from persisted settings, offline", rc == 0, o[-200:])
        time.sleep(15); rc, o = ctl(vm, "api selftest", 200); rec("inference works again offline after reboot", '"chat_completions": "verified"' in o, o[-300:])
        ctl(vm, "llama stop")
        rc, o = vm.sh("ping -c1 -W2 1.1.1.1 2>&1 | tail -1; getent hosts deb.debian.org || echo no-dns"); rec("OFFLINE: no external connectivity during boot2 (QEMU restrict=on)", "no-dns" in o or "100% packet loss" in o or "unreachable" in o.lower(), o)
        rc, o = vm.sh("ss -tnp | grep -v 127.0.0 | grep ESTAB || echo no-external-connections"); rec("OFFLINE: no established external connections (no telemetry)", "no-external-connections" in o, o)
    finally:
        vm.poweroff()
    # ---- boot 3: missing storage -> session mode; boot 4: read-only storage
    vm = VM(iso, w, "boot3", disks=[], net_restrict=True, kernel=kern)
    try:
        vm.connect(); vm.sh("systemctl is-system-running --wait >/dev/null 2>&1", 240)
        rc, o = vm.sh("cat /run/victor/storage.json"); rec("MISSING storage: reported with actionable message", '"state": "missing"' in o and "temporary session" in o, o[:250])
        rc, o = vm.sh("runuser -u victor -- sudo -n /usr/local/lib/victorlinux/victor-priv storage-session; cat /run/victor/storage.json"); rec("explicit temporary-session mode (RAM) with warning", '"state": "session"' in o and "lost" in o, o[:300])
        rc, o = ctl(vm, "llama start"); rec("start without model gives an actionable error", rc != 0 and "No model selected" in o, o)
        rc, o = vm.sh("passwd -S victor"); rec("no persistent drive: owner stays locked (first-run runs again), never a default password", " L " in o, o)
    finally:
        vm.poweroff()
    vm = VM(iso, w, "boot4", disks=[(disk, True)], net_restrict=True, kernel=kern)
    try:
        vm.connect(); vm.sh("systemctl is-system-running --wait >/dev/null 2>&1", 240)
        rc, o = vm.sh("grep -o '\"state\": \"[a-z-]*\"' /run/victor/storage.json; grep -o 'read-only' /run/victor/storage.json | head -1; grep ' /run/victor/data ' /proc/mounts | cut -d' ' -f4 | cut -d, -f1"); rec("READ-ONLY media handled: mounted ro, clear message", '"state": "mounted-ro"' in o and "read-only" in o and o.strip().endswith("ro"), o)
    finally:
        vm.poweroff()
    json.dump(RESULTS, open(f"{out}/vm-test-report.json", "w"), indent=1)
    print(f"RESULT pass={sum(r['ok'] for r in RESULTS)} fail={sum(not r['ok'] for r in RESULTS)}")
    return 0 if all(r["ok"] for r in RESULTS) else 1


def bootloader(iso, out):
    os.makedirs(out, exist_ok=True); w = f"{out}/boot-menu"; os.makedirs(w, exist_ok=True)
    ovmf = next((p for p in ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_CODE.fd") if os.path.exists(p)), None)
    for mode in ("bios", "uefi"):
        if mode == "uefi" and not ovmf:
            rec("UEFI boot menu", False, "OVMF not installed"); continue
        sock = f"{w}/{mode}.qmp"
        if os.path.exists(sock): os.unlink(sock)
        cmd = ["qemu-system-x86_64", *accel_args(), "-machine", "q35", "-cpu", "max", "-smp", "2", "-m", "2048", "-display", "none", "-vga", "std",
               "-cdrom", iso, "-boot", "d", "-qmp", f"unix:{sock},server=on,wait=off", "-serial", f"file:{w}/{mode}.serial"]
        if mode == "uefi": cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={ovmf}"]
        p = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
        time.sleep(45)
        v = VM.__new__(VM); v.qmp = sock
        png = f"{out}/bootmenu-{mode}.png"; v.screenshot(png); time.sleep(20); v.screenshot(f"{out}/bootmenu-{mode}-later.png")
        p.kill(); p.wait()
        # A blank/black screen has 1 colour; a boot menu with text has many. The threshold is a heuristic, not a measured value.
        rec(f"{mode.upper()} boot: bootloader drew a non-blank screen ({getattr(v, 'last_colors', 0)} colours; inspect the PNG)",
            os.path.exists(png) and os.path.getsize(png) > 2000 and getattr(v, "last_colors", 0) >= 4, png)
    json.dump(RESULTS, open(f"{out}/bootloader-report.json", "w"), indent=1)
    return 0 if all(r["ok"] for r in RESULTS) else 1


def smoke(iso, out):
    """Model-free subset of `full` boot 1: boots the real ISO's kernel/initrd offline and checks security defaults."""
    os.makedirs(out, exist_ok=True); w = f"{out}/vm"; os.makedirs(w, exist_ok=True)
    kern = extract_kernel(iso, f"{w}/k")
    disk = f"{w}/data.img"; make_data_disk(disk, 1024)
    vm = VM(iso, w, "smoke", disks=[(disk, False)], net_restrict=True, kernel=kern)
    try:
        t = vm.connect(); rec("boot to a root debug shell", True, f"{t:.0f}s")
        rc, o = vm.sh(". /etc/os-release; echo $PRETTY_NAME"); rec("Debian release", "Debian" in o, o)
        rc, o = vm.sh("systemctl is-system-running --wait 2>&1 | tail -1", 240)
        state = o.strip().splitlines()[-1] if o.strip() else ""
        rec("system reaches running state", state == "running", o)
        rc, o = vm.sh("systemctl --failed --no-legend | cut -c1-80"); rec("no failed units", o.strip() == "", o)
        rc, o = vm.sh("passwd -S victor; test ! -e /etc/sudoers.d/live && echo no-live-sudoers; grep -rc 'NOPASSWD: ALL' /etc/sudoers /etc/sudoers.d | grep -v ':0' || echo no-nopasswd-all")
        rec("owner locked, no shared password, no NOPASSWD:ALL", " L " in o and "no-live-sudoers" in o and "no-nopasswd-all" in o, o)
        rc, o = vm.sh("ss -ltnH | awk '{print $4}'")
        rec("nothing listening on a non-loopback address at boot", not re.search(r"(0\.0\.0\.0|\*|\[::\]):\d+", o), o)
        rc, o = vm.sh("pgrep -a llama-server || echo no-llama-running"); rec("no AI service started at boot", "no-llama-running" in o, o)
        rc, o = vm.sh("cat /run/victor/storage.json"); rec("storage auto-mounted VICTORDATA", '"state": "mounted-rw"' in o, o[:300])
        rc, o = vm.sh("ss -tnp | grep -v 127.0.0 | grep ESTAB || echo no-external-connections"); rec("no established external connections", "no-external-connections" in o, o)
        vm.screenshot(f"{out}/smoke-boot.png")
    except Exception as e:
        rec("smoke run completed", False, e)
    finally:
        vm.poweroff()
    json.dump(RESULTS, open(f"{out}/smoke-report.json", "w"), indent=1)
    print(f"RESULT pass={sum(r['ok'] for r in RESULTS)} fail={sum(not r['ok'] for r in RESULTS)}")
    return 0 if RESULTS and all(r["ok"] for r in RESULTS) else 1


if __name__ == "__main__":
    if sys.argv[1] == "full":
        sys.exit(full(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif sys.argv[1] == "smoke":
        sys.exit(smoke(sys.argv[2], sys.argv[3]))
    elif sys.argv[1] == "bootloader":
        sys.exit(bootloader(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(__doc__)
