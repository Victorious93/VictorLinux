"""RustDeskV built into both editions: validation, fail-closed policy, packaging toggle, no secrets in the tree."""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ARCH = os.path.join(ROOT, "airootfs")
DEB = os.path.join(ROOT, "debian-edition", "config", "includes.chroot_after_packages")
sys.path.insert(0, os.path.join(ARCH, "usr", "local", "lib", "victorlinux"))
import victorrustdeskv as rv  # noqa: E402

SHARED = ["usr/local/lib/victorlinux/victorrustdeskv.py", "usr/local/lib/victorlinux/victor-rustdeskv-priv",
          "usr/local/bin/victor-rustdeskv", "etc/systemd/system/victor-rustdeskv.service",
          "etc/systemd/system/victor-rustdeskv-restore.service", "etc/victorlinux/rustdeskv-relay.conf",
          "usr/share/applications/victor-rustdeskv.desktop", "usr/share/applications/victor-rustdeskv-setup.desktop"]


def load_priv():
    path = os.path.join(ARCH, "usr", "local", "lib", "victorlinux", "victor-rustdeskv-priv")
    loader = importlib.machinery.SourceFileLoader("victor_rustdeskv_priv", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def read(p):
    with open(p) as f:
        return f.read()


# ------------------------------------------------------------------ password: there is no default, and weak ones are refused

@pytest.mark.parametrize("pw", ["", "short", "aaaaaaaaaaaa", "password", "1234567890", " leadingspace1", "trailing1234 ",
                                "-startsdash12345", "line\nbreak12345", "x" * 129, "tab\tinside1234", "ünïcödepass12"])
def test_bad_passwords_refused(pw):
    with pytest.raises(rv.SetupError):
        rv.validate_password(pw)


def test_good_password_accepted():
    assert rv.validate_password("correct horse battery") == "correct horse battery"


def test_no_default_password_anywhere():
    for base in (ARCH, DEB):
        for rel in SHARED:
            text = read(os.path.join(base, rel))
            assert not re.search(r"--password\s+['\"]?[A-Za-z0-9]", text.replace("rd(\"--password\", pw)", "")), rel


# ------------------------------------------------------------------ relay placeholder: empty by default, validated when set

def test_shipped_relay_conf_is_empty_and_valid():
    cfg = rv.parse_relay(read(os.path.join(ARCH, "etc", "victorlinux", "rustdeskv-relay.conf")))
    assert cfg == {"rendezvous_server": "", "relay_server": "", "api_server": "", "key": ""}
    assert all(v == "" for _, v in rv.relay_options(cfg))      # empty value clears the option = public servers


def test_relay_accepts_own_server():
    cfg = rv.parse_relay("rendezvous_server = hbbs.example.com:21116\nrelay_server=10.0.0.5:21117\n"
                         "api_server = https://api.example.com\nkey = " + "A" * 43 + "=\n")
    assert cfg["rendezvous_server"] == "hbbs.example.com:21116" and cfg["relay_server"] == "10.0.0.5:21117"


@pytest.mark.parametrize("text", ["bogus = 1", "rendezvous_server = bad host", "rendezvous_server = a.b:99999",
                                  "rendezvous_server = 999.1.1.1", "api_server = http://example.com",
                                  "key = hunter2", "key = " + "A" * 43, "no equals sign", "rendezvous_server = $(id)",
                                  "rendezvous_server = a.b:1:2"])
def test_relay_rejects_bad_input(text):
    with pytest.raises(rv.SetupError):
        rv.parse_relay(text)


def test_relay_http_only_for_private():
    assert rv.parse_relay("api_server = http://192.168.1.5:21114")["api_server"] == "http://192.168.1.5:21114"


# ------------------------------------------------------------------ persistence detection mirrors Victorious Mesh

def test_persist_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("VICTOR_RUSTDESKV_PERSIST", raising=False)
    monkeypatch.setattr(rv, "RUN", str(tmp_path))
    assert rv.persist_dir() == (None, False)
    (tmp_path / "persist.json").write_text(json.dumps({"mode": "persistent", "data_root": "/mnt/x/victorlinux"}))
    assert rv.persist_dir() == ("/mnt/x/victorlinux/security/rustdeskv", True)
    (tmp_path / "persist.json").write_text(json.dumps({"mode": "temporary", "data_root": None}))
    assert rv.persist_dir() == (None, False)


# ------------------------------------------------------------------ root helper: fail closed, never echo the password

class FakeRD:
    """Stands in for `rustdesk` IPC: --option k v sets, --option k reads, --password pw."""
    def __init__(self, accept=True, password_ok=True):
        self.opts, self.accept, self.password_ok, self.calls = {}, accept, password_ok, []

    def __call__(self, *args, timeout=15):
        self.calls.append(args)
        out = subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "--option" and len(args) == 3 and self.accept:
            self.opts[args[1]] = args[2]
        elif args[0] == "--option" and len(args) == 2:
            out.stdout = self.opts.get(args[1], "") + "\n"
        elif args[0] == "--password":
            out.stdout = "Done!\n" if self.password_ok else "Error: permission denied for " + args[1] + "\n"
        return out


class Clock:
    t = 0.0

    @classmethod
    def time(cls):
        return cls.t

    @classmethod
    def sleep(cls, s):
        cls.t += max(s, 1)


@pytest.fixture
def priv(tmp_path, monkeypatch):
    p = load_priv()
    monkeypatch.setattr(rv, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(rv, "MARKER", str(tmp_path / "state" / "enabled"))
    monkeypatch.setattr(rv, "RELAY_CONF", str(tmp_path / "relay.conf"))
    monkeypatch.setattr(rv, "RUN", str(tmp_path / "run"))
    monkeypatch.delenv("VICTOR_RUSTDESKV_PERSIST", raising=False)
    monkeypatch.setattr(p, "time", Clock)
    Clock.t = 0.0
    monkeypatch.setattr(p, "installed", lambda: True)

    real = p.safefs

    class NonRootSafefs:               # same symlink-safe code, but the test user stands in for root (uid check / chown dropped)
        UnsafePath = real.UnsafePath
        read_file = staticmethod(real.read_file)

        @staticmethod
        def open_dir(path, uid=None, create=False, mode=0o700):
            return real.open_dir(path, uid=None, create=create, mode=mode)

        @staticmethod
        def write_file(dfd, name, data, uid=None, gid=None, mode=0o600):
            return real.write_file(dfd, name, data, mode=mode)
    monkeypatch.setattr(p, "safefs", NonRootSafefs)
    return p


def test_apply_sets_password_only_policy(priv, monkeypatch, capsys):
    fake = FakeRD()
    monkeypatch.setattr(priv, "rd", fake)
    priv.cmd_apply()
    assert fake.opts["verification-method"] == "use-permanent-password"
    assert fake.opts["approve-mode"] == "password"
    assert fake.opts["custom-rendezvous-server"] == "" and "public" in capsys.readouterr().out


def test_apply_uses_relay_config_when_set(priv, monkeypatch, tmp_path):
    (tmp_path / "relay.conf").write_text("rendezvous_server = hbbs.example.com\nrelay_server = hbbr.example.com\n")
    fake = FakeRD()
    monkeypatch.setattr(priv, "rd", fake)
    priv.cmd_apply()
    assert fake.opts["custom-rendezvous-server"] == "hbbs.example.com" and fake.opts["relay-server"] == "hbbr.example.com"


def test_apply_fails_closed_when_policy_not_confirmed(priv, monkeypatch):
    monkeypatch.setattr(priv, "rd", FakeRD(accept=False))
    with pytest.raises(SystemExit) as e:
        priv.cmd_apply()
    assert e.value.code != 0


def test_apply_refuses_invalid_relay_config(priv, monkeypatch, tmp_path):
    (tmp_path / "relay.conf").write_text("rendezvous_server = not a host\n")
    monkeypatch.setattr(priv, "rd", FakeRD())
    with pytest.raises(SystemExit):
        priv.cmd_apply()


def _setup(priv, monkeypatch, pw, rd, systemctl_rc=0):
    calls = []

    def fake_run(cmd, timeout=30, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, systemctl_rc, "", "boom " + pw if systemctl_rc else "")
    monkeypatch.setattr(priv, "run", fake_run)
    monkeypatch.setattr(priv, "rd", rd)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(pw + "\n"))
    return calls


def test_setup_creates_marker_only_after_password_is_accepted(priv, monkeypatch, capsys):
    calls = _setup(priv, monkeypatch, "my own long secret", FakeRD())
    priv.cmd_setup()
    assert os.path.exists(rv.MARKER)
    assert ["systemctl", "start", rv.UNIT] in calls
    assert "my own long secret" not in capsys.readouterr().out


def test_setup_rejects_weak_password_without_starting_anything(priv, monkeypatch):
    calls = _setup(priv, monkeypatch, "short", FakeRD())
    with pytest.raises(SystemExit):
        priv.cmd_setup()
    assert calls == [] and not os.path.exists(rv.MARKER)


def test_setup_rolls_back_and_hides_password_when_rustdesk_refuses(priv, monkeypatch, capsys):
    calls = _setup(priv, monkeypatch, "my own long secret", FakeRD(password_ok=False))
    with pytest.raises(SystemExit):
        priv.cmd_setup()
    err = capsys.readouterr().err
    assert "my own long secret" not in err
    assert not os.path.exists(rv.MARKER)
    assert ["systemctl", "stop", rv.UNIT] in calls


def test_setup_service_start_failure_hides_password(priv, monkeypatch, capsys):
    _setup(priv, monkeypatch, "my own long secret", FakeRD(), systemctl_rc=1)
    with pytest.raises(SystemExit):
        priv.cmd_setup()
    assert "my own long secret" not in capsys.readouterr().err and not os.path.exists(rv.MARKER)


def test_setup_refuses_when_not_installed(priv, monkeypatch):
    monkeypatch.setattr(priv, "installed", lambda: False)
    with pytest.raises(SystemExit):
        priv.cmd_setup()


def test_disable_removes_marker_so_the_unit_cannot_start(priv, monkeypatch):
    monkeypatch.setattr(priv, "run", lambda cmd, timeout=30, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    os.makedirs(rv.STATE_DIR)
    open(rv.MARKER, "w").write("1\n")
    priv.cmd_disable()
    assert not os.path.exists(rv.MARKER)


def test_restore_recreates_marker_from_persistent_storage(priv, monkeypatch, tmp_path):
    persist = tmp_path / "persist"
    persist.mkdir()
    (persist / "enabled").write_text("1\n")
    monkeypatch.setenv("VICTOR_RUSTDESKV_PERSIST", str(persist))
    priv.cmd_restore()
    assert os.path.exists(rv.MARKER)


def test_persistent_marker_is_written_and_removed_by_disable(priv, monkeypatch, tmp_path):
    persist = tmp_path / "persist"
    monkeypatch.setenv("VICTOR_RUSTDESKV_PERSIST", str(persist))
    assert priv.write_marker_persistent() is True
    assert (persist / "enabled").read_text() == "1\n"
    monkeypatch.setattr(priv, "run", lambda cmd, timeout=30, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    priv.cmd_disable()
    assert not (persist / "enabled").exists()


def test_planted_symlink_for_the_persistent_folder_is_refused(priv, monkeypatch, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    link = tmp_path / "persist"
    link.symlink_to(victim)
    monkeypatch.setenv("VICTOR_RUSTDESKV_PERSIST", str(link))
    with pytest.raises(SystemExit):
        priv.write_marker_persistent()
    assert list(victim.iterdir()) == []                        # nothing was written through the link


def test_planted_symlink_for_the_config_folder_is_refused(priv, monkeypatch, tmp_path):
    persist = tmp_path / "persist"
    persist.mkdir()
    victim = tmp_path / "etc"
    victim.mkdir()
    (persist / "config").symlink_to(victim)
    monkeypatch.setenv("VICTOR_RUSTDESKV_PERSIST", str(persist))
    monkeypatch.setattr(rv, "CONFIG_DIR", str(tmp_path / "cfgdir"))
    monkeypatch.setattr(priv, "run", lambda cmd, timeout=30, **kw: pytest.fail("must not mount: " + " ".join(cmd)))
    with pytest.raises(SystemExit):
        priv.cmd_mount()


def test_config_folder_is_bound_by_fd_not_by_path(priv, monkeypatch, tmp_path):
    persist = tmp_path / "persist"
    monkeypatch.setenv("VICTOR_RUSTDESKV_PERSIST", str(persist))
    monkeypatch.setattr(rv, "CONFIG_DIR", str(tmp_path / "cfgdir"))
    seen = []

    def fake_run(cmd, timeout=30, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[0] == "mountpoint" else 0, "", "")
    monkeypatch.setattr(priv, "run", fake_run)
    priv.cmd_mount()
    mount = [c for c in seen if c[0] == "mount"][0]
    assert mount[1] == "--bind" and mount[2].startswith("/proc/self/fd/")


def test_restore_without_persistence_does_nothing(priv):
    priv.cmd_restore()
    assert not os.path.exists(rv.MARKER)


def test_priv_rejects_unknown_verb(priv):
    with pytest.raises(SystemExit):
        priv.main(["x", "rm -rf /"])


# ------------------------------------------------------------------ units, wiring and the two editions

def test_unit_is_gated_and_fails_closed():
    unit = read(os.path.join(ARCH, "etc", "systemd", "system", "victor-rustdeskv.service"))
    assert "ConditionPathExists=/var/lib/victorlinux/rustdeskv/enabled" in unit
    assert re.search(r"^ExecStartPost=/usr/local/lib/victorlinux/victor-rustdeskv-priv apply$", unit, re.M)   # no '-' prefix
    assert "WantedBy=multi-user.target" in unit and "Conflicts=rustdesk.service" in unit


def test_editions_ship_identical_files():
    for rel in SHARED:
        assert read(os.path.join(ARCH, rel)) == read(os.path.join(DEB, rel)), rel


def test_sudoers_lets_owner_run_only_the_helper():
    for path in (os.path.join(ARCH, "etc/sudoers.d/10-victor"), os.path.join(DEB, "etc/sudoers.d/victor-priv")):
        lines = [x for x in read(path).splitlines() if "rustdeskv" in x and not x.startswith("#")]
        assert len(lines) == 1 and lines[0].startswith("victor ALL=(root) NOPASSWD: /usr/local/lib/victorlinux/victor-rustdeskv-priv")


def test_debian_hook_pins_and_toggle():
    hook = read(os.path.join(ROOT, "debian-edition", "config", "hooks", "live", "0270-rustdeskv.hook.chroot"))
    env = read(os.path.join(ROOT, "debian-edition", "versions.env"))
    assert re.search(r"^RUSTDESKV_DEB_SHA256=[0-9a-f]{64}$", env, re.M)
    assert 'RUSTDESKV:-1}" = "0"' in hook and "sha256sum" in hook and "Refusing to install" in hook
    build = read(os.path.join(ROOT, "debian-edition", "build.sh"))
    assert "--no-rustdeskv" in build and 'RUSTDESKV=0' in build


def test_arch_prepare_sources_pins_and_toggle():
    s = read(os.path.join(ROOT, "scripts", "prepare-sources.sh"))
    assert "opt checksums rustdeskv_arch" in s and 'VICTOR_RUSTDESKV:-1}" != "0"' in s
    assert s.index("opt checksums rustdeskv_arch") < s.index("rustdeskv.sha256")


def _render(tmp_path, args, env=None):
    work = tmp_path / "r"
    shutil.copytree(os.path.join(ROOT, ".ci"), work / ".ci")
    for dirpath, _, files in os.walk(ROOT):
        if any(x in dirpath for x in (".git", "node_modules", "debian-edition", "pi-edition", "docs")):
            continue
        for f in files:
            if f.endswith(".jinja2"):
                dst = work / os.path.relpath(os.path.join(dirpath, f), ROOT)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(os.path.join(dirpath, f), dst)
    e = {k: v for k, v in os.environ.items() if k != "VICTOR_RUSTDESKV"}
    e.update(env or {})
    r = subprocess.run([sys.executable, ".ci/configure.py", *args], cwd=work, capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stderr
    return work, r.stdout


@pytest.mark.parametrize("kind", ["headless", "gui"])
def test_both_arch_types_include_rustdeskv_by_default(tmp_path, kind):
    work, out = _render(tmp_path, ["amd", kind])
    assert "rustdeskv=on" in out
    assert "gst-plugin-pipewire" in read(work / "packages.x86_64")
    cust = read(work / "airootfs/root/customize_airootfs.sh")
    assert "pacman -U --noconfirm --noscriptlet" in cust and "systemctl enable victor-rustdeskv-restore.service victor-rustdeskv.service" in cust
    assert ("rustdesk-link.desktop" in cust) == (kind == "gui")      # desktop entries only on the GUI edition
    assert subprocess.run(["bash", "-n", str(work / "airootfs/root/customize_airootfs.sh")]).returncode == 0


@pytest.mark.parametrize("how", ["flag", "env"])
def test_toggle_off_renders_without_rustdeskv(tmp_path, how):
    args = ["amd", "gui"] + (["--no-rustdeskv"] if how == "flag" else [])
    work, out = _render(tmp_path, args, {"VICTOR_RUSTDESKV": "0"} if how == "env" else None)
    assert "rustdeskv=off" in out
    assert "gst-plugin-pipewire" not in read(work / "packages.x86_64")
    cust = read(work / "airootfs/root/customize_airootfs.sh")
    assert "pacman -U" not in cust and "victor-rustdeskv-priv" in cust and "sed -i '/victor-rustdeskv-priv/d'" in cust
    assert subprocess.run(["bash", "-n", str(work / "airootfs/root/customize_airootfs.sh")]).returncode == 0


def test_empty_edition_never_gets_rustdeskv(tmp_path):
    _, out = _render(tmp_path, ["amd", "empty"])
    assert "rustdeskv=off" in out


def test_no_secrets_or_relay_keys_committed():
    paths = [os.path.join(b, r) for b in (ARCH, DEB) for r in SHARED]
    paths += [os.path.join(ROOT, "docs", "rustdeskv.md")]
    for p in paths:
        if not os.path.exists(p):
            continue
        for line in read(p).splitlines():
            assert not re.search(r"(?i)(password|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+]{8,}", line.split("#")[0]), (p, line)
        assert not re.search(r"[A-Za-z0-9+/]{43}=", read(p)), p       # no server public key / secret blob either
