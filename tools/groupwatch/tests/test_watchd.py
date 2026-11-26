"""watchd.py：one_tick 主管道 / digest 排程 / flock / run_forever / build_deps 装配。

测试合同：真 store(tmp)+真 ingest+真 render+真 audit（模板/校验全走真代码），
chat、push、时钟、重试 sleep 全部注入假件；无网络、无子进程、无截图。
"""
import dataclasses
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stderr
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch
import ingest
import modelclient
import store as store_mod
import watchd
from config import (Config, ConfigError, IngestCfg, Limits, ModelRole, PushCfg,
                    WatchItem, ensure_home)

ITEM = WatchItem("hw", "作业", "老师布置的作业与需携带物品")
GROUP = "a@chatroom"
GNAME = "三年(2)班"
JUDGE_ROLE = ModelRole("https://invalid.example/v1", "judge-model", "k-j")
EXTRACT_ROLE = ModelRole("https://invalid.example/v1", "extract-model", "k-e")
JUDGE_TPL = "j系统\n===\nj用户 {watchitems} {group} {batch}"
EXTRACT_TPL = "e系统\n===\ne用户 {watchitems} {group} {batch} {hits} {context}"

_HOMEWORK = "明天记得带剪刀到教室"
_JUDGE_HITS = [{"item_id": "hw", "msg_ids": [2], "reason": "要求携带工具"}]
_EXTRACT_ITEMS = [{"item_id": "hw", "relevant": True, "title": "明天带剪刀",
                   "detail": "手工课要剪刀", "deadline": "明天上午",
                   "action": "带一把剪刀", "excerpts": [_HOMEWORK]}]


def _rec(local_id, text="水群闲聊", create_time=1732000000, conv=GROUP,
         name=GNAME, kind="group"):
    return {"ts": "2026-11-24T10:00:00", "db": "message_0.db", "table": "Msg_x",
            "local_id": local_id, "create_time": create_time, "kind": kind,
            "conversation_id": conv, "conversation_name": name,
            "sender_id": "wxid_z", "sender_name": "张三", "text": text}


def _append(path, *recs):
    with open(path, "ab") as f:
        for r in recs:
            f.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))


def _append_raw(path, raw: bytes):
    with open(path, "ab") as f:
        f.write(raw)


def _cfg(jsonl, *, realtime=True, digest_enabled=True,
         digest_times=("20:30",), poll=5, batch_max_msgs=3):
    home = Path(jsonl).parent
    return Config(home, {"judge": JUDGE_ROLE, "extract": EXTRACT_ROLE},
                  IngestCfg(Path(jsonl), poll, batch_max_msgs, 60),
                  PushCfg("self", realtime, digest_enabled, digest_times,
                          Path("wxsend.py")),
                  Limits(3, 10, 30, 20), home / "prompts")


def _chat(judge_hits=None, extract_items=None, *, fail=False):
    """按 role.model 分派的假 chat；fail=True 则一律抛 ModelError。"""
    calls = {"judge": 0, "extract": 0}

    def chat(role, system, user, *, retries=3):
        key = "judge" if role.model == "judge-model" else "extract"
        calls[key] += 1
        if fail:
            raise modelclient.ModelError("模拟模型服务不可用")
        if key == "judge":
            return json.dumps({"hits": judge_hits if judge_hits is not None
                               else []}, ensure_ascii=False)
        return json.dumps({"items": extract_items if extract_items is not None
                           else []}, ensure_ascii=False)
    return chat, calls


