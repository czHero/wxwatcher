import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roster import Target, load_roster, resolve

GOOD = json.dumps(
    {
        "targets": {
            "zhang": {"kind": "user", "ident": "RPT-张三"},
            "team": {"kind": "group", "ident": "123@chatroom"},
        }
    },
    ensure_ascii=False,
)

# 运行期 roster.json 的真实形状（Task 6b 联机校准产物）：
# row=置顶会话行 y 坐标，wxid=直连表名用。
REAL = json.dumps(
    {
        "targets": {
            "filehelper": {
                "kind": "user",
                "ident": "文件传输助手",
                "wxid": "filehelper",
                "row": 141,
            }
        }
    },
    ensure_ascii=False,
)


class RosterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, content: str) -> Path:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_load_and_resolve(self):
        roster = load_roster(self.write("r.json", GOOD))
        target = resolve(roster, "zhang")
        self.assertEqual(target, Target("zhang", "user", "RPT-张三"))

    def test_old_entries_without_row_wxid_stay_valid(self):
        roster = load_roster(self.write("r.json", GOOD))
        self.assertIsNone(roster["zhang"].row)
        self.assertIsNone(roster["zhang"].wxid)

    def test_real_runtime_shape_parses(self):
        roster = load_roster(self.write("r.json", REAL))
        self.assertEqual(
            roster["filehelper"],
            Target("filehelper", "user", "文件传输助手", row=141,
                   wxid="filehelper"),
        )

    def test_group_kind_accepted(self):
        roster = load_roster(self.write("r.json", GOOD))
        target = resolve(roster, "team")
        self.assertEqual(target.kind, "group")
        self.assertEqual(target.ident, "123@chatroom")

    def test_unknown_key_raises(self):
        roster = load_roster(self.write("r.json", GOOD))
        with self.assertRaises(KeyError):
            resolve(roster, "nobody")

    def test_bad_json_raises_valueerror(self):
        bad = json.dumps({"targets": {"zhang": {"kind": "user"}}})
        with self.assertRaises(ValueError):
            load_roster(self.write("bad.json", bad))

    # ---- row / wxid 校验（Task 6c）----

    def test_row_wrong_type_raises_valueerror_naming_key(self):
        for bad in ("141", 14.0, True, None):
            with self.subTest(row=bad):
                doc = {"targets": {"zhang": {"kind": "user", "ident": "R",
                                             "row": bad}}}
                with self.assertRaises(ValueError) as cm:
                    load_roster(self.write("r.json", json.dumps(doc)))
                self.assertIn("zhang", str(cm.exception))
                self.assertIn("row", str(cm.exception))

    def test_row_negative_raises(self):
        doc = {"targets": {"zhang": {"kind": "user", "ident": "R", "row": -1}}}
        with self.assertRaises(ValueError) as cm:
            load_roster(self.write("r.json", json.dumps(doc)))
        self.assertIn("zhang", str(cm.exception))

    def test_wxid_wrong_type_or_empty_raises(self):
        for bad in (123, "", True):
            with self.subTest(wxid=bad):
                doc = {"targets": {"zhang": {"kind": "user", "ident": "R",
                                             "wxid": bad}}}
                with self.assertRaises(ValueError) as cm:
                    load_roster(self.write("r.json", json.dumps(doc)))
                self.assertIn("zhang", str(cm.exception))
                self.assertIn("wxid", str(cm.exception))

    def test_example_roster_matches_row_mode_shape(self):
        # roster.example.json is tracked: every example target must carry a
        # row (row-mode prerequisite), and the retired 'search' field is gone.
        example = Path(__file__).resolve().parents[1] / "roster.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        targets = load_roster(example)
        self.assertTrue(targets)
        for key, target in targets.items():
            self.assertIsNotNone(target.row, key)
            self.assertNotIn("search", raw["targets"][key])
            self.assertNotIn("target_mode", raw["targets"][key])


if __name__ == "__main__":
    unittest.main()
