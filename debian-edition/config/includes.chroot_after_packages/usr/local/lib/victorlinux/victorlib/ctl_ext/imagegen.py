"""victor-ctl image ...: offline image generation with a user-supplied stable-diffusion.cpp (same rules as Arch).

Off by default, nothing is downloaded, no cloud fallback. Models go in images/models on the data partition, pictures
are saved to images/out.
"""
import os, subprocess
from ..common import VictorError, data_path
from .. import imagegen

NAME = "image"
POPEN = subprocess.Popen


def register(sp):
    p = sp.add_parser(NAME, help="offline image generation (stable-diffusion.cpp you provide); off by default")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    st = s.add_parser("set"); st.add_argument("assign", nargs="+", metavar="KEY=VALUE", help="enabled, model, bin, threads")
    g = s.add_parser("generate"); g.add_argument("prompt")
    g.add_argument("--negative", default=""); g.add_argument("--width", type=int, default=512); g.add_argument("--height", type=int, default=512)
    g.add_argument("--steps", type=int, default=20); g.add_argument("--cfg", type=float, default=7.0); g.add_argument("--seed", type=int, default=-1)
    g.add_argument("--sampler", default="euler_a", choices=imagegen.SAMPLERS)


def _path():
    return data_path("config", "imagegen.json")


def _files():
    return imagegen.model_files(data_path("images", "models"))


def handle(a):
    try:
        c, files = imagegen.load(_path()), _files()
        if a.cmd == "status":
            return dict(imagegen.status(c, files), folder=data_path("images", "models"))
        if a.cmd == "set":
            b = {}
            for x in a.assign:
                k, sep, v = x.partition("=")
                if not sep or k not in ("enabled", "model", "bin", "threads"):
                    raise VictorError(f"Unknown setting {x} (use enabled, model, bin, threads)")
                if k == "enabled":
                    b[k] = v.lower() in ("1", "true", "yes", "on")
                elif k == "threads":
                    b[k] = int(v) if v.isdigit() else -1
                elif k == "model" and v and os.sep not in v:
                    b[k] = next((f for f in files if os.path.basename(f) == v), v)    # bare file name from the folder
                else:
                    b[k] = v
            n = imagegen.update(c, b, files)
            imagegen.save(_path(), n)
            return n
        if a.cmd == "generate":
            job = {"prompt": a.prompt, "negative": a.negative, "width": a.width, "height": a.height, "steps": a.steps,
                   "cfg": a.cfg, "seed": a.seed, "sampler": a.sampler}
            path = imagegen.generate(c, job, data_path("images", "out"), popen=POPEN)
            return {"ok": True, "file": path}
    except imagegen.ImageGenError as e:
        raise VictorError(str(e))
