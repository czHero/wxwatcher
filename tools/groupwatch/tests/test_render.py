import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render

# 固定 epoch；HH:MM 断言按本机时区换算（与实现同语义，跨时区机器仍确定）。
TS = 1764000000


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _row(**over):
    row = {"group": "g1", "item_id": "hw", "title": "数学",
           "detail": "明天完成课本第5页，拍照上传班级圈",
           "deadline": "明天(11/25)晚上", "action": "拍照上传",
           "ts": TS, "source_keys": ["g1#1"]}
    row.update(over)
    return row


class TestRenderIntel(unittest.TestCase):
    def test_render_intel_realtime_shape(self):
        out = render.render_intel(_row(), "作业", "三(2)班群")
        self.assertEqual(out, "\n".join([
            "【作业 · 三(2)班群】数学",
            "明天完成课本第5页，拍照上传班级圈",
            "截止：明天(11/25)晚上",
            f"来源：三(2)班群 · {_hhmm(TS)}",
        ]))
        # deadline 非空才出该行：空串/None 时回到恰三行式
        for empty in ("", None):
            lines = render.render_intel(_row(deadline=empty), "作业",
                                        "三(2)班群").splitlines()
            self.assertEqual(lines, [
                "【作业 · 三(2)班群】数学",
                "明天完成课本第5页，拍照上传班级圈",
                f"来源：三(2)班群 · {_hhmm(TS)}",
            ])

    def test_render_intel_multiline_detail_preserved(self):
        detail = "第一行\n第二行\n第三行"
        out = render.render_intel(_row(title="通知", detail=detail,
                                       deadline=None), "通知事项", "家长群")
        self.assertEqual(out.splitlines(), [
            "【通知事项 · 家长群】通知",
            "第一行",
            "第二行",
            "第三行",
            f"来源：家长群 · {_hhmm(TS)}",
        ])
        self.assertIn(detail, out)  # 原样保留，未被转义/折叠


class TestRenderDigest(unittest.TestCase):
    NAMES = ({"hw": "作业", "nt": "通知"},
             {"g1": "三(2)班群", "g2": "家长群"})

    def test_render_digest_groups_by_item_then_time(self):
        item_names, group_names = self.NAMES
        rows = [  # 故意乱序传入：排序职责在 render
            _row(item_id="hw", ts=3000, title="英语"),
            _row(item_id="nt", ts=2000, group="g2", title="运动会"),
            _row(item_id="hw", ts=1000, title="数学"),
        ]
        out = render.render_digest("2026-11-24", rows, item_names,
                                   group_names)
        self.assertEqual(out.splitlines(), [
            "2026-11-24 汇总",
            "",
            "【作业】",
            f"  {_hhmm(1000)} 三(2)班群 · 数学",
            f"  {_hhmm(3000)} 三(2)班群 · 英语",
            "【通知】",
            f"  {_hhmm(2000)} 家长群 · 运动会",
        ])

    def test_render_digest_empty_none(self):
        self.assertIsNone(render.render_digest("2026-11-24", [], {}, {}))

    def test_render_digest_header_has_date(self):
        item_names, group_names = self.NAMES
        out = render.render_digest("2026-11-24", [_row()], item_names,
                                   group_names)
        self.assertIn("2026-11-24 汇总", out.splitlines()[0])

    def test_render_digest_suppressed_note(self):
        # I4（§5.5 该句落地）：suppressed_count>0 → 尾行"累计 N 条被抑制
        # （重试耗尽）"；默认 0 → 快照与旧版逐字节一致（不加行）。
        item_names, group_names = self.NAMES
        base = render.render_digest("2026-11-24", [_row()], item_names,
                                    group_names)
        self.assertNotIn("抑制", base)
        self.assertEqual(render.render_digest("2026-11-24", [_row()],
                                              item_names, group_names,
                                              suppressed_count=0),
                         base)
        with_note = render.render_digest("2026-11-24", [_row()], item_names,
                                         group_names, suppressed_count=3)
        self.assertEqual(with_note,
                         base + "\n另有累计 3 条情报被抑制（重试耗尽），"
                         "跑 groupwatch status 查看。")

    def test_render_digest_name_fallback(self):
        rows = [_row(item_id="ghost", group="gid9")]
        out = render.render_digest("2026-11-24", rows, {}, {})
        self.assertIn("【未知项】", out)
        self.assertIn(f"{_hhmm(TS)} gid9 · 数学", out)


if __name__ == "__main__":
    unittest.main()
