"""Build-time downloads that are not git commits must be compared with a SHA-256 pinned in the repo."""
import configparser
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _cfg():
    c = configparser.ConfigParser()
    c.read(os.path.join(ROOT, ".ci", "options.ini"))
    return c


def test_pins_exist_and_look_like_sha256():
    c = _cfg()
    for key in ("llama_ui", "nvidia_pstated"):
        assert re.fullmatch(r"[0-9a-f]{64}", c["checksums"][key]), key


def test_prepare_sources_compares_every_download_with_its_pin():
    s = open(os.path.join(ROOT, "scripts", "prepare-sources.sh")).read()
    assert "opt checksums llama_ui" in s and "opt checksums nvidia_pstated" in s
    # the old behaviour (hash computed after download and just recorded) must not come back
    assert 'sha256sum "$dest/nvidia-pstated" | awk' not in s.replace('got_ps=$(sha256sum "$dest/nvidia-pstated" | awk', "")
    assert s.index("opt checksums nvidia_pstated") < s.index('chmod +x "$dest/nvidia-pstated"')


def test_checksums_section_does_not_break_the_profile_renderer():
    src = open(os.path.join(ROOT, ".ci", "configure.py")).read()
    assert '("repositories", "revisions", "files", "build")' in src      # only named sections are read