class _TickBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.jsonl = self.home / "messages.jsonl"
        self.jsonl.write_bytes(b"")
        self.store = store_mod.open_store(self.home / "groupwatch.db")
        self.addCleanup(self.store._conn.close)
        self.pushed = []

        def fake_push(jobs, kind):
            """假件按真 dispatch 的状态迁移建模：sent→mark_status+log ok；
            lock_busy/pre_fail 保持 pending 只记流水——否则"pending 每 tick
            续拉"的正确行为会被假件失真成"永远重复推"。"""
            self.pushed.append((jobs, kind))
            status = self.push_status
            if status == "ok":
                for oid, _text in jobs:
                    if oid is not None:
                        self.store.mark_status(oid, "sent")
                self.store.log_dispatch(kind, "ok", f"{len(jobs)} 条")
            elif status == "pre_fail":
                self.store.log_dispatch(kind, "pre_fail", "闸门未过（测试注入）")
            elif status.startswith("stopped:"):
                self.store.log_dispatch(kind, status.split(":", 1)[1],
                                        "批次中止（测试注入）")
            return status
        self.fake_push = fake_push
        self.push_status = "ok"
        # 默认钟钉在"本地当日 10:00"：早于任何 digest 时点，让无 digest 意图的
        # 测试在任何时区都确定地不碰 digest 段（历史值 0.0 在 UTC=本地 时区下
        # day=1970-01-01 会误命中当日 20:30 排程——UTC+8 机器上此前被掩盖）。
        day = time.strftime("%Y-%m-%d", time.localtime())
        self.clock = [time.mktime(time.strptime(f"{day} 10:00",
                                                "%Y-%m-%d %H:%M"))]

    def make_deps(self, cfg, chat):
        return watchd.Deps(cfg, self.store, (ITEM,), frozenset({GROUP}),
                           ingest.BatchBuffer(cfg.ingest.batch_max_msgs,
                                              cfg.ingest.batch_max_wait_s),
                           JUDGE_TPL, EXTRACT_TPL,
                           chat, self.fake_push, lambda: self.clock[0], {},
                           sleep=lambda s: None)

    def seed_home(self, *, models=None, prompts=True, batch_max_msgs=3,
                  digest_enabled=True, apis=None):
        """build_deps 用的真 home：config/watchitems/groups/prompts/key 全套。
        apis={"judge": "systemone", ...} 写 models.<role>.api（accept-fix）。"""
        ensure_home(self.home)
        keys = {"judge": "judge.key", "extract": "extract.key"}
        (self.home / "judge.key").write_text("kkk")
        (self.home / "extract.key").write_text("kkk")
        models_json = {}
        for r in (models or ("judge", "extract")):
            entry = {"base_url": "https://invalid.example/v1",
                     "model": f"{r}-model", "api_key_file": keys[r]}
            if apis and r in apis:
                entry["api"] = apis[r]
            models_json[r] = entry
        cfg = {"models": models_json,
               "ingest": {"jsonl": str(self.jsonl), "poll_interval_s": 5,
                          "batch_max_msgs": batch_max_msgs,
                          "batch_max_wait_s": 60},
               "push": {"target_roster_key": "self", "realtime": True,
                        "digest": {"enabled": digest_enabled,
                                   "times": ["20:30"]}},
               "limits": {"max_retries": 3, "push_max_per_tick": 10,
                          "inbox_retention_days": 30}}
        (self.home / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        (self.home / "watchitems.json").write_text(
            json.dumps([{"id": "hw", "name": "作业", "desc": "作业"}],
                       ensure_ascii=False), encoding="utf-8")
        (self.home / "groups.json").write_text(
            json.dumps({"subscribe": [GROUP]}, ensure_ascii=False),
            encoding="utf-8")
        if prompts:
            pd = self.home / "prompts"
            pd.mkdir()
            # home 副本内容刻意不同于 repo 默认模板——build_deps 必须读 home。
            (pd / "judge.md").write_text("HOME-JUDGE\n===\n{watchitems}{group}{batch}",
                                         encoding="utf-8")
            (pd / "extract.md").write_text(
                "HOME-EXTRACT\n===\n{watchitems}{group}{batch}{hits}{context}",
                encoding="utf-8")

    def audit_statuses(self):
        with closing(sqlite3.connect(str(self.home / "groupwatch.db"))) as conn:
            return conn.execute(
                "SELECT status, hits FROM audit_log ORDER BY id").fetchall()

    def digest_fired_rows(self):
        with closing(sqlite3.connect(str(self.home / "groupwatch.db"))) as conn:
            return conn.execute(
                "SELECT day, hhmm FROM digest_fired ORDER BY day, hhmm").fetchall()


class OneTickEndToEndTest(_TickBase):
    def test_one_tick_end_to_end_dry(self):
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        chat, calls = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        result = watchd.one_tick(deps)
        self.assertEqual(result["ingested"], 3)
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["intel_new"], 1)
        self.assertEqual(result["push"], "ok")
        self.assertIs(result["pre"], True)
        self.assertNotIn("note", result)  # 正常 tick 不带截断提示
        # 推送成功：pending 清零，dispatch 流水 ok
        self.assertEqual(len(self.store.pending(10)), 0)
        rows = self.store.day_intels(time.strftime("%Y-%m-%d", time.localtime()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["source_keys"]),
                         ["message_0.db/Msg_x/3"])
        raw = json.loads(rows[0]["raw_json"])
        self.assertEqual(raw["excerpts"], [_HOMEWORK])
        self.assertEqual(raw["item_id"], "hw")
        # 推送作业渲染与 kind
        self.assertEqual(len(self.pushed), 1)
        jobs, kind = self.pushed[0]
        self.assertEqual(kind, "realtime")
        self.assertEqual(len(jobs), 1)
        oid, text = jobs[0]
        self.assertEqual(oid, rows[0]["id"])
        self.assertEqual([d["status"] for d in self.store.recent_dispatches(1)],
                         ["ok"])
        self.assertIn("【作业 · 三年(2)班】明天带剪刀", text)
        self.assertIn("截止：明天上午", text)
        # 游标推进到 EOF；audit_log 成功态
        self.assertEqual(self.store.cursor_get("jsonl"),
                         self.jsonl.stat().st_size)
        self.assertEqual(self.audit_statuses(), [("ok", 1)])
        # addendum 8：群名从记录进内存
        self.assertEqual(deps.group_names, {GROUP: GNAME})

    def test_group_name_falls_back_to_raw_gid(self):
        # 记录未带 conversation_name → group_names 不更新，渲染回落 raw gid。
        _append(self.jsonl, _rec(1, name=""), _rec(2, name=""),
                _rec(3, name="", text=_HOMEWORK))
        chat, _ = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        watchd.one_tick(deps)
        self.assertEqual(deps.group_names, {})
        self.assertIn(f"【作业 · {GROUP}】", self.pushed[0][0][0][1])

    def test_seen_dedup_on_manual_cursor_reset_replay(self):
        # tail 层去重重钉（原"崩溃模型"保留为 seen 过滤回归）：游标手工回 0，
        # 同一 jsonl 重读被 is_seen 全部拦下——accept_message 事务化后此路仍在。
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        chat, calls = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        first = watchd.one_tick(deps)
        self.assertEqual(first["intel_new"], 1)
        # 模拟游标重置：重放同一 jsonl（seen 表在，同库）
        self.store.cursor_set("jsonl", 0)
        deps2 = self.make_deps(_cfg(self.jsonl), chat)
        second = watchd.one_tick(deps2)
        self.assertEqual(second["ingested"], 0)
        self.assertEqual(second["batches"], 0)
        self.assertEqual(second["intel_new"], 0)
        self.assertEqual(len(self.pushed), 1)  # push 无新 job
        self.assertEqual(self.store.counts()["intelligence"], 1)
        self.assertEqual(calls["extract"], 1)  # 重放不重审

    def test_replay_after_crash_no_duplicate_intel(self):
        # 诚实崩溃模型（终审 C2）：jsonl 游标早已推过缓冲中的消息，崩溃丢的是
        # 易失缓冲——重启 = build_deps 沿 audited 水位从 inbox 找回未审计批。
        # deps1 tick 只摄取不成批；同 home/store/jsonl 的新 deps2 tick → 情报
        # 恰好产出一二次校验：产出一次 + 推送恰好尝试一次；第三 tick → 零重复。
        self.seed_home(batch_max_msgs=5)
        _append(self.jsonl, _rec(1), _rec(2, text=_HOMEWORK))
        hits = [{"item_id": "hw", "msg_ids": [1], "reason": "要求携带工具"}]
        chat, calls = _chat(hits, _EXTRACT_ITEMS)

        def ok_push(jobs, kind):
            self.pushed.append((jobs, kind))
            for oid, _t in jobs:
                if oid is not None:
                    self.store.mark_status(oid, "sent")
            self.store.log_dispatch(kind, "ok", f"{len(jobs)} 条")
            return "ok"

        # deps1：游标推到 EOF，2 条滞留内存缓冲（旧模型在这里静默丢消息）
        deps1 = self.make_deps(_cfg(self.jsonl, batch_max_msgs=5), chat)
        r1 = watchd.one_tick(deps1)
        self.assertEqual((r1["ingested"], r1["batches"], r1["intel_new"]),
                         (2, 0, 0))
        self.assertEqual(self.store.cursor_get("jsonl"),
                         self.jsonl.stat().st_size)
        self.assertEqual(calls["judge"], 0)

        # "崩溃"→ 全新装配：同 home/store/jsonl，批从 inbox 恢复
        deps2 = watchd.build_deps(self.home, chat=chat, push=ok_push,
                                  now=lambda: 1732000200.0,
                                  contacts_loader=lambda: {})
        self.addCleanup(deps2.store._conn.close)
        self.assertEqual(deps2.recovered, 2)  # Deps 暴露恢复条数
        rec0 = deps2.buf._bufs[GROUP][0]  # 恢复记录与 ingest_records 同形状
        self.assertEqual(rec0["source_key"], "message_0.db/Msg_x/1")
        self.assertEqual(rec0["epoch"], 1732000000)  # 原始 epoch 保留
        self.assertEqual(rec0["conversation_id"], GROUP)
        self.assertEqual(rec0["rowid"], 1)
        r2 = watchd.one_tick(deps2)
        self.assertEqual((r2["batches"], r2["intel_new"], r2["push"]),
                         (1, 1, "ok"))
        self.assertEqual(calls["extract"], 1)  # 恰好产出一次
        self.assertEqual(self.store.counts()["intelligence"], 1)
        self.assertEqual(self.store.counts()["inbox"], 2)  # 恢复零重插
        self.assertEqual([k for _j, k in self.pushed], ["realtime"])  # 尝试发生
        self.assertEqual(self.store.pending(10), [])
        self.assertEqual(self.store.cursor_get(f"audited:{GROUP}"), 2)

        # 第三 tick：无新料、无残余、无 pending → 零重复
        r3 = watchd.one_tick(deps2)
        self.assertEqual((r3["ingested"], r3["batches"], r3["intel_new"],
                          r3["push"]), (0, 0, 0, None))
        self.assertEqual(calls["extract"], 1)
        self.assertEqual(len(self.pushed), 1)

    def test_crash_after_audit_before_push_never_reaudits_or_resends_hold(self):
        # 安全红线（C2 审查焦点）：水位在"审计尝试"时前进、不在"推送成功"时
        # 前进。审计已落库、推送半途 hold：重启后既不再审（source_hash +
        # 水位双保险），hold 行也绝不回流续推——已尝试的消息永不自动重发。
        self.seed_home(batch_max_msgs=5)
        _append(self.jsonl, _rec(1), _rec(2, text=_HOMEWORK))
        hits = [{"item_id": "hw", "msg_ids": [1], "reason": "要求携带工具"}]
        chat, calls = _chat(hits, _EXTRACT_ITEMS)

        def hold_push(jobs, kind):
            self.pushed.append((jobs, kind))
            for oid, _t in jobs:
                if oid is not None:
                    self.store.mark_status(oid, "hold")
            self.store.log_dispatch(kind, "needs_check", "模拟挂起")
            return "stopped:needs_check"

        deps1 = self.make_deps(_cfg(self.jsonl, batch_max_msgs=5), chat)
        watchd.one_tick(deps1)
        deps2 = watchd.build_deps(self.home, chat=chat, push=hold_push,
                                  now=lambda: 1732000200.0,
                                  contacts_loader=lambda: {})
        self.addCleanup(deps2.store._conn.close)
        self.assertEqual(deps2.recovered, 2)
        r2 = watchd.one_tick(deps2)  # 审计提交、推送挂起：崩溃点
        self.assertEqual(r2["intel_new"], 1)
        self.assertEqual(self.store.counts()["hold"], 1)
        self.assertEqual(self.store.cursor_get(f"audited:{GROUP}"), 2)

        # 再一次"重启"：水位已越过这两行 → 无可恢复；hold 行不在 pending
        # 续拉里 → 既不重审也不重发。
        deps3 = watchd.build_deps(self.home, chat=chat, push=hold_push,
                                  now=lambda: 1732000300.0,
                                  contacts_loader=lambda: {})
        self.addCleanup(deps3.store._conn.close)
        self.assertEqual(deps3.recovered, 0)
        r3 = watchd.one_tick(deps3)
        self.assertEqual((r3["batches"], r3["intel_new"]), (0, 0))
        self.assertEqual(calls["extract"], 1)  # 不再审
        self.assertEqual(len(self.pushed), 1)  # hold 永不自动续推

    def test_once_flushes_subbatch_remainder(self):
        # C2 契约：one_tick(flush_all=True)（once 子命令用）把不足一批的残余
        # 直接判掉——不让用户干等 batch_max_msgs/batch_max_wait_s。
        _append(self.jsonl, _rec(1, text=_HOMEWORK))
        chat, calls = _chat(_JUDGE_HITS2, _EXTRACT_ITEMS2)
        deps = self.make_deps(_cfg(self.jsonl, batch_max_msgs=40), chat)
        r = watchd.one_tick(deps, flush_all=True)
        self.assertEqual((r["batches"], r["intel_new"], r["push"]), (1, 1, "ok"))
        self.assertEqual(calls["extract"], 1)

    def test_tick_without_flush_all_keeps_remainder(self):
        # 对照组：默认 flush_all=False（run_forever 语义）不动未到期残余。
        _append(self.jsonl, _rec(1, text=_HOMEWORK))
        chat, _ = _chat(_JUDGE_HITS2, _EXTRACT_ITEMS2)
        deps = self.make_deps(_cfg(self.jsonl, batch_max_msgs=40), chat)
        r = watchd.one_tick(deps)
        self.assertEqual((r["batches"], r["intel_new"]), (0, 0))
        self.assertEqual(deps.buf.pending_count(), 1)

    def test_duplicate_intel_counted_not_pushed(self):
        # source_hash UNIQUE 兜底：同一批被重审两次（如残余手工重放），
        # 第二次 insert 返回 None → 计 dup，intel_new 不涨、不新推。
        recs = [dict(_rec(9, text=_HOMEWORK),
                     source_key="message_0.db/Msg_x/9", epoch=1732000000)]
        chat, _ = _chat(_JUDGE_HITS2, _EXTRACT_ITEMS2)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        deps.buf.add(GROUP, recs, now=0.0)  # 未满 3 不成批，留残余
        self.clock[0] = 60.0                 # 满 max_wait_s，flush_stale 出批
        r1 = watchd.one_tick(deps)
        self.assertEqual((r1["batches"], r1["intel_new"], r1["intel_dup"]),
                         (1, 1, 0))
        deps.buf.add(GROUP, recs, now=60.0)
        self.clock[0] = 120.0
        r2 = watchd.one_tick(deps)
        self.assertEqual((r2["batches"], r2["intel_new"], r2["intel_dup"]),
                         (1, 0, 1))
        self.assertEqual(self.store.counts()["intelligence"], 1)
        self.assertEqual(len(self.pushed), 1)  # dup 不触发推送
        # 手工入批（无 inbox rowid）的水批语义：水位不动（无坐标可依）
        self.assertEqual(self.store.cursor_get(f"audited:{GROUP}"), 0)

    def test_bad_lines_persist_in_cursor(self):
        _append_raw(self.jsonl, b"not-json\n")
        _append(self.jsonl, _rec(1))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl), chat)
        r1 = watchd.one_tick(deps)
        self.assertEqual(r1["ingested"], 1)
        self.assertEqual(self.store.cursor_get("bad_lines"), 1)
        _append_raw(self.jsonl, b"{broken\n")
        watchd.one_tick(deps)
        self.assertEqual(self.store.cursor_get("bad_lines"), 2)

    def test_realtime_disabled_still_stores(self):
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        chat, _ = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        result = watchd.one_tick(deps)
        self.assertEqual(result["intel_new"], 1)
        self.assertIsNone(result["push"])
        self.assertIsNone(result["pre"])
        self.assertEqual(len(self.pushed), 0)  # 入库零推送
        self.assertEqual(len(self.store.pending(10)), 1)

    def test_push_pre_fail_reflected(self):
        self.push_status = "pre_fail"
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        chat, _ = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        result = watchd.one_tick(deps)
        self.assertEqual(result["push"], "pre_fail")
        self.assertIs(result["pre"], False)

    def test_all_irrelevant_extract_yields_no_intel_no_push(self):
        # accept-fix 2 实况链："测试"句 judge 命中 → extract 全部 relevant:false
        # → extract_intel 返回 [] → 零 intel_new、零推送作业、audit_log 仍 ok。
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        irrelevant = [dict(_EXTRACT_ITEMS[0], relevant=False)]
        chat, calls = _chat(_JUDGE_HITS, irrelevant)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        r = watchd.one_tick(deps)
        self.assertEqual((r["hits"], r["intel_new"]), (1, 0))
        self.assertIsNone(r["push"])
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(self.store.pending(10)), 0)
        self.assertEqual(self.audit_statuses(), [("ok", 1)])

    def test_pending_resumes_without_new_intel(self):
        # 设计 §5.5："预检不过 → pending 整队保持，条件恢复后自动续流"。
        # 续流不得依赖新情报到达——静默群一旦 lock_busy/pre_refuse-crash
        # 用尽额度，行会挂 pending 到下一批情报为止（无界延迟）。每个 tick
        # 都拉 pending：空拉零成本，非空即续推。
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        chat, _ = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        self.push_status = "stopped:lock_busy"
        r1 = watchd.one_tick(deps)
        self.assertEqual(r1["push"], "stopped:lock_busy")
        self.assertEqual(len(self.store.pending(10)), 1)  # 整队保持 pending
        # tick2：jsonl 无任何新行，仅推送条件恢复 → 同一行应被续推并转 sent
        self.push_status = "ok"
        r2 = watchd.one_tick(deps)
        self.assertEqual((r2["ingested"], r2["batches"], r2["intel_new"]),
                         (0, 0, 0))
        self.assertEqual(r2["push"], "ok")
        self.assertEqual(len(self.store.pending(10)), 0)
        self.assertEqual(len(self.pushed), 2)
        self.assertEqual(self.pushed[1][1], "realtime")
        self.assertEqual([j[0] for j in self.pushed[1][0]],
                         [j[0] for j in self.pushed[0][0]])  # 同一批 id 续推
        statuses = [d["status"] for d in self.store.recent_dispatches(8)]
        self.assertEqual(statuses, ["ok", "lock_busy"])  # DESC：先 ok 后 lock_busy


