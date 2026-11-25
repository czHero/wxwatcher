"""gui_driver tests: calib.json loading, placeholder expansion, runner injection.

calib.json is the pinned data-driven shape (see task-4 brief): every external
command lives as an argv TEMPLATE here so this module never embeds ydotool
CLI syntax (ydotool 1.0.4 lacks --absolute; syntax drifts between versions).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui_driver import Calib, expand_steps, load_calib, run_steps

GOOD = {
    "screen": [1920, 1080],
    "steps": {
        "focus": [["echo", "wmctrl", "-a", "WeChat"]],
        "open_search": [["echo", "ydotool", "key", "ctrl+f"], ["echo", "{IDENT}"]],
        "paste": [["echo", "wl-copy", "{TEXT}"], ["echo", "ydotool", "key", "ctrl+v"]],
        "select_first": [["echo", "ydotool", "key", "down"], ["echo", "ydotool", "key", "tab"]],
        "send": [["echo", "ydotool", "key", "return"]],
    },
}


def good_calib(tmp: Path) -> Calib:
    path = tmp / "calib.json"
    path.write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    return load_calib(path)


class GuiDriverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write_calib(self, data) -> Path:
        path = self.dir / "calib.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_load_calib_rejects_missing_fields(self):
        base = json.loads(json.dumps(GOOD))
        del base["screen"]
        with self.assertRaises(ValueError):
            load_calib(self.write_calib(base))
        for phase in ("focus", "open_search", "paste", "select_first", "send"):
            missing = json.loads(json.dumps(GOOD))
            del missing["steps"][phase]
            with self.assertRaises(ValueError):
                load_calib(self.write_calib(missing))
        bad = json.loads(json.dumps(GOOD))
        bad["steps"]["paste"] = ["wl-copy {TEXT}"]  # argv not a list
        with self.assertRaises(ValueError):
            load_calib(self.write_calib(bad))
        bad2 = json.loads(json.dumps(GOOD))
        bad2["steps"]["paste"][0].append(123)  # argv element not a string
        with self.assertRaises(ValueError):
            load_calib(self.write_calib(bad2))
        calib = good_calib(self.dir)
        self.assertEqual(calib.screen, (1920, 1080))
        self.assertEqual(sorted(calib.steps), sorted(GOOD["steps"]))

    def test_expand_ident_and_text_placeholders(self):
        calib = good_calib(self.dir)
        opened = expand_steps(calib, "open_search", ident="RPT-张三")
        self.assertEqual(
            opened,
            [["echo", "ydotool", "key", "ctrl+f"], ["echo", "RPT-张三"]],
        )
        pasted = expand_steps(calib, "paste", text="hello\nworld  ")
        self.assertEqual(
            pasted,
            [["echo", "wl-copy", "hello\nworld"], ["echo", "ydotool", "key", "ctrl+v"]],
        )
        # placeholder present but arg missing -> ValueError
        with self.assertRaises(ValueError):
            expand_steps(calib, "open_search")
        with self.assertRaises(ValueError):
            expand_steps(calib, "paste")
        # phases without placeholders expand verbatim
        self.assertEqual(
            expand_steps(calib, "send"), [["echo", "ydotool", "key", "return"]]
        )

    def test_paste_steps_contain_no_return_key(self):
        # FP#1: stray Return before chat selection sends to the wrong target,
        # so return/enter key tokens are banned outside the "send" template.
        calib = good_calib(self.dir)
        for phase in ("focus", "open_search", "paste", "select_first"):
            for argv in expand_steps(calib, phase, ident="X", text="Y"):
                for token in argv:
                    parts = [p.lower() for p in token.split("+")]
                    self.assertNotIn("return", parts, argv)
                    self.assertNotIn("enter", parts, argv)
        bad = json.loads(json.dumps(GOOD))
        bad["steps"]["paste"] = [["echo", "wl-copy", "{TEXT}"], ["echo", "ydotool", "key", "return"]]
        with self.assertRaises(ValueError):
            load_calib(self.write_calib(bad))

    def test_text_argv_is_last_arg_only(self):
        # {TEXT} is only ever a standalone argv element; multi-line user text
        # can never merge into or split other argv (no shell, no injection).
        calib = good_calib(self.dir)
        pasted = expand_steps(calib, "paste", text="a; rm -rf /\n")
        self.assertEqual(pasted[0][-1], "a; rm -rf /")
        self.assertEqual(len(pasted[0]), 3)
        merged = json.loads(json.dumps(GOOD))
        merged["steps"]["paste"][0] = ["echo", "wl-{TEXT}"]  # substring merge
        with self.assertRaises(ValueError):
            load_calib(self.write_calib(merged))

    def test_text_rstrip_preserves_leading_whitespace(self):
        # Review-Focus #1: only TRAILING whitespace is stripped from {TEXT};
        # leading whitespace is part of the message body.
        calib = good_calib(self.dir)
        pasted = expand_steps(calib, "paste", text="  hello\nworld  ")
        self.assertEqual(pasted[0], ["echo", "wl-copy", "  hello\nworld"])

    def test_example_calib_loads(self):
        # calib.example.json is tracked; it must match the current row-mode
        # production shape: no target_mode/search, {IDENT}=row y in BOTH
        # open_search and send (closing row-click), paste template redirects
        # the wl-copy daemon's fds (pipe-hang fix) with {TEXT} a whole element.
        example = Path(__file__).resolve().parents[1] / "calib.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        self.assertNotIn("target_mode", raw)
        calib = load_calib(example)
        self.assertEqual(sorted(calib.steps), sorted(GOOD["steps"]))
        for phase in ("focus", "open_search", "paste", "select_first", "send"):
            for argv in expand_steps(calib, phase, ident="141", text="正文"):
                self.assertNotIn("{IDENT}", argv)
                self.assertNotIn("{TEXT}", argv)
        sent = expand_steps(calib, "send", ident="141")
        self.assertTrue(any("141" in argv for argv in sent),
                        "send must carry the closing row-click {IDENT}")
        paste = expand_steps(calib, "paste", text="正文")
        self.assertEqual(
            paste[0],
            ["sh", "-c", 'wl-copy "$1" >/dev/null 2>&1', "_", "正文"],
        )

    def test_run_steps_uses_injected_runner(self):
        calls = []

        def fake_runner(argv):
            calls.append(argv)
            return ("done", argv)

        steps = [["echo", "one"], ["echo", "two"], ["echo", "{TEXT}"]]
        results = run_steps(steps, runner=fake_runner)
        self.assertEqual(calls, steps)  # verbatim argv, sequential order
        self.assertEqual(
            results, [("done", s) for s in steps]
        )


if __name__ == "__main__":
    unittest.main()
