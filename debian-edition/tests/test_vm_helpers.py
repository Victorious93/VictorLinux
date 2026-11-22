"""Unit tests for the pure helpers in vm_test.py (no QEMU needed)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import vm_test  # noqa: E402


def write_ppm(path, pixels, w, h):
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        f.write(b"".join(bytes(p) for p in pixels))


class PpmColors(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)

    def test_black_screen_is_one_colour(self):
        p = os.path.join(self.d.name, "a.ppm")
        write_ppm(p, [(0, 0, 0)] * 64, 8, 8)
        self.assertEqual(vm_test.ppm_colors(p), 1)

    def test_menu_like_screen_has_many_colours(self):
        p = os.path.join(self.d.name, "b.ppm")
        write_ppm(p, [(i, 0, 255 - i) for i in range(64)], 8, 8)
        self.assertEqual(vm_test.ppm_colors(p), 64)

    def test_not_a_ppm_is_zero(self):
        p = os.path.join(self.d.name, "c.ppm")
        with open(p, "wb") as f:
            f.write(b"garbage")
        self.assertEqual(vm_test.ppm_colors(p), 0)

    def test_cap(self):
        p = os.path.join(self.d.name, "d.ppm")
        write_ppm(p, [(i, 1, 2) for i in range(64)], 8, 8)
        self.assertEqual(vm_test.ppm_colors(p, limit=10), 10)


class Accel(unittest.TestCase):
    def test_returns_kvm_or_tcg(self):
        self.assertIn(vm_test.accel_args(), (["-accel", "kvm"], ["-accel", "tcg"]))


if __name__ == "__main__":
    unittest.main()