# 单条命中版本（重审/重放测试用，批只含 1 条消息）
_JUDGE_HITS2 = [{"item_id": "hw", "msg_ids": [0], "reason": "要求携带工具"}]
_EXTRACT_ITEMS2 = _EXTRACT_ITEMS


class AuditFailureTest(_TickBase):
    def test_audit_failed_does_not_block_cursor(self):
        chat, calls = _chat(fail=True)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        _append(self.jsonl, _rec(1), _rec(2), _rec(3))
        r1 = watchd.one_tick(deps)
        self.assertEqual((r1["ingested"], r1["batches"], r1["hits"],
                          r1["intel_new"]), (3, 1, 0, 0))
        self.assertIsNone(r1["push"])
        # 游标推进——审计失败不得堵死摄取（Review Focus 4）
        self.assertEqual(self.store.cursor_get("jsonl"),
                         self.jsonl.stat().st_size)
        # addendum 6：外层 retries=1 → 每批 2 次尝试
        self.assertEqual(calls["judge"], 2)
        self.assertEqual(self.audit_statuses(), [("audit_failed", 0)])
        # C2：审计"尝试"即推进水位（失败也算尝试）——重启不再重审堵队列。
        self.assertEqual(self.store.cursor_get(f"audited:{GROUP}"), 3)
        # 下批继续
        _append(self.jsonl, _rec(4), _rec(5), _rec(6))
        r2 = watchd.one_tick(deps)
        self.assertEqual(r2["batches"], 1)
        self.assertEqual(calls["judge"], 4)
        self.assertEqual(len(self.audit_statuses()), 2)
        self.assertEqual(self.store.cursor_get(f"audited:{GROUP}"), 6)

    def test_audit_unexpected_exception_does_not_abort_tick(self):
        # I2 保险带：chat 抛非审计类异常（RuntimeError——SDK 内部炸）此前会
        # 裸穿 _audit_batch 打断整个 tick。现按 audit_failed 记一笔后继续：
        # 本批不产情报、pending 续推照常、audited 水位照常前进。
        def bad_chat(role, system, user, *, retries=3):
            raise RuntimeError("SDK 内部炸了")
        _append(self.jsonl, _rec(1), _rec(2), _rec(3))
        iid = self.store.insert_intelligence({
            "group": GROUP, "item_id": "hw", "title": "既有待推",
            "detail": "d", "deadline": None, "action": None,
            "source_keys": ["pre-seed-key"], "raw_json": "{}"})
        deps = self.make_deps(_cfg(self.jsonl), bad_chat)
        r = watchd.one_tick(deps)  # 不得抛
        self.assertEqual(r["batches"], 1)
        self.assertEqual(r["intel_new"], 0)
        self.assertEqual(self.audit_statuses(), [("audit_failed", 0)])
        self.assertEqual(r["push"], "ok")  # 推送阶段仍被走到（既有 pending 行）
        self.assertEqual(self.store.counts()["pending"], 0)
        self.assertEqual(self.store.cursor_get(f"audited:{GROUP}"), 3)

    def test_extract_failed_logs_extract_failed(self):
        def chat(role, system, user, *, retries=3):
            if role.model == "judge-model":
                return json.dumps({"hits": _JUDGE_HITS}, ensure_ascii=False)
            raise modelclient.ModelError("extract 挂了")
        deps = self.make_deps(_cfg(self.jsonl), chat)
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        result = watchd.one_tick(deps)
        self.assertEqual((result["hits"], result["intel_new"]), (1, 0))
        self.assertEqual(self.audit_statuses(), [("extract_failed", 1)])
        self.assertEqual(len(self.store.pending(10)), 0)


