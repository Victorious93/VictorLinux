"""scripts/make-usb.sh against file-backed loop devices (never a real disk). Needs root, losetup, sgdisk, mkfs.ext4.

Covers: real `write` on 512-byte loop disks (MBR layout for an MBR image, GPT for a GPT hybrid image), a sparse 3 TiB
GPT layout (partition end beyond the 32-bit MBR limit) validated by sgdisk, 4Kn refusal, too-small refusal, and that every
refusal happens BEFORE the target is modified. A QEMU boot of the written layouts runs when qemu + OVMF + grub-mkrescue exist
(this proves the boot path of a *test* GRUB image, not of the VictorLinux ISO, and not physical hardware).
"""
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

import test_layout_planner as tlp

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(REPO, "scripts", "make-usb.sh")
need = ["losetup", "sgdisk", "mkfs.ext4", "blockdev", "lsblk", "wipefs", "partprobe", "findmnt"]
pytestmark = pytest.mark.skipif(os.geteuid() != 0 or any(shutil.which(t) is None for t in need), reason="needs root and " + ", ".join(need))
ENV = dict(os.environ, VICTOR_USB_TEST_LOOP="1")


def sh(*a, check=True, **kw):
    return subprocess.run(list(a), capture_output=True, text=True, check=check, **kw)


def usb(*a, check=False):
    return subprocess.run(["bash", SCRIPT, *a], capture_output=True, text=True, env=ENV, check=check)


@pytest.fixture
def loopdisk(tmp_path):
    made = []

    def make(size, sector=None):
        p = tmp_path / f"disk{len(made)}.img"
        with open(p, "wb") as f:
            try:
                f.truncate(size)
            except OSError:
                pytest.skip("filesystem cannot hold a sparse file this large")
        args = ["losetup", "-f", "--show", "-P"] + (["--sector-size", str(sector)] if sector else []) + [str(p)]
        r = sh(*args, check=False)
        if r.returncode != 0:
            pytest.skip("cannot create loop device: " + r.stderr.strip())
        made.append(r.stdout.strip())
        return str(p), r.stdout.strip()
    yield make
    for d in made:
        sh("losetup", "-d", d, check=False)


def image_with_sha(tmp_path, kind="mbr", size=100 << 20, name="x.iso"):
    iso = tmp_path / name
    tlp.make_iso(str(iso), size=size, kind=kind)
    with open(iso, "r+b") as f:   # make_iso leaves an all-zero payload: stamp a name-unique one so two images never compare equal
        f.seek(1 << 20); f.write(hashlib.sha256(name.encode()).digest() * 4096)
    (tmp_path / (name + ".sha256")).write_text(hashlib.sha256(iso.read_bytes()).hexdigest() + "  " + name + "\n")
    return str(iso)


def disk_digest(path, n=4 << 20):
    with open(path, "rb") as f:
        head = f.read(n)
        f.seek(-n, 2)
        return hashlib.sha256(head + f.read(n)).hexdigest()


def test_mbr_image_on_6gib_disk_gets_slot3_and_ext4_label(tmp_path, loopdisk):
    iso = image_with_sha(tmp_path)
    img, dev = loopdisk(6 << 30)
    r = usb("write", "--iso", iso, "--device", dev, "--confirm-serial", "LOOPTEST")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "partition table: mbr" in r.stdout and "payload" in r.stdout and "NOT byte-identical" in r.stdout
    assert sh("blkid", "-p", "-s", "LABEL", "-o", "value", dev + "p3").stdout.strip() == "VICTORDATA"
    assert "VICTORDATA" not in sh("sgdisk", "-p", dev, check=False).stdout            # MBR disk: no GPT was invented


def test_gpt_image_keeps_entries_and_sgdisk_validates_after_write(tmp_path, loopdisk):
    if not shutil.which("grub-mkrescue"):
        pytest.skip("grub-mkrescue not installed")
    tree = tmp_path / "t" / "boot" / "grub"; tree.mkdir(parents=True)
    (tree / "grub.cfg").write_text("serial --unit=0\nterminal_input serial console\nterminal_output serial console\necho VICTORLINUX-TEST-GRUB-BOOTED\n")
    iso = str(tmp_path / "g.iso")
    if sh("grub-mkrescue", "-o", iso, str(tmp_path / "t"), check=False).returncode != 0:
        pytest.skip("grub-mkrescue cannot build an image here")
    (tmp_path / "g.iso.sha256").write_text(hashlib.sha256(open(iso, "rb").read()).hexdigest() + "  g.iso\n")
    img, dev = loopdisk(3 << 40)                                                       # sparse 3 TiB
    r = usb("write", "--iso", iso, "--device", dev, "--confirm-serial", "LOOPTEST")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "partition table: gpt" in r.stdout
    assert "No problems found" in sh("sgdisk", "-v", dev).stdout
    listing = sh("sgdisk", "-p", dev).stdout
    assert "VICTORDATA" in listing and "EFI boot partition" in listing
    last = int(listing.strip().splitlines()[-1].split()[2])
    assert last > 2 ** 32                                                              # data partition ends beyond the MBR limit
    out = sh("blockdev", "--getsize64", dev + "p5").stdout.strip()
    assert int(out) > 2.9 * (1 << 40)                                                  # ~3 TiB usable, not truncated to 2 TiB
    if shutil.which("qemu-system-x86_64") and os.path.exists("/usr/share/OVMF/OVMF_CODE_4M.fd"):
        for mode, fw in (("uefi", ["-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd"]), ("bios", [])):
            log = tmp_path / f"{mode}.log"
            subprocess.run(["timeout", "45", "qemu-system-x86_64", "-m", "512", "-display", "none", "-no-reboot", *fw,
                            "-drive", f"file={img},format=raw,if=ide", "-serial", f"file:{log}"], capture_output=True)
            assert b"VICTORLINUX-TEST-GRUB-BOOTED" in log.read_bytes(), mode


