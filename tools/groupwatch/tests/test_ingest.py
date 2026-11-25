"""ingest.py：JSONL 尾部读取（残行按住）/ 过滤去重 / 攒批。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ingest
import store


def _rec(local_id=1, kind="group", conv="a@chatroom", db="message_0.db",
         table="Msg_x", text="明天带剪刀", create_time=1732000000,
         sender="张三"):
    """monitor 侧记录形状：ts 是 ISO 串，create_time 才是 epoch。"""
    return {"ts": "2026-11-24T10:00:00", "db": db, "table": table,
            "local_id": local_id, "create_time": create_time, "kind": kind,
            "conversation_id": conv, "conversation_name": "三年(2)班",
            "sender_id": "wxid_z", "sender_name": sender, "text": text}


def _line(rec):
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")


class TailTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_tail_full_records_and_offset_advances(self):
        p = self.dir / "messages.jsonl"
        data = _line(_rec(local_id=1)) + _line(_rec(local_id=2))
        p.write_bytes(data)
        res = ingest.tail_jsonl(p, 0)
        self.assertEqual([r["local_id"] for r in res.records], [1, 2])
        self.assertEqual(res.new_offset, len(data))
        self.assertEqual(res.skipped, 0)
        # 从新偏移再读 → 空
        again = ingest.tail_jsonl(p, res.new_offset)
        self.assertEqual((again.records, again.new_offset, again.skipped),
                         ([], len(data), 0))

    def test_tail_partial_last_line_is_held(self):
        # Review Focus 1：写入方分批 append，末行无换行 = 正在写，不许消费。
        p = self.dir / "messages.jsonl"
        full1, full2 = _rec(local_id=1), _rec(local_id=2)
        line2 = _line(full2)
        with open(p, "ab") as f:
            f.write(_line(full1) + line2[:len(line2) // 2])
            f.flush()
        res = ingest.tail_jsonl(p, 0)
        self.assertEqual([r["local_id"] for r in res.records], [1])
        self.assertEqual(res.new_offset, len(_line(full1)))  # 停在最后一个 \n 后
        self.assertEqual(res.skipped, 0)
        with open(p, "ab") as f:
            f.write(line2[len(line2) // 2:])
            f.flush()
        res2 = ingest.tail_jsonl(p, res.new_offset)
        self.assertEqual([r["local_id"] for r in res2.records], [2])
        self.assertEqual(res2.skipped, 0)  # 残行补齐不是坏行
        self.assertEqual(res2.new_offset, len(_line(full1) + line2))

    def test_tail_bad_line_skipped_cursor_moves(self):
        p = self.dir / "messages.jsonl"
        data = _line(_rec(local_id=1)) + b"not-json\n" + _line(_rec(local_id=3))
        p.write_bytes(data)
        res = ingest.tail_jsonl(p, 0)
        self.assertEqual([r["local_id"] for r in res.records], [1, 3])
        self.assertEqual(res.skipped, 1)
        self.assertEqual(res.new_offset, len(data))  # 游标越过坏行

    def test_tail_missing_required_field_skipped(self):
        p = self.dir / "messages.jsonl"
        bad = json.dumps({"kind": "group", "db": "d"}).encode("utf-8") + b"\n"
        data = bad + _line(_rec(local_id=1))
        p.write_bytes(data)
        res = ingest.tail_jsonl(p, 0)
        self.assertEqual([r["local_id"] for r in res.records], [1])
        self.assertEqual(res.skipped, 1)
        self.assertEqual(res.new_offset, len(data))

    def test_tail_missing_create_time_skipped(self):
        # create_time 属必备字段：缺了不能回退 epoch 0（会污染 recent 时序、
        # 立即可被 prune），而是按坏行进 skipped。
        p = self.dir / "messages.jsonl"
        rec = _rec(local_id=1)
        del rec["create_time"]
        data = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8") \
            + _line(_rec(local_id=2))
        p.write_bytes(data)
        res = ingest.tail_jsonl(p, 0)
        self.assertEqual([r["local_id"] for r in res.records], [2])
        self.assertEqual(res.skipped, 1)
        self.assertEqual(res.new_offset, len(data))  # 游标越过

    def test_tail_non_int_create_time_skipped(self):
        # 毒丸行：null/数字串/bool 的 create_time 能过"键存在"校验，却会在
        # ingest_records 的 int(create_time) 处 TypeError——tail 必须按坏行拦下，
        # skipped+1 并让游标越过，绝不让一行脏数据卡死整轮 tick。
        p = self.dir / "messages.jsonl"
        poison = []
        for i, bad in enumerate((None, "1732000000", True)):
            rec = _rec(local_id=i + 1)
            rec["create_time"] = bad
            poison.append((json.dumps(rec, ensure_ascii=False) + "\n").encode())
        data = b"".join(poison) + _line(_rec(local_id=99))
        p.write_bytes(data)
        res = ingest.tail_jsonl(p, 0)
        self.assertEqual([r["local_id"] for r in res.records], [99])
        self.assertEqual(res.skipped, 3)
        self.assertEqual(res.new_offset, len(data))  # 游标越过全部毒丸行

    def test_ingest_records_never_sees_non_int_create_time(self):
        # tail→ingest 契约：经 tail 放行的记录 create_time 必为 int，
        # ingest_records 不再需要防御 TypeError（真 store 端到端跑一遍不抛）。
        p = self.dir / "messages.jsonl"
        bad = _rec(local_id=1)
        bad["create_time"] = None
        data = (json.dumps(bad, ensure_ascii=False) + "\n").encode() \
            + _line(_rec(local_id=2))
        p.write_bytes(data)
        st = store.open_store(self.dir / "groupwatch.db")
        self.addCleanup(st._conn.close)
        res = ingest.tail_jsonl(p, 0)
        got = ingest.ingest_records(st, frozenset({"a@chatroom"}), res.records)
        self.assertEqual([g["local_id"] for g in got], [2])
        self.assertEqual(got[0]["epoch"], 1732000000)

    def test_tail_missing_file(self):
        res = ingest.tail_jsonl(self.dir / "nope.jsonl", 0)
        self.assertEqual((res.records, res.new_offset, res.skipped), ([], 0, 0))


class IngestRecordsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = store.open_store(Path(tmp.name) / "groupwatch.db")
        self.addCleanup(self.store._conn.close)

    def test_ingest_records_filter_dedupe_and_inbox(self):
        # Review Focus 3：非订阅群 / 单聊 / 重复 (db,table,local_id) 各一 + 合法一。
        groups = frozenset({"a@chatroom"})
        good = _rec(local_id=3)
        recs = [
            _rec(local_id=1, kind="single", conv="a@chatroom"),  # 单聊
            _rec(local_id=2, conv="b@chatroom"),                  # 非订阅群
            good,
            dict(good),                                           # 重复三元组
        ]
        got = ingest.ingest_records(self.store, groups, recs)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["source_key"], "message_0.db/Msg_x/3")
        self.assertEqual(got[0]["epoch"], 1732000000)  # epoch = create_time，非 ISO ts
        # 再喂同记录 → 全部已见 → 空
        self.assertEqual(ingest.ingest_records(self.store, groups, [dict(good)]), [])
        self.assertEqual(self.store.counts()["inbox"], 1)
        rows = self.store.recent("a@chatroom", before_ts=10 ** 12, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], 1732000000)
        self.assertEqual(rows[0]["group"], "a@chatroom")
        self.assertEqual(rows[0]["local_id"], 3)
        self.assertEqual(rows[0]["text"], "明天带剪刀")
        self.assertEqual(rows[0]["sender_name"], "张三")

    def test_ingest_records_carry_inbox_rowid(self):
        # C2：accept_message 返回的 inbox rowid 必须挂在接受记录上——watchd 靠
        # 它在批审计后推进 audited 水位（不挂 = 重启恢复永远重复这批）。
        got = ingest.ingest_records(self.store, frozenset({"a@chatroom"}),
                                    [_rec(local_id=5)])
        self.assertEqual(len(got), 1)
        rows = self.store.inbox_after("a@chatroom", 0, 10)
        self.assertEqual(got[0]["rowid"], rows[0]["rowid"])


class BatchBufferTest(unittest.TestCase):
    def test_buffer_flush_by_size(self):
        buf = ingest.BatchBuffer(max_msgs=3, max_wait_s=60)
        out = buf.add("a@chatroom", [_rec(local_id=1), _rec(local_id=2)], now=0.0)
        self.assertEqual(out, [])
        self.assertEqual(buf.pending_count(), 2)
        out = buf.add("a@chatroom", [_rec(local_id=3)], now=1.0)  # 凑满即出
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].group, "a@chatroom")
        self.assertEqual([m["local_id"] for m in out[0].msgs], [1, 2, 3])
        self.assertEqual(buf.pending_count(), 0)
        out = buf.add("a@chatroom", [_rec(local_id=i) for i in range(7)], now=2.0)
        self.assertEqual([len(b.msgs) for b in out], [3, 3])  # 一次可出多批
        self.assertEqual(buf.pending_count(), 1)

    def test_buffer_stale_timer_not_reset_without_emit(self):
        # 回归：持续小流量不得反复重置等待计时——否则 max_wait_s 永不触发。
        buf = ingest.BatchBuffer(max_msgs=3, max_wait_s=60)
        self.assertEqual(buf.add("a@chatroom", [_rec(local_id=1)], now=0.0), [])
        self.assertEqual(buf.add("a@chatroom", [_rec(local_id=2)], now=50.0), [])
        out = buf.flush_stale(61.0)  # r1 自 now=0 已等 61s
        self.assertEqual([(b.group, [m["local_id"] for m in b.msgs]) for b in out],
                         [("a@chatroom", [1, 2])])
        # 真出批后残余才重新计时：now=100 出满批，剩 1 条按 100 起等
        buf2 = ingest.BatchBuffer(max_msgs=3, max_wait_s=60)
        buf2.add("a@chatroom", [_rec(local_id=i) for i in range(4)], now=100.0)
        self.assertEqual(buf2.pending_count(), 1)
        self.assertEqual(buf2.flush_stale(159.9), [])   # 残余按 100 计，未满 60
        self.assertEqual(len(buf2.flush_stale(160.0)), 1)

    def test_buffer_flush_stale_by_time(self):
        buf = ingest.BatchBuffer(max_msgs=100, max_wait_s=60)
        buf.add("a@chatroom", [_rec(local_id=1)], now=100.0)
        self.assertEqual(buf.flush_stale(159.9), [])          # 未满等待期
        self.assertEqual(buf.pending_count(), 1)
        out = buf.flush_stale(160.0)                           # ≥ max_wait_s 出批
        self.assertEqual(len(out), 1)
        self.assertEqual([m["local_id"] for m in out[0].msgs], [1])
        self.assertEqual(buf.pending_count(), 0)
        self.assertEqual(buf.flush_stale(999.0), [])           # 清空后不再重复出

    def test_prime_keeps_original_epoch_and_oldest_ts_timing(self):
        # C2 重启恢复：prime 进缓冲的消息保留原始 epoch，stale 计时从 oldest_ts
        # 起算——重启后第一个 tick 就能把它审掉，不再空等一个 max_wait_s。
        buf = ingest.BatchBuffer(max_msgs=100, max_wait_s=60)
        recs = [dict(_rec(local_id=1), epoch=1000),
                dict(_rec(local_id=2), epoch=1001)]
        buf.prime("a@chatroom", recs, oldest_ts=1000)
        self.assertEqual(buf.pending_count(), 2)
        self.assertEqual([m["epoch"] for m in buf._bufs["a@chatroom"]],
                         [1000, 1001])  # 原始 epoch 原样保留
        self.assertEqual(buf.flush_stale(1059.9), [])
        out = buf.flush_stale(1060.0)
        self.assertEqual([m["local_id"] for m in out[0].msgs], [1, 2])

    def test_prime_merges_into_existing_buffer_earliest_wins(self):
        buf = ingest.BatchBuffer(max_msgs=100, max_wait_s=60)
        buf.add("a@chatroom", [_rec(local_id=9)], now=5000.0)
        buf.prime("a@chatroom", [_rec(local_id=1)], oldest_ts=1000)
        self.assertEqual(buf.pending_count(), 2)
        # 合并序=追加（真实路径 prime 先于 add，本分支只守计时取最早者）：
        out = buf.flush_stale(1060.0)  # 按 oldest_ts=1000 起算已到期
        self.assertEqual([m["local_id"] for m in out[0].msgs], [9, 1])

    def test_drain_all_ignores_size_and_wait(self):
        # C2 once 语义：flush_all 把每个群的残余整批倒出，不看尺寸不看等待。
        buf = ingest.BatchBuffer(max_msgs=100, max_wait_s=60)
        buf.add("a@chatroom", [_rec(local_id=1)], now=0.0)
        buf.add("b@chatroom", [_rec(local_id=2)], now=0.0)
        out = buf.drain_all()
        self.assertEqual(sorted(b.group for b in out),
                         ["a@chatroom", "b@chatroom"])
        self.assertEqual([len(b.msgs) for b in out], [1, 1])
        self.assertEqual(buf.pending_count(), 0)
        self.assertEqual(buf.drain_all(), [])
        self.assertEqual(buf.flush_stale(10 ** 9), [])  # 计时残档一并清掉

    def test_buffer_groups_isolated(self):
        buf = ingest.BatchBuffer(max_msgs=2, max_wait_s=10)
        out = buf.add("a@chatroom", [_rec(local_id=1), _rec(local_id=2)], now=0.0)
        self.assertEqual([b.group for b in out], ["a@chatroom"])  # a 满，b 不受牵连
        buf.add("b@chatroom", [_rec(local_id=5)], now=1.0)
        self.assertEqual(buf.pending_count(), 1)
        out = buf.flush_stale(11.0)
        self.assertEqual([(b.group, [m["local_id"] for m in b.msgs]) for b in out],
                         [("b@chatroom", [5])])


if __name__ == "__main__":
    unittest.main()