class TruncationGuardTest(_TickBase):
    def test_cursor_beyond_file_replays_with_note(self):
        # addendum 5：游标 > 实际文件大小 = jsonl 被截断/轮转，必须回 0 重放。
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        self.store.cursor_set("jsonl", self.jsonl.stat().st_size + 1024)
        chat, _ = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        deps = self.make_deps(_cfg(self.jsonl), chat)
        result = watchd.one_tick(deps)
        self.assertIn("重放", result.get("note", ""))
        self.assertEqual(result["ingested"], 3)
        self.assertEqual(result["intel_new"], 1)
        self.assertEqual(self.store.cursor_get("jsonl"),
                         self.jsonl.stat().st_size)


class DigestSchedulingTest(_TickBase):
    def test_digest_fires_once_per_day_per_time(self):
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()  # 恒空 hits：与 realtime 路径解耦
        # realtime=False：本测试只盯 digest 排程；§5.5 续拉语义由
        # test_pending_resumes_without_new_intel 单独盯防
        deps = self.make_deps(
            _cfg(self.jsonl, digest_times=("20:30", "21:00"),
                 realtime=False), chat)

        # 未到点：不推不占 fire
        self.clock[0] = due - 60
        watchd.one_tick(deps)
        self.assertEqual(self.pushed, [])

        # 到点但 day_intels 为空：不推，且不占 fire（"空那天不推不占 fire"）
        self.clock[0] = due + 60
        watchd.one_tick(deps)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.digest_fired_rows(), [])

        # 当日情报出现后：该 slot 仍应能 fire（占位未被空跑吃掉）
        self._seed_intel()
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 1)
        jobs, kind = self.pushed[0]
        self.assertEqual(kind, "digest")
        self.assertEqual(jobs[0][0], None)
        self.assertEqual(jobs[0][1].splitlines()[0], f"{day} 汇总")

        # 同日同 slot 再 tick：只推一次
        self.clock[0] = due + 300
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 1)

        # 第二个时间点：到点再推一次（每 slot 各一次/天）
        self.clock[0] = time.mktime(
            time.strptime(f"{day} 21:01", "%Y-%m-%d %H:%M"))
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 2)
        self.assertEqual(self.pushed[1][1], "digest")
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 2)

        self.assertEqual(len(self.digest_fired_rows()), 2)

    def test_two_overdue_slots_fire_one_digest_per_tick(self):
        # M8（catch-up 轰炸防护）：一 tick 至多试 fire 一个 slot——停机半天后
        # 恢复，20:30 与 21:00 同 tick 双双到期，也只发第一条；第二条留给
        # 下一 tick（逐 tick 追赶，内容同为当日汇总，不叠轰炸）。
        day = time.strftime("%Y-%m-%d", time.localtime())
        self.clock[0] = time.mktime(
            time.strptime(f"{day} 21:01", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(
            _cfg(self.jsonl, digest_times=("20:30", "21:00"),
                 realtime=False), chat)
        self._seed_intel()
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual([k for _j, k in self.pushed], ["digest"])
        self.assertEqual(len(self.digest_fired_rows()), 1)
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 2)
        self.assertEqual(len(self.digest_fired_rows()), 2)
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 2)  # 两 slot 都已占用

    def test_digest_disabled_no_fires(self):
        day = time.strftime("%Y-%m-%d", time.localtime())
        self.clock[0] = time.mktime(
            time.strptime(f"{day} 20:31", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, digest_enabled=False,
                                   realtime=False), chat)
        self._seed_intel()
        watchd.one_tick(deps)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.digest_fired_rows(), [])

    def test_digest_slot_reclaimed_on_pre_fail_and_refires(self):
        # C1：digest 推送失败 ≠ 已发。tick1@20:31 pre_fail → digest 未 ok
        # 落流水、slot 回滚（digest_fired 空）；tick2 恢复 → 恰好一次 digest
        # 推送，dispatch 流水留 [pre_fail, ok] 两行证据（pre_fail 行保留）。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self.clock[0] = due + 60
        self._seed_intel()
        self.push_status = "pre_fail"
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed], ["digest"])
        self.assertEqual([d["status"] for d in self.store.recent_dispatches(1)],
                         ["pre_fail"])  # 非 ok 流水
        self.assertEqual(self.digest_fired_rows(), [])  # slot 未被吃掉
        self.push_status = "ok"
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed], ["digest", "digest"])
        self.assertEqual(len(self.digest_fired_rows()), 1)
        watchd.one_tick(deps)  # ok 之后 slot 才真正占用
        self.assertEqual(len(self.pushed), 2)
        statuses = [d["status"] for d in self.store.recent_dispatches(2)]
        self.assertEqual(statuses, ["ok", "pre_fail"])  # DESC：证据链完整

    def test_realtime_stop_skips_digest_same_tick(self):
        # C1：同 tick realtime 推送已 pre_fail（环境不可用）→ digest 一律不
        # 尝试：try_fire 都不做（slot 从未被碰），当 tick 零 digest 流水。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl), chat)  # realtime=True
        self.clock[0] = due + 60
        self._seed_intel()  # pending 情报：realtime 会先撞门并失败
        self.push_status = "pre_fail"
        r1 = watchd.one_tick(deps)
        self.assertEqual(r1["push"], "pre_fail")
        self.assertEqual([k for _j, k in self.pushed], ["realtime"])  # 无 digest
        self.assertEqual(self.digest_fired_rows(), [])
        self.assertEqual([d for d in self.store.recent_dispatches(5)
                          if d["kind"] == "digest"], [])
        # slot 依然空闲：恢复后的下一 tick 续推 realtime 并正常补上 digest
        self.push_status = "ok"
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed],
                         ["realtime", "realtime", "digest"])
        self.assertEqual(len(self.digest_fired_rows()), 1)

    def test_digest_includes_suppressed_note_from_counts(self):
        # I4（§5.5 该句落地）：watchd 把 store.counts()["suppressed"] 传给
        # 渲染器——汇总尾部点名累计抑制条数。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self.clock[0] = due + 60
        keep = self._seed_intel()  # 当日一条（pending，realtime=False 不推）
        supp = self.store.insert_intelligence({
            "group": GROUP, "item_id": "hw", "title": "被抑制的一条",
            "detail": "d", "deadline": None, "action": None,
            "source_keys": ("message_0.db/Msg_x/88",), "raw_json": "{}"})
        self.store.mark_status(supp, "suppressed")
        watchd.one_tick(deps)
        self.assertEqual(len(self.pushed), 1)
        text = self.pushed[0][0][0][1]
        self.assertIn("另有累计 1 条情报被抑制（重试耗尽）", text)
        self.assertIn("groupwatch status", text)
        self.store.mark_status(keep, "hold")  # hold 不进抑制计数（口径=counts）

    # ---- F2：持久补发队列（digest_pending 跨 tick、跨午夜） ----

    def _pending_pairs(self):
        with closing(sqlite3.connect(str(self.home / "groupwatch.db"))) as conn:
            return conn.execute(
                "SELECT day, hhmm FROM digest_pending ORDER BY created_ts, "
                "day, hhmm").fetchall()

    def _pending_states(self):
        with closing(sqlite3.connect(str(self.home / "groupwatch.db"))) as conn:
            return conn.execute(
                "SELECT day, hhmm, state FROM digest_pending ORDER BY "
                "created_ts, day, hhmm").fetchall()

    def _digest_pushes(self):
        return [k for _j, k in self.pushed if k == "digest"]

    # ---- F2-round2：交付结果分级（歧义态 hold，零执行态续试） ----

    def test_digest_needs_check_holds_exactly_two_then_silence(self):
        # 实况（2026-11-26 受控验证）：09:12:40 与 09:13:24 两笔 digest 都是
        # rc3 NEEDS_CHECK——"非 ok 一律留队重试"把歧义发送每个 tick 重发一遍。
        # 新语义：每个槽位至多自动尝试一次；两个到期槽位各撞一次（恰 2 笔
        # digest 推送调用）后双双 held，第三个 attempt 永不发生，digest_fired
        # 保持空（歧义 ≠ 已发）。
        day = time.strftime("%Y-%m-%d", time.localtime())
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, digest_times=("20:30", "21:00"),
                                   realtime=False), chat)
        self.clock[0] = time.mktime(
            time.strptime(f"{day} 21:01", "%Y-%m-%d %H:%M"))
        self._seed_intel()
        self.push_status = "stopped:needs_check"
        for _ in range(4):
            watchd.one_tick(deps)
        self.assertEqual(len(self._digest_pushes()), 2)
        self.assertEqual(self._pending_states(),
                         [(day, "20:30", "held"), (day, "21:00", "held")])
        self.assertEqual(self.store.held_digest_count(), 2)
        self.assertEqual(self.store.pending_digest_count(), 0)
        self.assertEqual(self.digest_fired_rows(), [])

    def test_digest_pre_fail_stays_pending_and_keeps_retrying(self):
        # 对照：pre_fail / lock_busy 类结果**零执行已被证明**（预检在 wxsend
        # 之前）——留 pending、逐 tick 重试是正确行为，直到 ok 定稿。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self.clock[0] = due + 60
        self._seed_intel()
        self.push_status = "pre_fail"
        watchd.one_tick(deps)
        self.assertEqual(self._pending_states(), [(day, "20:30", "pending")])
        watchd.one_tick(deps)
        self.assertEqual(self.store.held_digest_count(), 0)
        self.assertEqual(len(self._digest_pushes()), 2)  # 第二笔照常尝试
        self.push_status = "stopped:lock_busy"
        watchd.one_tick(deps)
        self.assertEqual(self._pending_states(), [(day, "20:30", "pending")])
        self.push_status = "ok"
        watchd.one_tick(deps)
        self.assertEqual(self.digest_fired_rows(), [(day, "20:30")])
        self.assertEqual(self.store.pending_digest_count(), 0)
        self.assertEqual(len(self._digest_pushes()), 4)

    def test_held_never_reattempted_until_human_unhold(self):
        # held 行健康 tick 也不碰；人工按 README SQL 改回 pending 后才重发。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self.clock[0] = due + 60
        self._seed_intel()
        self.push_status = "stopped:needs_check"
        watchd.one_tick(deps)
        self.assertEqual(len(self._digest_pushes()), 1)
        self.push_status = "ok"
        watchd.one_tick(deps)  # 环境好了也绝不自动重发歧义笔
        self.assertEqual(len(self._digest_pushes()), 1)
        self.assertEqual(self.digest_fired_rows(), [])
        # 裁决路径②"确认没送达且环境已修"：UPDATE 回 pending → 下 tick 重发
        self.store._conn.execute(
            "UPDATE digest_pending SET state='pending' WHERE day=? AND hhmm=?",
            (day, "20:30"))
        self.store._conn.commit()
        watchd.one_tick(deps)
        self.assertEqual(len(self._digest_pushes()), 2)
        self.assertEqual(self.digest_fired_rows(), [(day, "20:30")])
        self.assertEqual(self.store.held_digest_count(), 0)

    def test_digest_persists_across_midnight(self):
        # F2 实况（2026-11-26）：20:30 被闸门挡下 → 旧流程过午夜即永久丢弃。
        # 新流程：被挡的 day1 槽位留在 digest_pending，day2 首个健康 tick 补发，
        # digest_fired 记的是 **day1** 槽位、文本仍是 day1 的汇总（不重渲染）。
        day1 = time.strftime("%Y-%m-%d", time.localtime())
        due1 = time.mktime(time.strptime(f"{day1} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self._seed_intel()
        self.clock[0] = due1 + 60
        self.push_status = "pre_fail"
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed], ["digest"])
        self.assertEqual(self._pending_pairs(), [(day1, "20:30")])
        self.assertEqual(self.digest_fired_rows(), [])  # 失败 ≠ 已发
        # 过午夜 → day2 首个健康 tick：补发的是 day1 排队的那条
        self.push_status = "ok"
        self.clock[0] = due1 + 86400 + 60
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed], ["digest", "digest"])
        self.assertEqual(self._pending_pairs(), [])
        day2 = time.strftime("%Y-%m-%d", time.localtime(self.clock[0]))
        self.assertNotEqual(day2, day1)  # 确已跨天
        self.assertEqual(self.digest_fired_rows(), [(day1, "20:30")])
        text = self.pushed[1][0][0][1]
        self.assertEqual(text.splitlines()[0], f"{day1} 汇总")  # 不重渲染

    def test_empty_day_does_not_enqueue(self):
        # 空跑不占位：当日无情报 → 零排队零推送；晚到的情报在后续 tick 入队。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self.clock[0] = due + 60
        watchd.one_tick(deps)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self._pending_pairs(), [])
        self.assertEqual(self.digest_fired_rows(), [])
        # 情报晚到 → 下个 tick 入队（此 tick 推送被挡 → 队列行可见）
        self._seed_intel()
        self.push_status = "pre_fail"
        watchd.one_tick(deps)
        self.assertEqual(self._pending_pairs(), [(day, "20:30")])

    def test_digest_finalized_only_on_ok(self):
        # "ok 才定稿"：被挡 tick → fired 不加行；成功 tick → 恰加一行；
        # 之后健康 tick 队列已空 → 不再推不再加行（幂等）。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, realtime=False), chat)
        self.clock[0] = due + 60
        self._seed_intel()
        self.push_status = "pre_fail"
        watchd.one_tick(deps)
        self.assertEqual(self.digest_fired_rows(), [])
        self.assertEqual(self.store.pending_digest_count(), 1)
        self.push_status = "ok"
        watchd.one_tick(deps)
        self.assertEqual(self.digest_fired_rows(), [(day, "20:30")])
        self.assertEqual(self.store.pending_digest_count(), 0)
        watchd.one_tick(deps)  # 已定稿 → 无在队项可发
        self.assertEqual([k for _j, k in self.pushed], ["digest", "digest"])
        self.assertEqual(self.digest_fired_rows(), [(day, "20:30")])

    def test_realtime_down_skips_digest_delivery(self):
        # realtime_down 闸门保留：它只延迟**推送**（GUI），入队（纯 DB）照常。
        day = time.strftime("%Y-%m-%d", time.localtime())
        due = time.mktime(time.strptime(f"{day} 20:30", "%Y-%m-%d %H:%M"))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl), chat)  # realtime=True
        self.clock[0] = due + 60
        self._seed_intel()
        self.push_status = "pre_fail"
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed], ["realtime"])
        self.assertEqual(self._pending_pairs(), [(day, "20:30")])  # 入队已发生
        watchd.one_tick(deps)
        # 不重复入队；仍无 digest 推送尝试
        self.assertEqual(self._pending_pairs(), [(day, "20:30")])
        self.assertEqual([k for _j, k in self.pushed],
                         ["realtime", "realtime"])
        self.push_status = "ok"
        watchd.one_tick(deps)
        self.assertEqual([k for _j, k in self.pushed],
                         ["realtime", "realtime", "realtime", "digest"])
        self.assertEqual(self.digest_fired_rows(), [(day, "20:30")])

    def _seed_intel(self):
        # 直插一条当日情报（ts 用真 time.time，与 day_intels 的 localtime 同天）
        return self.store.insert_intelligence({
            "group": GROUP, "item_id": "hw", "title": "明天带剪刀",
            "detail": "手工课要剪刀", "deadline": "明天上午", "action": None,
            "source_keys": ("message_0.db/Msg_x/77",),
            "raw_json": "{}"})


