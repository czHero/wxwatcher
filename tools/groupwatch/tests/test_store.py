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
