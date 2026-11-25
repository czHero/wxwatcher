import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor


class StateTest(unittest.TestCase):
    def test_roundtrip_and_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            state = {"version": 1, "watermarks": {"message_0.db|Msg_x": 42}}
            monitor.save_state(path, state)
            self.assertEqual(monitor.load_state(path), state)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertFalse((Path(td) / "state.json.tmp").exists())

    def test_missing_state_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            state = monitor.load_state(Path(td) / "missing.json")
            self.assertEqual(state, {"version": 1, "watermarks": {}})

    def test_group_table_name(self):
        groups = {"123@chatroom": {"name": "group", "table": "SessionTable"}}
        table_map = monitor.group_table_map(groups)
        digest = hashlib.md5(b"123@chatroom").hexdigest()
        self.assertIn(f"Msg_{digest}", table_map)
        self.assertEqual(table_map[f"Msg_{digest}"]["name"], "group")


if __name__ == "__main__":
    unittest.main()