class LockTest(unittest.TestCase):
    def test_lock_second_instance_exits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        fd = watchd.acquire_lock(home)
        self.addCleanup(os.close, fd)
        self.assertTrue((home / "groupwatchd.lock").exists())
        with self.assertRaises(SystemExit) as ctx:
            watchd.acquire_lock(home)
        self.assertEqual(str(ctx.exception), "另一 groupwatchd 在跑")


class RunForeverTest(_TickBase):
    def test_ticks_and_sleep_interval(self):
        _append(self.jsonl, _rec(1))
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl, poll=7), chat)
        slept = []
        rv = watchd.run_forever(deps, sleep=slept.append, ticks=3)
        self.assertEqual(rv, 0)
        self.assertEqual(slept, [7, 7])  # tick 间隔 cfg.ingest.poll_interval_s
        self.assertEqual(self.store.cursor_get("jsonl"),
                         self.jsonl.stat().st_size)

    def test_tick_exception_printed_and_loop_continues(self):
        # jsonl 指向目录 → tail 抛异常 → 中文一行打印，下一轮照常（单 tick 崩不杀守护）
        cfg = _cfg(self.jsonl)
        broken = dataclasses.replace(
            cfg, ingest=dataclasses.replace(cfg.ingest, jsonl=self.home))
        deps = self.make_deps(broken, _chat()[0])  # ingest.jsonl = 目录本身
        err = StringIO()
        slept = []
        with redirect_stderr(err):
            rv = watchd.run_forever(deps, sleep=slept.append, ticks=2)
        self.assertEqual(rv, 0)
        # 只数守护自己的中文行：跨测试泄漏对象的 ResourceWarning 属解释器
        # 噪声（本套件曾踩：GC 恰落此窗口），不是 one_tick/run_forever 的输出。
        lines = [ln for ln in err.getvalue().strip().splitlines()
                 if ln.startswith("groupwatchd:")]
        self.assertEqual(len(lines), 2)
        self.assertIn("groupwatchd", lines[0])
        self.assertEqual(slept, [5])

    def test_keyboard_interrupt_returns_zero(self):
        chat, _ = _chat()
        deps = self.make_deps(_cfg(self.jsonl), chat)
        orig = watchd.one_tick

        def interrupt(deps_):
            raise KeyboardInterrupt
        watchd.one_tick = interrupt
        try:
            self.assertEqual(watchd.run_forever(deps, sleep=lambda s: None), 0)
        finally:
            watchd.one_tick = orig


