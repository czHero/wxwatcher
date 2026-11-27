import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store


def _intel(group="g@chatroom", keys=("db:t:1", "db:t:2")):
    """insert_intelligence 的 row 样例：键集与 Task 5 Intel 摊平后一致。"""
    return {
        "group": group,
        "item_id": "item-a",
        "title": "标题",
        "detail": "详情",
        "deadline": "明天",
        "action": "跟进",
        "source_keys": list(keys),
        "raw_json": "{}",
    }


def _msg(db="db", table="t", local_id=1, ts=100, group="a@chatroom",
         sender="s", text="hello"):
    return {"db": db, "table": table, "local_id": local_id, "ts": ts,
            "group": group, "sender_name": sender, "text": text}


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "groupwatch.db"
        self.store = store.open_store(self.path)
        self.addCleanup(self.store._conn.close)

    # ---- cursor ----

    def test_cursor_roundtrip(self):
        self.assertEqual(self.store.cursor_get("tail"), 0)  # 缺 0
        self.store.cursor_set("tail", 42)
        self.assertEqual(self.store.cursor_get("tail"), 42)
        self.store.cursor_set("tail", 43)  # 覆盖而非追加
        self.assertEqual(self.store.cursor_get("tail"), 43)

    # ---- messages_seen / inbox ----

    def test_seen_roundtrip_and_inbox_idempotent(self):
        m = _msg()
        self.assertFalse(self.store.is_seen("db", "t", 1))
        self.store.mark_seen("db", "t", 1)
        self.assertTrue(self.store.is_seen("db", "t", 1))
        self.assertFalse(self.store.is_seen("db", "t", 2))
        self.store.insert_inbox(m)
        self.store.insert_inbox(m)  # INSERT OR IGNORE：同 (db,table,local_id) 不重复
        self.assertEqual(self.store.counts()["inbox"], 1)

    # ---- recent ----

    def test_recent_order_ascending_and_group_isolated(self):
        for i, ts in enumerate((100, 300, 200)):
            self.store.insert_inbox(_msg(local_id=i + 1, ts=ts,
                                         group="a@chatroom"))
        self.store.insert_inbox(_msg(local_id=10, ts=150, group="b@chatroom"))
        self.store.insert_inbox(_msg(local_id=11, ts=250, group="b@chatroom"))
        got = self.store.recent("a@chatroom", before_ts=250, limit=2)
        self.assertEqual([r["ts"] for r in got], [100, 200])  # ts<250 最近 2 条
        self.assertTrue(all(r["group"] == "a@chatroom" for r in got))
        got = self.store.recent("a@chatroom", 1000, 2)
        self.assertEqual([r["ts"] for r in got], [200, 300])  # 返回升序
        self.assertIsInstance(got[0], dict)
        self.assertEqual(got[0]["text"], "hello")

    # ---- intelligence ----

    def test_insert_intelligence_unique_source_hash(self):
        iid = self.store.insert_intelligence(_intel(keys=("k1", "k2")))
        self.assertIsInstance(iid, int)
        dup = self.store.insert_intelligence(_intel(keys=("k2", "k1")))
        self.assertIsNone(dup)  # 同集合顺序不同 → 同哈希 → 冲突不写
        self.assertEqual(self.store.counts()["intelligence"], 1)
        other = self.store.insert_intelligence(_intel(keys=("k1", "k3")))
        self.assertIsInstance(other, int)
        self.assertEqual(self.store.counts()["intelligence"], 2)

    def test_pending_bump_retry_mark_status(self):
        iid = self.store.insert_intelligence(_intel())
        got = self.store.pending(10)
        self.assertEqual([r["id"] for r in got], [iid])
        self.assertEqual(got[0]["pushed"], "pending")
        self.assertEqual(self.store.bump_retry(iid), 1)  # 返回新值
        self.assertEqual(self.store.bump_retry(iid), 2)
        self.assertEqual(self.store.pending(1)[0]["retries"], 2)
        self.store.mark_status(iid, "sent")
        self.assertEqual(self.store.pending(10), [])
        self.store.mark_status(iid, "hold")
        self.assertEqual(self.store.counts()["hold"], 1)
        self.store.mark_status(iid, "suppressed")
        self.assertEqual(self.store.counts()["suppressed"], 1)

    def test_day_intels_filters_by_local_date(self):
        iid = self.store.insert_intelligence(_intel())
        today = date.fromtimestamp(time.time()).isoformat()
        got = self.store.day_intels(today)
        self.assertEqual([r["id"] for r in got], [iid])
        self.assertEqual(self.store.day_intels("1999-01-01"), [])

    # ---- digest_fired ----

    def test_try_fire_digest_once(self):
        self.assertTrue(self.store.try_fire_digest("2026-11-24", "08:00"))
        self.assertFalse(self.store.try_fire_digest("2026-11-24", "08:00"))
        self.assertTrue(self.store.try_fire_digest("2026-11-24", "20:30"))
        self.assertTrue(self.store.try_fire_digest("2026-11-25", "08:00"))

    def test_digest_unfire_reclaims_slot(self):
        # C1：推送失败的 digest 不算已发——unfire 还位后同 slot 可再 fire。
        self.assertTrue(self.store.try_fire_digest("2026-11-24", "20:30"))
        self.store.digest_unfire("2026-11-24", "20:30")
        self.assertTrue(self.store.try_fire_digest("2026-11-24", "20:30"))
        # unfire 精确到 (day, hhmm)：邻位仍被占用，重复 unfire / unfire 不
        # 存在的行都是无害 no-op。
        self.assertTrue(self.store.try_fire_digest("2026-11-24", "21:00"))
        self.store.digest_unfire("2026-11-24", "20:30")
        self.assertFalse(self.store.try_fire_digest("2026-11-24", "21:00"))
        self.assertTrue(self.store.try_fire_digest("2026-11-24", "20:30"))
        self.store.digest_unfire("1999-01-01", "00:00")

    # ---- digest_pending（F2 持久补发队列）----

    def _digest_fired_pairs(self):
        rows = self.store._conn.execute(
            "SELECT day, hhmm FROM digest_fired ORDER BY day, hhmm").fetchall()
        return [(r["day"], r["hhmm"]) for r in rows]

    def test_digest_pending_enqueue_idempotent_and_oldest(self):
        self.assertEqual(self.store.pending_digest_count(), 0)
        self.assertIsNone(self.store.oldest_digest_pending())
        self.assertTrue(self.store.enqueue_digest("2026-11-25", "20:30",
                                                  "汇总A", 100))
        # PK 撞已排队：False，且不覆盖原文本
        self.assertFalse(self.store.enqueue_digest("2026-11-25", "20:30",
                                                   "汇总A改", 999))
        self.assertEqual(self.store.oldest_digest_pending()["text"], "汇总A")
        self.assertTrue(self.store.enqueue_digest("2026-11-25", "21:00",
                                                  "汇总B", 100))
        self.assertEqual(self.store.pending_digest_count(), 2)
        # 队首 = created_ts 最早（入队序）；同 ts（同 tick 齐排）按 day, hhmm
        # 升序破平 → 20:30 先于 21:00
        self.assertTrue(self.store.enqueue_digest("2026-11-26", "08:00",
                                                  "次日早班", 150))
        oldest = self.store.oldest_digest_pending()
        # F2-round2 后队首行携带 state（新槽位默认 'pending'）
        self.assertEqual(set(oldest), {"day", "hhmm", "text", "created_ts",
                                       "state"})
        self.assertEqual(oldest["state"], "pending")
        self.assertEqual((oldest["day"], oldest["hhmm"], oldest["text"],
                          oldest["created_ts"]),
                         ("2026-11-25", "20:30", "汇总A", 100))
        self.store.resolve_digest("2026-11-25", "20:30")
        self.assertEqual(self.store.pending_digest_count(), 2)
        self.assertEqual(self.store.oldest_digest_pending()["hhmm"], "21:00")
        # 前一日槽位补完 → 次日队首
        self.store.resolve_digest("2026-11-25", "21:00")
        self.assertEqual(self.store.oldest_digest_pending()["day"],
                         "2026-11-26")

    def test_digest_resolve_finalizes_and_has_record(self):
        # resolve = 出队 + 定稿（digest_fired 加行）；has_digest_record 覆盖
        # 两表——已排队或已定稿都算"已处理"（enqueue 守卫，防重排/重发）。
        self.assertFalse(self.store.has_digest_record("2026-11-25", "20:30"))
        self.store.enqueue_digest("2026-11-25", "20:30", "汇总", 100)
        self.assertTrue(self.store.has_digest_record("2026-11-25", "20:30"))
        self.assertEqual(self._digest_fired_pairs(), [])  # 排队 ≠ 已发
        self.store.resolve_digest("2026-11-25", "20:30")
        self.assertEqual(self.store.pending_digest_count(), 0)
        self.assertEqual(self._digest_fired_pairs(),
                         [("2026-11-25", "20:30")])
        self.assertTrue(self.store.has_digest_record("2026-11-25", "20:30"))
        # 定稿幂等：再 resolve 一次不产生第二行（PK + INSERT OR IGNORE）
        self.store.resolve_digest("2026-11-25", "20:30")
        self.assertEqual(self._digest_fired_pairs(),
                         [("2026-11-25", "20:30")])

    def test_has_digest_record_covers_both_tables(self):
        # 只在 fired（历史定稿）→ True；只在 pending（在队）→ True；皆无 → False
        self.store.try_fire_digest("2026-11-24", "08:00")
        self.assertTrue(self.store.has_digest_record("2026-11-24", "08:00"))
        self.store.enqueue_digest("2026-11-24", "09:00", "t", 1)
        self.assertTrue(self.store.has_digest_record("2026-11-24", "09:00"))
        self.assertFalse(self.store.has_digest_record("2026-11-24", "10:00"))
        self.assertEqual(self.store.pending_digest_count(), 1)

    # ---- digest_pending 状态机（F2-round2：歧义结局 held 永不自动重发）----

    def test_hold_digest_excludes_from_fifo_and_surfaces_held(self):
        self.assertEqual(self.store.held_digest_count(), 0)
        self.assertEqual(self.store.held_digests(), [])
        self.store.enqueue_digest("2026-11-25", "20:30", "A", 100)
        self.store.enqueue_digest("2026-11-25", "21:00", "B", 110)
        self.store.hold_digest("2026-11-25", "20:30")
        # held 被 FIFO 排除；待补发计数只数 state='pending'
        self.assertEqual(self.store.oldest_digest_pending()["hhmm"], "21:00")
        self.assertEqual(self.store.pending_digest_count(), 1)
        self.assertEqual(self.store.held_digest_count(), 1)
        held = self.store.held_digests()
        self.assertEqual([(h["day"], h["hhmm"], h["state"]) for h in held],
                         [("2026-11-25", "20:30", "held")])
        # held 行仍是"已处理"记录：不会被重新渲染入队（has_record 覆盖任意态）
        self.assertTrue(self.store.has_digest_record("2026-11-25", "20:30"))
        self.assertFalse(self.store.enqueue_digest("2026-11-25", "20:30",
                                                   "A2", 200))
        self.assertEqual(self.store.held_digests()[0]["text"], "A")  # 不覆盖

    def test_resolve_finalizes_held_row_too(self):
        # 人工裁决"确认已送达"= resolve 语义：held 行同样删除 + 进 digest_fired。
        self.store.enqueue_digest("2026-11-25", "09:12", "歧义条", 100)
        self.store.hold_digest("2026-11-25", "09:12")
        self.store.resolve_digest("2026-11-25", "09:12")
        self.assertEqual(self.store.held_digest_count(), 0)
        self.assertEqual(self.store.pending_digest_count(), 0)
        self.assertEqual(self._digest_fired_pairs(), [("2026-11-25", "09:12")])

    def test_fired_digests_lists_day_slots_sorted(self):
        # F2-round4：status 点名"已发送"要用 fired_digests(day)——只回该日
        # digest_fired 的 hhmm 升序列表；排队未定稿的行不算，别日行不掺。
        self.assertEqual(self.store.fired_digests("2026-11-27"), [])  # 空表
        self.store.enqueue_digest("2026-11-27", "20:30", "A", 100)
        self.assertEqual(self.store.fired_digests("2026-11-27"), [])  # 排队≠已发
        self.store.resolve_digest("2026-11-27", "20:30")
        self.store.resolve_digest("2026-11-27", "08:00")  # 从未排队也可定稿
        self.store.resolve_digest("2026-11-26", "21:00")  # 别日，不得掺入
        self.assertEqual(self.store.fired_digests("2026-11-27"),
                         ["08:00", "20:30"])
        self.assertEqual(self.store.fired_digests("2026-11-26"), ["21:00"])

    def test_open_store_alters_pre_round2_schema(self):
        # 实况库（2026-11-26 事故机）建表于 round-1：digest_pending 无 state
        # 列。open_store 必须 ALTER 补列，旧行按 DEFAULT 落到 'pending'
        # （恢复可重试，无 held 语义），既有行/主键守卫原样保留，重复打开幂等。
        legacy = self.path.parent / "legacy-round1.db"
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE digest_pending ("
                     "day TEXT, hhmm TEXT, text TEXT, created_ts INTEGER, "
                     "PRIMARY KEY (day, hhmm))")
        conn.execute("INSERT INTO digest_pending VALUES"
                     "('2026-11-25','20:30','旧行',100)")
        conn.commit()
        conn.close()
        st2 = store.open_store(legacy)
        self.addCleanup(st2._conn.close)
        cols = {r[1] for r in st2._conn.execute(
            "PRAGMA table_info(digest_pending)")}
        self.assertIn("state", cols)
        job = st2.oldest_digest_pending()
        self.assertEqual((job["text"], job["state"]), ("旧行", "pending"))
        self.assertEqual(st2.pending_digest_count(), 1)
        self.assertEqual(st2.held_digest_count(), 0)
        self.assertFalse(st2.enqueue_digest("2026-11-25", "20:30", "新", 200))
        st2.hold_digest("2026-11-25", "20:30")
        self.assertIsNone(st2.oldest_digest_pending())
        # 再次打开：迁移幂等，不报错不重跑
        st3 = store.open_store(legacy)
        self.addCleanup(st3._conn.close)
        self.assertEqual(st3.held_digest_count(), 1)

    # ---- dispatches / audit_log ----

    def test_log_dispatch_and_recent_dispatches(self):
        self.store.log_dispatch("realtime", "ok", "sent 1")
        self.store.log_dispatch("digest", "fail", "boom")
        self.store.log_dispatch("realtime", "ok", "sent 2")
        got = self.store.recent_dispatches(2)
        self.assertEqual(len(got), 2)
        self.assertEqual([r["kind"] for r in got], ["realtime", "digest"])  # 最新在前
        self.assertEqual(got[0]["detail"], "sent 2")
        self.assertEqual(got[0]["status"], "ok")
        self.assertIsInstance(got[0]["ts"], int)

    def test_log_audit_persists(self):
        self.store.log_audit("a@chatroom", n_msgs=5, hits=2, status="ok")
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        rows = conn.execute('SELECT ts, "group", n_msgs, hits, status '
                            "FROM audit_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:], ("a@chatroom", 5, 2, "ok"))

    def test_last_dispatch_filters_by_kind(self):
        # F1：跨类型去重遮蔽的根修——按 kind 取最新一笔，别的 kind 更新的行
        # 不得遮蔽（20:30 digest pre_fail 曾被 realtime 行的"最新一笔"吞掉）。
        self.assertIsNone(self.store.last_dispatch("digest"))  # 空表
        self.store.log_dispatch("digest", "pre_fail", "d1")
        self.store.log_dispatch("realtime", "pre_fail", "r1")
        self.store.log_dispatch("digest", "ok", "d2")
        self.store.log_dispatch("realtime", "ok", "r2")
        last = self.store.last_dispatch("digest")
        self.assertIsInstance(last, dict)
        self.assertEqual((last["kind"], last["status"], last["detail"]),
                         ("digest", "ok", "d2"))  # 同 kind 内最新在前，r2 不遮蔽
        last = self.store.last_dispatch("realtime")
        self.assertEqual((last["kind"], last["status"], last["detail"]),
                         ("realtime", "ok", "r2"))
        self.assertIsNone(self.store.last_dispatch("nope"))  # 无此 kind → None

    # ---- counts ----

    def test_counts_shape(self):
        self.store.insert_inbox(_msg())
        iid = self.store.insert_intelligence(_intel())
        self.store.log_dispatch("realtime", "ok", "")
        self.store.mark_status(iid, "hold")
        got = self.store.counts()
        self.assertEqual(set(got), {"inbox", "intelligence", "pending",
                                    "hold", "suppressed", "dispatches"})
        self.assertEqual(got, {"inbox": 1, "intelligence": 1, "pending": 0,
                               "hold": 1, "suppressed": 0, "dispatches": 1})

    # ---- prune_inbox ----

    def test_prune_inbox_keeps_recent_and_intelligence(self):
        now = int(time.time())
        day = 86400
        old = _msg(local_id=1, ts=now - 10 * day)
        recent = _msg(local_id=2, ts=now - 1 * day)
        for m in (old, recent):
            self.store.insert_inbox(m)
            self.store.mark_seen(m["db"], m["table"], m["local_id"])
        iid = self.store.insert_intelligence(_intel())
        self.store.prune_inbox(now, retention_days=7)
        self.assertEqual(self.store.counts()["inbox"], 1)
        self.assertEqual(self.store.counts()["intelligence"], 1)  # 情报不动
        self.assertFalse(self.store.is_seen("db", "t", 1))  # 旧 seen 删除
        self.assertTrue(self.store.is_seen("db", "t", 2))
        self.assertEqual([r["id"] for r in self.store.pending(10)], [iid])

    # ---- accept_message / inbox_after（C2 audited watermark 基座）----

    def test_accept_message_writes_both_rows_and_returns_rowid(self):
        rid = self.store.accept_message(_msg(local_id=7))
        self.assertIsInstance(rid, int)
        self.assertTrue(self.store.is_seen("db", "t", 7))
        self.assertEqual(self.store.counts()["inbox"], 1)
        again = self.store.accept_message(_msg(local_id=7))
        self.assertEqual(again, rid)  # INSERT OR IGNORE 撞已有行 → 仍回原 rowid
        self.assertEqual(self.store.counts()["inbox"], 1)

    def test_accept_message_atomic_single_transaction(self):
        # C2：seen 标记与 inbox 落库同一条事务、同生死——向 inbox 插入注错，
        # 回滚后不得留半截（否则消息被 is_seen 永久跳过而正文根本没落仓 =
        # 静默丢失，重启恢复也无从谈起）。
        real = self.store._conn

        class _Flaky:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *a, **k):
                if "INTO inbox" in sql:
                    raise sqlite3.OperationalError("模拟落盘中途崩溃")
                return self._conn.execute(sql, *a, **k)

            def rollback(self):
                self._conn.rollback()

            def commit(self):
                self._conn.commit()

        self.store._conn = _Flaky(real)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.accept_message(_msg(local_id=8))
        finally:
            self.store._conn = real
        self.assertFalse(self.store.is_seen("db", "t", 8))
        self.assertEqual(self.store.counts()["inbox"], 0)
        # 回滚必须彻底：后续别的写入不得把半截事务带出来
        self.store.cursor_set("x", 1)
        self.assertFalse(self.store.is_seen("db", "t", 8))
        self.assertEqual(self.store.counts()["inbox"], 0)

    def test_inbox_after_rowid_asc_group_isolated_with_limit(self):
        for i in (1, 2, 3, 4):
            self.store.accept_message(_msg(local_id=i, ts=100 + i))
        self.store.accept_message(_msg(local_id=9, group="b@chatroom", ts=111))
        got = self.store.inbox_after("a@chatroom", 0, 3)
        self.assertEqual([r["local_id"] for r in got], [1, 2, 3])  # rowid 升序
        self.assertTrue(all(isinstance(r["rowid"], int) for r in got))
        tail = self.store.inbox_after("a@chatroom", got[-1]["rowid"], 10)
        self.assertEqual([r["local_id"] for r in tail], [4])
        self.assertEqual({r["group"] for r in tail}, {"a@chatroom"})  # 群隔离
        self.assertEqual(tail[0]["ts"], 104)

    # ---- open_store ----

    def test_open_store_idempotent_and_durable(self):
        self.store.cursor_set("tail", 7)
        again = store.open_store(self.path)  # 重复建表不报错
        self.addCleanup(again._conn.close)
        self.assertEqual(again.cursor_get("tail"), 7)  # 写入已提交可见


if __name__ == "__main__":
    unittest.main()
