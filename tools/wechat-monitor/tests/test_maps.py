import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capture_linux import parse_writable_maps

SAMPLE = """\
178996f3d000-178996f3e000 ---p 00000000 00:00 0                          [anon:partition_alloc]
178996f3e000-178996f3f000 rw-p 00000000 00:00 0                          [anon:partition_alloc]
17899713e000-17899713f000 r-xp 00001000 08:01 123456 /opt/wechat/wechat
7f1000000000-7f1000100000 rw-s 00000000 00:05 9999 /dev/shm/x
700000000000-700000001000 rw-p 00000000 00:00 0
7f0000000000-7f0080000000 rw-p 00000000 00:00 0
"""


class ParseWritableMapsTest(unittest.TestCase):
    def setUp(self):
        self.regions = parse_writable_maps(SAMPLE, max_size=1 << 30)

    def test_includes_private_and_shared_writable(self):
        starts = {s for s, _e, _n in self.regions}
        self.assertIn(0x178996F3E000, starts)  # rw-p anon
        self.assertIn(0x700000000000, starts)  # rw-p no name
        self.assertIn(0x7F1000000000, starts)  # rw-s file-backed

    def test_excludes_nonwritable_and_exec(self):
        starts = {s for s, _e, _n in self.regions}
        self.assertNotIn(0x178996F3D000, starts)  # ---p
        self.assertNotIn(0x17899713E000, starts)  # r-xp

    def test_skips_region_over_max_size(self):
        starts = {s for s, _e, _n in self.regions}
        self.assertNotIn(0x7F0000000000, starts)  # 1GB > max_size

    def test_name_fallback(self):
        names = {n for _s, _e, n in self.regions}
        self.assertIn("[anon]", names)
        self.assertIn("/dev/shm/x", names)


if __name__ == "__main__":
    unittest.main()