class BuildDepsTest(_TickBase):
    def test_dry_run_wiring(self):
        self.seed_home()
        deps = watchd.build_deps(self.home, dry_run=True,
                                 contacts_loader=lambda: {})
        self.addCleanup(deps.store._conn.close)
        self.assertEqual(deps.group_names, {})
        self.assertEqual(deps.judge_tpl, "HOME-JUDGE\n===\n{watchitems}{group}{batch}")
        self.assertNotIn("HOME-JUDGE",
                         (Path(watchd.__file__).parent / "prompts" / "judge.md")
                         .read_text(encoding="utf-8"))  # 读的是 home 副本非 repo
        self.assertEqual(deps.chat(JUDGE_ROLE, "s", "u", retries=3),
                         '{"hits": []}')
        self.assertEqual(deps.push([(1, "t")], "realtime"), "ok")
        with closing(sqlite3.connect(str(self.home / "groupwatch.db"))) as conn:
            rows = conn.execute("SELECT kind, status FROM dispatches").fetchall()
        self.assertEqual(rows, [("realtime", "dry")])
        self.assertEqual((deps.buf.max_msgs, deps.buf.max_wait_s), (3, 60))
        # 按字段比对，不比 WatchItem 类恒等——test_config 会 reload config，
        # 两个同名类在不同 import 顺序下可能不同源。
        self.assertEqual([(it.id, it.name, it.desc) for it in deps.items],
                         [("hw", "作业", "作业")])
        self.assertEqual(deps.groups, frozenset({GROUP}))

    def test_dry_tick_end_to_end_no_push_no_network(self):
        self.seed_home()
        deps = watchd.build_deps(self.home, dry_run=True,
                                 contacts_loader=lambda: {})
        self.addCleanup(deps.store._conn.close)
        _append(self.jsonl, _rec(1))
        result = watchd.one_tick(deps)  # 假 clock 无；真 now 走 dry 恒空 hits
        self.assertEqual((result["ingested"], result["hits"],
                          result["intel_new"]), (1, 0, 0))
        self.assertEqual(self.store.counts()["dispatches"], 0)

    def test_missing_extract_role_raises_config_error(self):
        # addendum 4：daemon 路径绝不裸 KeyError
        self.seed_home(models=("judge",))
        with self.assertRaises(ConfigError) as ctx:
            watchd.build_deps(self.home, dry_run=True)
        self.assertIn("extract", str(ctx.exception))
        self.assertIn("models", str(ctx.exception))

    def test_missing_judge_role_raises_config_error(self):
        self.seed_home(models=("extract",))
        with self.assertRaises(ConfigError) as ctx:
            watchd.build_deps(self.home, dry_run=True)
        self.assertIn("judge", str(ctx.exception))

    def test_missing_prompt_template_raises_config_error_naming_path(self):
        # addendum 7：模板缺失 → ConfigError 且点名路径
        self.seed_home()
        os.unlink(self.home / "prompts" / "extract.md")
        with self.assertRaises(ConfigError) as ctx:
            watchd.build_deps(self.home, dry_run=True)
        self.assertIn("extract.md", str(ctx.exception))
        self.assertIn(str(self.home / "prompts"), str(ctx.exception))

    def test_dry_run_systemone_stub_all_zero_no_intel(self):
        # accept-fix §4：judge api=systemone 的 dry-run——noul 恒 0.0 桩，
        # answers 键按 questions 现构（qid 必对齐），整链零命中零情报。
        self.seed_home(apis={"judge": "systemone"})
        deps = watchd.build_deps(self.home, dry_run=True,
                                 contacts_loader=lambda: {})
        self.addCleanup(deps.store._conn.close)
        answers = deps.systemone(None, "任意状态",
                                 {"hw": {"type": "noul"}, "x": {}})
        self.assertEqual(answers, {"hw": {"type": "noul", "noul": 0.0},
                                   "x": {"type": "noul", "noul": 0.0}})
        _append(self.jsonl, _rec(1), _rec(2, text=_HOMEWORK))
        result = watchd.one_tick(deps)
        self.assertEqual((result["ingested"], result["hits"],
                          result["intel_new"]), (2, 0, 0))
        self.assertEqual(self.store.counts()["dispatches"], 0)

    def test_real_wiring_spies_systemone_module_attr(self):
        # accept-fix §4：真装配从 modelclient 模块属性取 systemone（与
        # push_texts/chat 同打法）；api=systemone 的 judge 逐消息调用、决策
        # 命中经 extract（仍是 chat）落库推送；judge 角色的 chat 一次不碰。
        self.seed_home(apis={"judge": "systemone"}, digest_enabled=False)
        calls = []

        def spy(role, state, questions, **kw):
            calls.append((role.model, state, sorted(questions)))
            p = 0.95 if state == _HOMEWORK else 0.01
            return {qid: {"type": "noul", "noul": p} for qid in questions}

        chat, chat_calls = _chat(None, _EXTRACT_ITEMS)
        orig = modelclient.systemone
        modelclient.systemone = spy
        try:
            deps = watchd.build_deps(self.home, chat=chat, push=self.fake_push,
                                     now=lambda: self.clock[0],
                                     contacts_loader=lambda: {})
        finally:
            modelclient.systemone = orig
        self.addCleanup(deps.store._conn.close)
        _append(self.jsonl, _rec(1), _rec(2), _rec(3, text=_HOMEWORK))
        r = watchd.one_tick(deps)
        self.assertEqual((r["batches"], r["hits"], r["intel_new"], r["push"]),
                         (1, 1, 1, "ok"))
        self.assertEqual(calls, [("judge-model", "水群闲聊", ["hw"]),
                                 ("judge-model", "水群闲聊", ["hw"]),
                                 ("judge-model", _HOMEWORK, ["hw"])])
        self.assertEqual(chat_calls["judge"], 0)
        self.assertEqual(chat_calls["extract"], 1)

    def test_dry_build_deps_systemone_field_always_set(self):
        # 契约 §4：Deps.systemone 默认 None 只兜直接构造的旧测试；凡经
        # build_deps（两种模式）必须已填——守护不留裸 None 触网/触崩路径。
        self.seed_home()
        deps_dry = watchd.build_deps(self.home, dry_run=True,
                                     contacts_loader=lambda: {})
        self.addCleanup(deps_dry.store._conn.close)
        self.assertIsNotNone(deps_dry.systemone)
        deps = watchd.build_deps(self.home, contacts_loader=lambda: {})
        self.addCleanup(deps.store._conn.close)
        self.assertIs(deps.systemone, modelclient.systemone)

    def test_real_wiring_uses_modelclient_and_push_texts(self):
        self.seed_home()
        deps = watchd.build_deps(self.home, contacts_loader=lambda: {})
        self.addCleanup(deps.store._conn.close)
        self.assertIs(deps.chat, modelclient.chat)
        seen = []
        orig = dispatch.push_texts

        def spy(cfg, store, jobs, *, kind, **kw):
            seen.append((cfg is deps.cfg, store is deps.store, jobs, kind))
            return "ok"
        dispatch.push_texts = spy
        try:
            rv = deps.push([(3, "hi")], "realtime")
        finally:
            dispatch.push_texts = orig
        self.assertEqual(rv, "ok")
        self.assertEqual(seen, [(True, True, [(3, "hi")], "realtime")])

    def test_build_deps_seeds_real_group_names(self):
        # accept-fix-names：build_deps 开机预载 contact.db 真群名；jsonl 的
        # conversation_name==裸 gid（上游同值缺陷）不得冲掉预载名。
        self.seed_home()
        (self.home / "groups.json").write_text(
            json.dumps({"subscribe": ["X@chatroom"]}), encoding="utf-8")
        hits = [{"item_id": "hw", "msg_ids": [0], "reason": "要求携带工具"}]
        chat, _ = _chat(hits, _EXTRACT_ITEMS)
        deps = watchd.build_deps(self.home, dry_run=True, chat=chat,
                                 push=self.fake_push,
                                 now=lambda: self.clock[0],
                                 contacts_loader=lambda: {"X@chatroom": "家庭"})
        self.addCleanup(deps.store._conn.close)
        self.assertEqual(deps.group_names, {"X@chatroom": "家庭"})
        _append(self.jsonl, _rec(1, conv="X@chatroom", name="X@chatroom",
                                 text=_HOMEWORK))
        r = watchd.one_tick(deps, flush_all=True)
        self.assertEqual(r["intel_new"], 1)
        text = self.pushed[0][0][0][1]
        self.assertIn("家庭", text)
        self.assertNotIn("X@chatroom", text)
        # gid 等值记录不覆写：预载真名保持胜出
        self.assertEqual(deps.group_names, {"X@chatroom": "家庭"})

    def test_contacts_loader_failure_defaults_empty(self):
        # 预载失败（无微信环境/monitor 缺失）→ group_names={}，渲染回落
        # 裸 gid，守护路径不崩。
        self.seed_home()

        def boom():
            raise RuntimeError("monitor 目录不存在")

        chat, _ = _chat(_JUDGE_HITS, _EXTRACT_ITEMS)
        with redirect_stderr(StringIO()):
            deps = watchd.build_deps(self.home, dry_run=True, chat=chat,
                                     push=self.fake_push,
                                     now=lambda: self.clock[0],
                                     contacts_loader=boom)
        self.addCleanup(deps.store._conn.close)
        self.assertEqual(deps.group_names, {})
        _append(self.jsonl, _rec(1, name=GROUP), _rec(2, name=GROUP),
                _rec(3, name=GROUP, text=_HOMEWORK))
        r = watchd.one_tick(deps, flush_all=True)
        self.assertEqual(r["intel_new"], 1)
        self.assertIn(f"【作业 · {GROUP}】", self.pushed[0][0][0][1])


if __name__ == "__main__":
    unittest.main()
