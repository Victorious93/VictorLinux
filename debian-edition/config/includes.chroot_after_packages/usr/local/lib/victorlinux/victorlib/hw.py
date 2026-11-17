"""Hardware + backend detection. Reports what is MEASURED, never what is merely installed."""
import glob, os, re
from .common import *

VENDORS = {"1002": "AMD", "10de": "NVIDIA", "8086": "Intel"}
SOFTWARE_MARKERS = ("llvmpipe", "lavapipe", "swiftshader", "softpipe")


def pci_gpus():
    out = []
    p = run(["lspci", "-nnk"], timeout=15)
    cur = None
    for line in p.stdout.splitlines():
        if line and not line[0].isspace():
            cur = None
            if re.search(r"\[(0300|0302|0380)\]", line):
                m = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
                cur = {"desc": line.split(": ", 1)[-1], "vendor": VENDORS.get(m.group(1), m.group(1)) if m else "?",
                       "pci_id": f"{m.group(1)}:{m.group(2)}" if m else "?", "driver": None}
                out.append(cur)
        elif cur is not None and "Kernel driver in use:" in line:
            cur["driver"] = line.split(":", 1)[1].strip()
    return out


def sysfs_vram():
    res = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        d = f"{card}/device"
        try:
            total = int(open(f"{d}/mem_info_vram_total").read())
            used = int(open(f"{d}/mem_info_vram_used").read())
        except (OSError, ValueError):
            continue
        busy = None
        try:
            busy = int(open(f"{d}/gpu_busy_percent").read())
        except (OSError, ValueError):
            pass
        res.append({"card": os.path.basename(card), "vram_total": total, "vram_used": used, "gpu_busy_percent": busy})
    return res


def nvidia_smi():
    p = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version",
             "--format=csv,noheader,nounits"], timeout=10)
    if p.returncode != 0:
        return []
    r = []
    for l in p.stdout.strip().splitlines():
        n, mt, mu, ut, dv = [x.strip() for x in l.split(",")]
        r.append({"name": n, "vram_total": int(mt) * 2**20, "vram_used": int(mu) * 2**20, "gpu_busy_percent": int(ut), "driver": dv})
    return r


def vulkan_devices():
    p = run(["vulkaninfo", "--summary"], timeout=30)
    devs, cur = [], {}
    for line in p.stdout.splitlines():
        m = re.match(r"\s*(\w+)\s*=\s*(.*)", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "deviceName":
            cur = {"name": v}
            devs.append(cur)
        elif cur and k in ("deviceType", "driverName", "driverInfo", "apiVersion"):
            cur[k] = v
    for d in devs:
        d["software"] = d.get("deviceType", "").endswith("CPU") or any(s in d["name"].lower() for s in SOFTWARE_MARKERS)
    return {"devices": devs, "error": None if p.returncode == 0 else (p.stderr.strip() or "vulkaninfo failed")}


def llama_devices():
    """What the bundled llama.cpp can actually use (authoritative for backend selection)."""
    if not os.path.exists(LLAMA_SERVER):
        return {"devices": [], "error": f"{LLAMA_SERVER} missing"}
    p = run([LLAMA_SERVER, "--list-devices"], timeout=60)
    devs = []
    for line in (p.stdout + p.stderr).splitlines():
        m = re.match(r"\s*(\w+\d*):\s*(.*?)\s*\((\d+) MiB,\s*(\d+) MiB free\)", line)
        if m:
            devs.append({"id": m.group(1), "name": m.group(2), "total_mib": int(m.group(3)), "free_mib": int(m.group(4))})
    return {"devices": devs, "error": None if p.returncode == 0 else p.stderr.strip()[-300:]}


def device_permissions():
    info = {"render_nodes": [], "kfd": os.path.exists("/dev/kfd")}
    for n in sorted(glob.glob("/dev/dri/renderD*")):
        st = os.stat(n)
        info["render_nodes"].append({"path": n, "mode": oct(st.st_mode & 0o777),
                                     "user_can_rw": os.access(n, os.R_OK | os.W_OK)})
    try:
        import grp
        info["user_groups"] = sorted(grp.getgrgid(g).gr_name for g in os.getgroups())
    except Exception:
        info["user_groups"] = []
    return info


def select_backend(requested, llama_devs, vk):
    """Pure policy. requested in auto|vulkan|cpu. Returns dict(backend, device, accelerated, reason)."""
    hw = [d for d in llama_devs if d["id"].lower().startswith(("vulkan", "cuda", "rocm", "hip"))]
    soft = [d for d in hw if any(s in d["name"].lower() for s in SOFTWARE_MARKERS)]
    real = [d for d in hw if d not in soft]
    if requested == "cpu":
        return {"backend": "cpu", "device": None, "accelerated": False, "reason": "CPU selected by user"}
    if real:
        d = max(real, key=lambda x: x["total_mib"])
        return {"backend": d["id"].rstrip("0123456789").lower(), "device": d["id"], "accelerated": True,
                "reason": f"{d['name']} ({d['total_mib']} MiB) found by llama.cpp"}
    why = []
    if soft:
        why.append("only a software Vulkan renderer (" + soft[0]["name"] + ") is present; it is slower than the CPU backend")
    if not hw:
        why.append("llama.cpp found no GPU device (missing /dev/dri access, driver/firmware not loaded, or "
                   "no supported GPU; run Control Center > Diagnostics)")
    why.append("falling back to CPU inference")
    return {"backend": "cpu", "device": None, "accelerated": False, "reason": "; ".join(why),
            "visible_warning": True}


def snapshot(with_llama=True):
    ld = llama_devices() if with_llama else {"devices": [], "error": "skipped"}
    vk = vulkan_devices()
    return {"ram": mem_info(), "pci_gpus": pci_gpus(), "sysfs_vram": sysfs_vram(), "nvidia_smi": nvidia_smi(),
            "vulkan": vk, "llama_devices": ld, "permissions": device_permissions(),
            "cpu": {"cores": os.cpu_count(), "model": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo")
                    if l.startswith("model name")), "?")},
            "kernel": os.uname().release}


def physical_cores():
    """Distinct (physical id, core id) pairs from /proc/cpuinfo; falls back to the logical count."""
    seen, phys, core = set(), "0", None
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                if k == "physical id":
                    phys = v
                elif k == "core id":
                    seen.add((phys, v))
    except OSError:
        pass
    return len(seen) or (os.cpu_count() or 1)


def machine_id():
    """Stable per-PC id for saved benchmark profiles (CPU model, GPU names, RAM size). Never sent anywhere."""
    from . import autotune
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next((l.split(":", 1)[1].strip() for l in f if l.startswith("model name")), "")
    except OSError:
        pass
    gpus = [d.get("name") for d in llama_devices().get("devices", [])]
    return autotune.machine_id(cpu, gpus, mem_info().get("total"))
