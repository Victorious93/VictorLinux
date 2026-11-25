"""Coral Edge TPU support (both editions): vision and wake-word ONLY, with Edge TPU models the owner supplies.

A Coral Edge TPU cannot run chat models (LLMs): it only runs small 8-bit TensorFlow Lite models compiled for it (a few
MB of on-chip memory). VictorLinux uses it for image classification/detection and wake-word spotting, nothing else.

OFF by default. Nothing is downloaded: the Edge TPU runtime (libedgetpu) and a TFLite runtime (tflite_runtime or
ai_edge_litert) must be installed by the owner, and models (*_edgetpu.tflite, optional labels .txt) are copied into the
coral folder on the data partition. No cloud fallback: if the TPU or runtime is missing, the feature says so and stops.
"""
import glob
import json
import os
import re

USB_IDS = {("1a6e", "089a"): "Coral USB Accelerator (firmware not loaded yet)",
           ("18d1", "9302"): "Coral USB Accelerator"}
TASKS = ("vision", "wake")
DEFAULTS = {"enabled": False, "vision_model": "", "wake_model": "", "wake_label": "", "wake_threshold": 0.8}
EDGETPU_OP = b"edgetpu-custom-op"
DELEGATES = ("libedgetpu.so.1", "libedgetpu.so.1.0", "edgetpu.dll", "libedgetpu.1.dylib")


class CoralError(Exception):
    pass


def load(path):
    c = dict(DEFAULTS)
    try:
        with open(path) as f:
            c.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return c


def save(path, c):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: c[k] for k in DEFAULTS}, f, indent=1)
    os.replace(tmp, path)


def detect(sys_usb="/sys/bus/usb/devices", dev="/dev"):
    """Edge TPUs present: USB Accelerators (sysfs vendor/product ids) and PCIe/M.2 modules (/dev/apex_N)."""
    found = []
    for d in sorted(glob.glob(os.path.join(sys_usb, "*"))):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                v = f.read().strip().lower()
            with open(os.path.join(d, "idProduct")) as f:
                p = f.read().strip().lower()
        except OSError:
            continue
        if (v, p) in USB_IDS:
            found.append({"kind": "usb", "id": f"{v}:{p}", "name": USB_IDS[(v, p)], "path": os.path.basename(d)})
    for a in sorted(glob.glob(os.path.join(dev, "apex_*"))):
        found.append({"kind": "pcie", "id": os.path.basename(a), "name": "Coral PCIe / M.2 Accelerator", "path": a,
                      "accessible": os.access(a, os.R_OK | os.W_OK)})
    return found


def is_edgetpu_model(path):
    """A TFLite flatbuffer ('TFL3' identifier) that contains the Edge TPU custom op, i.e. compiled by edgetpu_compiler."""
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 2 ** 20)
    except OSError:
        return False
    return len(data) > 8 and data[4:8] == b"TFL3" and EDGETPU_OP in data


def model_files(folder):
    out = []
    if folder and os.path.isdir(folder):
        for root, _dn, files in os.walk(folder):
            out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(".tflite")]
    return out


def labels_for(model_path):
    """Optional labels: same name with .txt (or labels.txt next to it). Lines 'N label' or 'label'."""
    base = re.sub(r"(_edgetpu)?\.tflite$", "", model_path, flags=re.I)
    for p in (base + ".txt", base + "_labels.txt", os.path.join(os.path.dirname(model_path), "labels.txt")):
        if os.path.isfile(p):
            out = {}
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, ln in enumerate(x.strip() for x in f):
                    if not ln:
                        continue
                    m = re.match(r"^(\d+)[\s:]+(.+)$", ln)
                    out[int(m.group(1)) if m else i] = m.group(2).strip() if m else ln
            return out
    return {}


def update(c, b, files):
    n = dict(c)
    if "enabled" in b:
        if not isinstance(b["enabled"], bool):
            raise CoralError("enabled must be true or false.")
        n["enabled"] = b["enabled"]
    for k in ("vision_model", "wake_model"):
        if k in b:
            v = str(b[k] or "")
            if v and v not in files:
                raise CoralError("Pick a model file from the coral folder.")
            if v and not is_edgetpu_model(v):
                raise CoralError(f"{os.path.basename(v)} is not compiled for the Edge TPU (run edgetpu_compiler on it first).")
            n[k] = v
    if "wake_label" in b:
        v = " ".join(str(b["wake_label"] or "").split())
        if len(v) > 64:
            raise CoralError("Wake label is too long.")
        n["wake_label"] = v
    if "wake_threshold" in b:
        try:
            v = float(b["wake_threshold"])
        except (TypeError, ValueError):
            raise CoralError("Threshold must be a number between 0.3 and 0.99.")
        if not 0.3 <= v <= 0.99:
            raise CoralError("Threshold must be a number between 0.3 and 0.99.")
        n["wake_threshold"] = v
    return n