def test_4kn_target_is_refused_before_any_write(tmp_path, loopdisk):
    iso = image_with_sha(tmp_path)
    img, dev = loopdisk(6 << 30, sector=4096)
    before = disk_digest(img)
    for sub in ("plan", "write"):
        r = usb(sub, "--iso", iso, "--device", dev, *(["--confirm-serial", "LOOPTEST"] if sub == "write" else []))
        assert r.returncode != 0 and "4Kn" in (r.stdout + r.stderr), sub
    assert disk_digest(img) == before


def test_too_small_target_is_refused_with_the_minimum_and_left_untouched(tmp_path, loopdisk):
    iso = image_with_sha(tmp_path, size=1900 << 20, name="big.iso")
    img, dev = loopdisk(3 << 30)
    with open(img, "r+b") as f:
        f.write(b"OWNER-DATA" * 100)
    before = disk_digest(img)
    r = usb("write", "--iso", iso, "--device", dev, "--confirm-serial", "LOOPTEST")
    assert r.returncode != 0 and "need at least" in r.stderr and "GiB" in r.stderr
    assert disk_digest(img) == before


def test_wrong_serial_and_missing_checksum_refused_untouched(tmp_path, loopdisk):
    iso = image_with_sha(tmp_path)
    img, dev = loopdisk(6 << 30)
    before = disk_digest(img)
    assert usb("write", "--iso", iso, "--device", dev, "--confirm-serial", "NOPE").returncode != 0
    os.unlink(iso + ".sha256")
    assert usb("write", "--iso", iso, "--device", dev, "--confirm-serial", "LOOPTEST").returncode != 0
    assert disk_digest(img) == before


def test_mbr_only_image_on_over_2tib_disk_is_refused_unless_cap_is_explicit(tmp_path, loopdisk):
    iso = image_with_sha(tmp_path)                       # synthetic MBR image whose first partition starts at LBA 0
    img, dev = loopdisk(3 << 40)
    before = disk_digest(img)
    r = usb("plan", "--iso", iso, "--device", dev)
    assert r.returncode != 0 and "GPT is required" in r.stderr and "--allow-mbr-cap" in r.stderr
    assert disk_digest(img) == before
    r = usb("plan", "--iso", iso, "--device", dev, "--allow-mbr-cap")
    assert r.returncode == 0 and "capped" in r.stdout


def test_update_in_place_keeps_victordata_contents_and_verify_is_read_only(tmp_path, loopdisk):
    """write v1 -> put a file on VICTORDATA -> update to a larger v2 image -> the file and UUID survive; verify passes."""
    old = image_with_sha(tmp_path, size=100 << 20, name="old.iso")
    img, dev = loopdisk(6 << 30)
    assert usb("write", "--iso", old, "--device", dev, "--confirm-serial", "LOOPTEST").returncode == 0
    mnt = tmp_path / "m"; mnt.mkdir()
    sh("mount", dev + "p3", str(mnt)); (mnt / "victorlinux" / "chats" / "keep.txt").write_text("precious"); sh("umount", str(mnt))
    uuid_before = sh("blkid", "-p", "-s", "UUID", "-o", "value", dev + "p3").stdout.strip()
    new = image_with_sha(tmp_path, size=180 << 20, name="new.iso")
    assert usb("update", "--iso", new, "--device", dev).returncode != 0                   # needs the serial confirmation
    r = usb("update", "--iso", new, "--device", dev, "--confirm-serial", "LOOPTEST")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sh("blkid", "-p", "-s", "UUID", "-o", "value", dev + "p3").stdout.strip() == uuid_before
    sh("mount", "-o", "ro", dev + "p3", str(mnt)); assert (mnt / "victorlinux" / "chats" / "keep.txt").read_text() == "precious"; sh("umount", str(mnt))
    before = disk_digest(img)
    v = usb("verify", "--iso", new, "--device", dev)
    assert v.returncode == 0 and "OK    OS image" in v.stdout and disk_digest(img) == before
    assert usb("verify", "--iso", old, "--device", dev).returncode != 0                   # the old image no longer matches
    too_big = image_with_sha(tmp_path, size=3 << 30, name="big.iso")
    before = disk_digest(img)
    r = usb("update", "--iso", too_big, "--device", dev, "--confirm-serial", "LOOPTEST")
    assert r.returncode != 0 and "overwrite your data" in r.stderr and disk_digest(img) == before
