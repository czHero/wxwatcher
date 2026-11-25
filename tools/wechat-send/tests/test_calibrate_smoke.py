"""Smoke tests for calibrate.py's PURE parts only (interactive main() is untested).

Targets: probe_argv (shlex -> `timeout 10` wrapping), png_size (PNG IHDR parse),
save_calib (0600 atomic write round-tripping through gui_driver.load_calib,
including the refusal path for return/enter outside send).
"""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibrate import png_size, probe_argv, save_calib
from gui_driver import load_calib

GOOD_STEPS = {
    "focus": [["wmctrl", "-a", "WeChat"]],
    "open_search": [["ydotool", "key", "ctrl+f"], ["ydotool", "type", "{IDENT}"]],
    "paste": [["wl-copy", "{TEXT}"], ["ydotool", "key", "ctrl+v"]],
    "select_first": [["ydotool", "key", "down"], ["ydotool", "key", "tab"]],
    "send": [["ydotool", "key", "return"]],
}


def png_bytes(w, h):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", w, h) + b"\x00" * 9)


class CalibratePureSmokeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_probe_argv_wraps_timeout_and_shlexes(self):
        self.assertEqual(probe_argv("ydotool key ctrl+f"),
                         ["timeout", "10", "ydotool", "key", "ctrl+f"])
        self.assertEqual(probe_argv('wl-copy "hello world"'),
                         ["timeout", "10", "wl-copy", "hello world"])
        with self.assertRaises(ValueError):
            probe_argv("   ")

    def test_png_size_parses_ihdr(self):
        p = self.dir / "shot.png"
        p.write_bytes(png_bytes(1366, 768))
        self.assertEqual(png_size(p), (1366, 768))
        (self.dir / "bad.png").write_bytes(b"not a png at all")
        with self.assertRaises(ValueError):
            png_size(self.dir / "bad.png")
        # clean OSError type (FileNotFoundError) when the shot is missing —
        # main() catches (OSError, ValueError) and falls back to manual WxH.
        with self.assertRaises(FileNotFoundError):
            png_size(self.dir / "missing.png")

    def test_save_calib_roundtrips_through_load_calib(self):
        out = self.dir / "calib.json"
        calib = {"screen": [1920, 1080], "steps": GOOD_STEPS}
        self.assertIsNone(save_calib(calib, out))
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        loaded = load_calib(out)
        self.assertEqual(loaded.screen, (1920, 1080))
        self.assertEqual(loaded.steps, GOOD_STEPS)

    def test_save_calib_refuses_invalid_and_preserves_old_file(self):
        out = self.dir / "calib.json"
        save_calib({"screen": [1920, 1080], "steps": GOOD_STEPS}, out)
        bad = json.loads(json.dumps(GOOD_STEPS))
        bad["select_first"] = [["ydotool", "key", "return"]]  # FP#1 ban
        with self.assertRaises(ValueError):
            save_calib({"screen": [800, 600], "steps": bad}, out)
        old = load_calib(out)  # tmp-then-replace keeps the previous good file
        self.assertEqual(old.screen, (1920, 1080))
        self.assertEqual(old.steps, GOOD_STEPS)
        self.assertFalse((self.dir / "calib.json.tmp").exists())  # refusal cleans stale tmp
        with self.assertRaises(ValueError):  # placeholder must be its own element
            save_calib({"screen": [1, 1], "steps": {**GOOD_STEPS,
                        "focus": [["echo", "wl-{TEXT}"]]}}, out)


if __name__ == "__main__":
    unittest.main()