def runtime():
    """(Interpreter class, load_delegate) from tflite_runtime or ai_edge_litert, or raise CoralError with the fix."""
    for mod in ("tflite_runtime.interpreter", "ai_edge_litert.interpreter"):
        try:
            m = __import__(mod, fromlist=["Interpreter", "load_delegate"])
            return m.Interpreter, m.load_delegate
        except ImportError:
            continue
    raise CoralError("No TensorFlow Lite runtime found. Install tflite_runtime (or ai_edge_litert) and the Edge TPU runtime "
                     "libedgetpu yourself; nothing is downloaded automatically.")


def interpreter(model_path, rt=None):
    """Interpreter on the Edge TPU. Refuses to fall back to the CPU silently: no TPU delegate means an error."""
    Interpreter, load_delegate = rt or runtime()
    last = None
    for name in DELEGATES:
        try:
            dlg = load_delegate(name)
            break
        except (ValueError, OSError) as e:
            last = e
    else:
        raise CoralError(f"Edge TPU runtime (libedgetpu) not loadable or no Coral attached: {last}")
    it = Interpreter(model_path=model_path, experimental_delegates=[dlg])
    it.allocate_tensors()
    return it


def status(c, files, devices):
    missing = []
    if not devices:
        missing.append("no Coral Edge TPU detected (USB Accelerator or PCIe/M.2 module)")
    try:
        runtime()
        rt = True
    except CoralError as e:
        rt = False
        missing.append(str(e))
    if not c["vision_model"] and not c["wake_model"]:
        missing.append("no Edge TPU model selected")
    return {"enabled": c["enabled"], "devices": devices, "runtime": rt, "files": files,
            "edgetpu_files": [f for f in files if is_edgetpu_model(f)], "missing": missing,
            "ready": bool(c["enabled"] and devices and rt and (c["vision_model"] or c["wake_model"])),
            "llm_note": "The Edge TPU cannot run chat models; it is used for vision and wake-word models only."}


def _quantize(values, detail):
    scale, zero = (detail.get("quantization") or (0.0, 0))[:2]
    if not scale:
        return values
    return [round(v / scale + zero) for v in values]


def _dequantize(values, detail):
    scale, zero = (detail.get("quantization") or (0.0, 0))[:2]
    return [float(v) for v in values] if not scale else [(float(v) - zero) * scale for v in values]


def top_k(scores, labels, k=3):
    idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    return [{"index": i, "label": labels.get(i, str(i)), "score": round(float(scores[i]), 4)} for i in idx]


def classify_image(it, image_path, labels, k=3):
    """Image classification on the TPU. Needs Pillow and numpy (installed with the TFLite runtime)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        raise CoralError("Image classification needs numpy and Pillow (python3-numpy, python3-pil).")
    inp, out = it.get_input_details()[0], it.get_output_details()[0]
    _, h, w, _ = inp["shape"]
    img = Image.open(image_path).convert("RGB").resize((int(w), int(h)))
    it.set_tensor(inp["index"], np.asarray(img, dtype=inp["dtype"])[None, ...])
    it.invoke()
    scores = _dequantize(it.get_tensor(out["index"])[0].tolist(), out)
    return top_k(scores, labels, k)


class WakeWord:
    """Wake-word spotting on the TPU for models whose input is a raw 16 kHz mono audio window ([1, N] samples).

    Feed s16le PCM frames (e.g. from victor-mic or parec); `feed` returns True once when the configured label's score
    passes the threshold, then waits `cooldown` windows. Models that need spectrogram features are not supported."""

    def __init__(self, it, labels, label, threshold=0.8, hop=0.5, cooldown=2):
        import numpy as np
        self.np, self.it = np, it
        self.inp, self.out = it.get_input_details()[0], it.get_output_details()[0]
        shape = list(self.inp["shape"])
        if len(shape) != 2 or shape[0] != 1:
            raise CoralError("This wake-word model does not take a raw audio window ([1, N] samples).")
        self.n = int(shape[1])
        self.labels, self.threshold = labels, float(threshold)
        inv = {v.lower(): k for k, v in labels.items()}
        if label.lower() not in inv:
            raise CoralError(f"Label '{label}' is not in the model's labels.")
        self.target = inv[label.lower()]
        self.hop, self.cooldown, self._cool = max(1, int(self.n * hop)), cooldown, 0
        self.buf = np.zeros(0, dtype=np.int16)

    def feed(self, pcm):
        np = self.np
        self.buf = np.concatenate([self.buf, np.frombuffer(pcm, dtype="<i2")])
        hit = False
        while len(self.buf) >= self.n:
            win, self.buf = self.buf[:self.n], self.buf[self.hop:]
            x = win.astype(np.float32) / 32768.0
            if self.inp["dtype"] != np.float32:
                x = np.asarray(_quantize(x.tolist(), self.inp), dtype=self.inp["dtype"])
            self.it.set_tensor(self.inp["index"], x[None, ...].astype(self.inp["dtype"]))
            self.it.invoke()
            score = _dequantize(self.it.get_tensor(self.out["index"])[0].tolist(), self.out)[self.target]
            if self._cool:
                self._cool -= 1
            elif score >= self.threshold:
                hit, self._cool = True, self.cooldown
        return hit
