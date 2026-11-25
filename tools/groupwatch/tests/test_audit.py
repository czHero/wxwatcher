"""audit.py 两阶段审计（judge → extract）测试：chat 全部注入假函数，不碰网络。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store
from config import ModelRole, WatchItem
from ingest import Batch
from modelclient import ModelError
import audit
from audit import (AuditError, AuditFailed, Hit, Intel, judge_batch,
                   extract_intel, render_judge_prompt, render_extract_prompt,
                   retryable_audit, split_prompt, source_hash)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

ROLE = ModelRole("https://dashscope.example/v1", "qwen-max", "sk-fake")
# 决策角色：与 ROLE 仅 api 不同——路由测试盯的就是这一个开关。
ROLE_S = ModelRole("https://dashscope.example/v1", "decision-model-preview",
                   "sk-fake", api="systemone")

ITEMS = (WatchItem("price", "价格变动", "关注报价调整"),
         WatchItem("ddl", "截止日期", "关注交付 deadline"))


def _rec(i):
    return {
        "db": "msg_001.db", "table": "message_1", "local_id": i,
        "ts": f"2026-11-24T10:0{i}:00", "epoch": 1763950000 + i,
        "group": "G1", "conversation_id": "G1", "kind": "group",
        "sender_name": f"u{i}", "text": f"原始一句话{i} 内容",
        "source_key": f"msg_001.db/message_1/{i}",
    }


BATCH = Batch("G1", [_rec(0), _rec(1), _rec(2)])


def fake_chat(reply, **capture):
    calls = []

    def _chat(role, system, user, **kw):
        calls.append((role, system, user, kw))
        if isinstance(reply, Exception):
            raise reply
        return reply
    _chat.calls = calls
    return _chat


def fake_sone(reply):
    """假 /systemone：按序记录 (role, state, questions, kw)；reply 可为
    dict（固定 answers）、callable(state, questions) 或异常。"""
    calls = []

    def _sone(role, state, questions, **kw):
        calls.append((role, state, questions, kw))
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(state, questions)
        return reply
    _sone.calls = calls
    return _sone


def sone_answers(price=None, ddl=None):
    out = {}
    if price is not None:
        out["price"] = {"type": "noul", "noul": price}
    if ddl is not None:
        out["ddl"] = {"type": "noul", "noul": ddl}
    return out


def judge_reply(hits):
    return json.dumps({"hits": hits}, ensure_ascii=False)


def extract_reply(items):
    return json.dumps({"items": items}, ensure_ascii=False)


class SplitAndRenderTests(unittest.TestCase):
    def test_split_prompt_splits_first_separator_only(self):
        system, user = split_prompt("A\n===\nB\n===\nC")
        self.assertEqual(system, "A")
        self.assertEqual(user, "B\n===\nC")

    def test_split_prompt_missing_separator_raises_audit_error(self):
        with self.assertRaises(AuditError) as ctx:
            split_prompt("没有分隔符")
        self.assertIn("===", str(ctx.exception))

    def test_split_prompt_error_message_is_single_line_literal(self):
        # 错误消息里的分隔符要渲染成字面 `\n===\n`（单反斜杠 n，单行）——守护
        # 日志里多行异常会碎成无法 grep 的一地；且必须与实际分隔符常量同源。
        with self.assertRaises(AuditError) as ctx:
            split_prompt("没有分隔符", "judge")
        msg = str(ctx.exception)
        self.assertNotIn("\n", msg)
        self.assertIn("\\n===\\n", msg)
        self.assertIn("judge", msg)

    def test_render_judge_prompt_substitutes_placeholders(self):
        tpl = "系统\n===\n项:{watchitems}\n群:{group}\n批:{batch}"
        out = render_judge_prompt(tpl, ITEMS, BATCH)
        # 占位符应全部被替换掉：
        for ph in ("{watchitems}", "{group}", "{batch}"):
            self.assertNotIn(ph, out)
        self.assertIn("price", out)
        self.assertIn("G1", out)
        self.assertIn("[#0][2026-11-24T10:00:00] u0: 原始一句话0 内容", out)
        self.assertIn("[#2]", out)

    def test_fill_single_pass_message_placeholders_inert(self):
        # M1：占位符替换必须单遍——消息正文里的字面 "{hits}"/"{context}" 是
        # 用户聊天内容（如转发的教程文案），不得被后续键的替换二次展开。
        msg = dict(_rec(0), text="教程：注意 {context} 与 {hits} 是字面占位符")
        batch = Batch("G1", [msg, _rec(1), _rec(2)])
        tpl = "sys\n===\n{watchitems}\n{group}\n{batch}\n{hits}\n{context}"
        hits = [Hit("price", (0,), "报价")]
        context = [{"ts": "t", "sender_name": "u9", "text": "前文真身"}]
        out = render_extract_prompt(tpl, ITEMS, batch, hits, context)
        self.assertIn("注意 {context} 与 {hits} 是字面占位符", out)  # 原样存活
        self.assertIn("前文真身", out)  # 真占位符照常填充
        self.assertIn("- item_id=price", out)
        self.assertNotIn("{watchitems}", out)

    def test_fill_single_pass_survives_in_judge_prompt_too(self):
        msg = dict(_rec(1), text="别抄 {batch} 这个示例")
        batch = Batch("G1", [msg])
        tpl = "系统\n===\n项:{watchitems}\n群:{group}\n批:{batch}"
        out = render_judge_prompt(tpl, ITEMS, batch)
        self.assertIn("别抄 {batch} 这个示例", out)
        self.assertIn("G1", out)

    def test_render_extract_prompt_adds_hits_and_context(self):
        tpl = "系统\n===\n{watchitems}\n{group}\n{batch}\n{hits}\n{context}"
        hits = [Hit("price", (0, 2), "报价")]
        ctx_rows = [{"ts": "2026-11-24T09:00:00", "sender_name": "u9",
                     "text": "前文一句话"}]
        out = render_extract_prompt(tpl, ITEMS, BATCH, hits, ctx_rows)
        for ph in ("{watchitems}", "{group}", "{batch}", "{hits}", "{context}"):
            self.assertNotIn(ph, out)
        self.assertIn("price", out)
        self.assertIn("报价", out)
        self.assertIn("前文一句话", out)


class JudgeTests(unittest.TestCase):
    def test_judge_parses_valid_hits_and_sorts_dedupes_msg_ids(self):
        reply = judge_reply([
            {"item_id": "price", "msg_ids": [2, 0, 2], "reason": "报价上调"},
            {"item_id": "ddl", "msg_ids": [1], "reason": "周五截止"},
        ])
        hits = judge_batch(ROLE, "sys\n===\nu {watchitems}{group}{batch}",
                           ITEMS, BATCH, chat=fake_chat(reply))
        self.assertEqual(hits, [
            Hit("price", (0, 2), "报价上调"),
            Hit("ddl", (1,), "周五截止"),
        ])

    def test_judge_rejects_unknown_item_id(self):
        reply = judge_reply([{"item_id": "nope", "msg_ids": [0], "reason": "x"}])
        with self.assertRaises(AuditError):
            judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                        ITEMS, BATCH, chat=fake_chat(reply))

    def test_judge_rejects_out_of_range_msg_id(self):
        reply = judge_reply([{"item_id": "price", "msg_ids": [3], "reason": "x"}])
        with self.assertRaises(AuditError) as ctx:
            judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                        ITEMS, BATCH, chat=fake_chat(reply))
        self.assertIn("越界", str(ctx.exception))

    def test_judge_rejects_negative_msg_id(self):
        reply = judge_reply([{"item_id": "price", "msg_ids": [-1], "reason": "x"}])
        with self.assertRaises(AuditError):
            judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                        ITEMS, BATCH, chat=fake_chat(reply))

    def test_judge_rejects_non_int_msg_id(self):
        # 约束矩阵：非整数 msg_ids（字符串/浮点/bool）→ AuditError，消息点名坏值。
        # bool 特意用 True：int(True)==1 在合法下标范围内，仅靠类型检查拦截。
        for bad in ("1", 1.0, True):
            with self.subTest(bad=bad):
                reply = judge_reply([
                    {"item_id": "price", "msg_ids": [0, bad], "reason": "x"}])
                with self.assertRaises(AuditError) as ctx:
                    judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                                ITEMS, BATCH, chat=fake_chat(reply))
                self.assertIn(repr(bad), str(ctx.exception))
                self.assertIn("msg_ids", str(ctx.exception))

    def test_judge_rejects_non_json(self):
        c = fake_chat("抱歉，我无法完成这个请求。")
        with self.assertRaises(AuditError):
            judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                        ITEMS, BATCH, chat=c)

    def test_judge_rejects_missing_hits_field(self):
        with self.assertRaises(AuditError):
            judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                        ITEMS, BATCH, chat=fake_chat('{"result": 1}'))

    def test_judge_empty_hits_is_ok(self):
        hits = judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                           ITEMS, BATCH, chat=fake_chat('{"hits": []}'))
        self.assertEqual(hits, [])

    def test_judge_passes_retries_to_chat_and_uses_system_section(self):
        c = fake_chat('{"hits": []}')
        judge_batch(ROLE, "JUDGE系统\n===\n{watchitems}{group}{batch}",
                    ITEMS, BATCH, chat=c, retries=5)
        role, system, user, kw = c.calls[0]
        self.assertEqual(role, ROLE)
        self.assertEqual(system, "JUDGE系统")
        self.assertEqual(kw.get("retries"), 5)

    def test_judge_chat_role_never_calls_systemone(self):
        # 路由回归（accept-fix）：api=chat 的角色即使注入了假决策协议也一步
        # 都不许碰——旧行为零改动。
        hits = judge_batch(ROLE, "s\n===\n{watchitems}{group}{batch}",
                           ITEMS, BATCH, chat=fake_chat('{"hits": []}'),
                           systemone=lambda *a, **k: self.fail(
                               "chat 角色不得触决策协议"))
        self.assertEqual(hits, [])


class SystemOneJudgeTests(unittest.TestCase):
    """judge 的 systemone 路由：逐消息 noul，阈值含边界；tpl 不参与。"""

    TPL = "不该被用的系统\n===\n{watchitems}{group}{batch}"

    def test_routes_to_systemone_never_chat(self):
        s = fake_sone(sone_answers(price=0.0, ddl=0.0))
        hits = judge_batch(ROLE_S, self.TPL, ITEMS, BATCH,
                           chat=lambda *a, **k: self.fail(
                               "systemone 角色不得触 chat"),
                           systemone=s)
        self.assertEqual(hits, [])
        self.assertEqual(len(s.calls), len(BATCH.msgs))  # 每消息一次
        self.assertEqual([st for _r, st, _q, _k in s.calls],
                         [m["text"] for m in BATCH.msgs])
        # 契约 §3：systemone 用自身默认重试调用，不转发 judge 的 retries
        self.assertEqual([kw for *_x, kw in s.calls], [{}] * len(BATCH.msgs))

    def test_questions_table_shape_exact(self):
        # 部分相关措辞（accept-fix-precision 追加，联机标定 2026-11-25）：
        # 整体主题式问法把混排真作业压到 0.1、噪声"测试"反给 0.72——改为
        # "任何部分涉及即 true"。qid/type/criteria 结构不变，钉死字面。
        s = fake_sone(sone_answers(price=0.0, ddl=0.0))
        judge_batch(ROLE_S, self.TPL, ITEMS, BATCH, systemone=s)
        _r, _st, questions, _k = s.calls[0]
        self.assertEqual(questions, {
            "price": {"type": "noul",
                      "instructions": "这条消息里是否【包含】「价格变动」的内容"
                                      "（关注报价调整）？只要原文任何部分涉及即"
                                      "答 true，即使消息同时混杂其他话题。",
                      "criteria": {"true": "消息的某一部分涉及上述事项",
                                   "false": "整条消息从头到尾没有任何相关内容"}},
            "ddl": {"type": "noul",
                    "instructions": "这条消息里是否【包含】「截止日期」的内容"
                                    "（关注交付 deadline）？只要原文任何部分涉及"
                                    "即答 true，即使消息同时混杂其他话题。",
                    "criteria": {"true": "消息的某一部分涉及上述事项",
                                 "false": "整条消息从头到尾没有任何相关内容"}},
        })

    def test_noul_above_threshold_makes_hit_with_probability_reason(self):
        # 仅第二条消息命中 price：Hit 的 msg_ids 恰为该条批内下标 (1,)。
        def by_state(state, questions):
            p = 0.97 if state == BATCH.msgs[1]["text"] else 0.0
            return {"price": {"type": "noul", "noul": p},
                    "ddl": {"type": "noul", "noul": 0.1}}
        hits = judge_batch(ROLE_S, self.TPL, ITEMS, BATCH,
                           systemone=fake_sone(by_state))
        self.assertEqual(hits, [Hit("price", (1,), "决策概率 0.97")])

    def test_threshold_boundary_inclusive_and_custom(self):
        for th, p, want in ((0.5, 0.5, True),   # 默认阈值，等号算命中
                            (0.5, 0.49, False),
                            (0.9, 0.9, True),   # 自定义阈值同样含边界
                            (0.9, 0.899, False)):
            with self.subTest(th=th, p=p):
                s = fake_sone(sone_answers(price=p, ddl=0.0))
                hits = judge_batch(ROLE_S, self.TPL, ITEMS, BATCH,
                                   systemone=s, threshold=th)
                if want:
                    self.assertEqual(len(hits), len(BATCH.msgs))
                    self.assertEqual(hits[0].item_id, "price")
                    self.assertEqual(hits[0].reason, f"决策概率 {p:.2f}")
                else:
                    self.assertEqual(hits, [])

    def test_missing_answer_qid_raises_audit_error_naming_qid(self):
        s = fake_sone(sone_answers(price=0.9))  # ddl 整条缺失
        with self.assertRaises(AuditError) as cm:
            judge_batch(ROLE_S, self.TPL, ITEMS, BATCH, systemone=s)
        self.assertIn("ddl", str(cm.exception))

    def test_bad_noul_shapes_raise_audit_error(self):
        # answers 非对象 / 答案非对象 / noul 缺失 / noul 字符串 / noul bool
        # （True 是 int 实例，不得伪装成概率 1.0 造命中）。
        for reply, frag in (
                (["不是对象"], "answers"),
                ({"price": "0.9", "ddl": {"noul": 0.0}}, "price"),
                ({"price": {"type": "noul"}, "ddl": {"noul": 0.0}}, "price"),
                ({"price": {"noul": "0.9"}, "ddl": {"noul": 0.0}}, "price"),
                ({"price": {"noul": True}, "ddl": {"noul": 0.0}}, "price")):
            with self.subTest(reply=reply):
                with self.assertRaises(AuditError) as cm:
                    judge_batch(ROLE_S, self.TPL, ITEMS, BATCH,
                                systemone=fake_sone(reply))
                self.assertIn(frag, str(cm.exception))

    def test_over_16_items_rejected_before_any_call(self):
        items = tuple(WatchItem(f"i{n}", f"名{n}", f"述{n}")
                      for n in range(17))
        s = fake_sone({})
        with self.assertRaises(AuditError) as cm:
            judge_batch(ROLE_S, self.TPL, items, BATCH, systemone=s)
        self.assertIn("决策模型单请求问题上限 16", str(cm.exception))
        self.assertEqual(len(s.calls), 0)

    def test_model_error_propagates_for_upstream_batch_retry(self):
        # 契约 §3：单消息决策失败抛 ModelError 原样穿透——retryable_audit
        # 整批重投（幂等重判），judge_batch 不包成 AuditError。
        with self.assertRaises(ModelError):
            judge_batch(ROLE_S, self.TPL, ITEMS, BATCH,
                        systemone=fake_sone(ModelError("决策模型 429 耗尽")))


class ExtractTests(unittest.TestCase):
    TPL = "sys\n===\n{watchitems}\n{group}\n{batch}\n{hits}\n{context}"

    def _extract(self, reply, hits=None, context=()):
        hits = hits or [Hit("price", (0, 2), "报价")]
        return extract_intel(ROLE, self.TPL, ITEMS, BATCH, hits, context,
                             chat=fake_chat(reply))

    def test_extract_maps_source_keys_from_hit_msg_ids(self):
        intel = self._extract(extract_reply([
            {"item_id": "price", "relevant": True, "title": "报价上调", "detail": "A 报价涨",
             "deadline": "下周一", "action": None,
             "excerpts": ["原始一句话0 内容"]},
        ]))
        self.assertEqual(len(intel), 1)
        got = intel[0]
        self.assertEqual(got.item_id, "price")
        self.assertEqual(got.title, "报价上调")
        self.assertEqual(got.deadline, "下周一")
        self.assertIsNone(got.action)
        self.assertEqual(got.excerpts, ("原始一句话0 内容",))
        # msg_ids (0,2) → 按序映射 batch 记录的 source_key：
        self.assertEqual(got.source_keys,
                         ("msg_001.db/message_1/0", "msg_001.db/message_1/2"))

    def test_extract_maps_source_keys_from_context_excerpt(self):
        hits = [Hit("ddl", (1,), "截止")]
        context = [{"ts": "t", "sender_name": "u9", "text": "前文：周五交付"}]
        intel = self._extract(extract_reply([
            {"item_id": "ddl", "relevant": True, "title": "交付时间", "detail": "周五交付",
             "deadline": "周五", "action": "跟进", "excerpts": ["周五交付"]},
        ]), hits=hits, context=context)
        self.assertEqual(intel[0].source_keys, ("msg_001.db/message_1/1",))

    def test_extract_rejects_empty_title_or_detail(self):
        base = {"item_id": "price", "relevant": True, "title": "T", "detail": "D",
                "deadline": None, "action": None, "excerpts": []}
        for bad in ({"title": ""}, {"detail": ""}, {"title": "   "},
                    {"detail": None}):
            doc = dict(base, **bad)
            with self.assertRaises(AuditError, msg=str(bad)):
                self._extract(extract_reply([doc]))

    def test_extract_rejects_item_id_without_hit(self):
        with self.assertRaises(AuditError):
            self._extract(extract_reply([
                {"item_id": "ddl", "relevant": True, "title": "T", "detail": "D",
                 "deadline": None, "action": None, "excerpts": []}]))

    def test_extract_rejects_non_verbatim_excerpt(self):
        with self.assertRaises(AuditError):
            self._extract(extract_reply([
                {"item_id": "price", "relevant": True, "title": "T", "detail": "D",
                 "deadline": None, "action": None,
                 "excerpts": ["这段是模型编造的原文里没有的话"]}]))

    def test_extract_rejects_out_of_range_hit_index(self):
        with self.assertRaises(AuditError):
            self._extract(
                extract_reply([{"item_id": "price", "relevant": True, "title": "T", "detail": "D",
                                "deadline": None, "action": None,
                                "excerpts": []}]),
                hits=[Hit("price", (99,), "坏下标")])

    def test_extract_rejects_non_json(self):
        with self.assertRaises(AuditError):
            self._extract("这不是 JSON")

    def test_extract_rejects_missing_relevant_field(self):
        # 相关性否决（accept-fix 2）：模型必须对每个命中项显式给出 relevant——
        # 键缺失即 AuditError（点名 items[idx] 与字段名），绝不静默当作 true。
        doc = {"item_id": "price", "title": "T", "detail": "D",
               "deadline": None, "action": None, "excerpts": []}
        with self.assertRaises(AuditError) as cm:
            self._extract(extract_reply([doc]))
        self.assertIn("items[0]", str(cm.exception))
        self.assertIn("relevant", str(cm.exception))

    def test_extract_rejects_non_bool_relevant(self):
        # 非严格 bool（字符串 "true" / 数字 / null / 空串）一律 AuditError——
        # "true" 字符串正是"模型把布尔写成文本"的实况形态。
        for bad in ("true", 1, 0, None, ""):
            with self.subTest(bad=bad):
                doc = {"item_id": "price", "relevant": bad, "title": "T",
                       "detail": "D", "deadline": None, "action": None,
                       "excerpts": []}
                with self.assertRaises(AuditError) as cm:
                    self._extract(extract_reply([doc]))
                self.assertIn("relevant", str(cm.exception))

    def test_extract_relevant_false_drops_that_item_only(self):
        # 同一响应里 false 项整条丢弃（不产 Intel、不入库、不推送），
        # true 项照常——误伤拦截不误伤同批其他真情报。
        hits = [Hit("price", (0,), "报价"), Hit("ddl", (1,), "截止")]
        intel = self._extract(extract_reply([
            {"item_id": "price", "relevant": False, "title": "无作业相关信息",
             "detail": "未提及任何作业", "deadline": None, "action": None,
             "excerpts": []},
            {"item_id": "ddl", "relevant": True, "title": "周五交付",
             "detail": "明说周五交付", "deadline": "周五", "action": None,
             "excerpts": ["原始一句话1 内容"]},
        ]), hits=hits)
        self.assertEqual([i.item_id for i in intel], ["ddl"])

    def test_extract_all_irrelevant_returns_empty(self):
        # "测试"误伤实况：judge 命中但抽取判全部无关 → [] → 上游零情报。
        intel = self._extract(extract_reply([
            {"item_id": "price", "relevant": False, "title": "无作业相关信息",
             "detail": "未提及任何作业", "deadline": None, "action": None,
             "excerpts": []}]))
        self.assertEqual(intel, [])


class RetryableAuditTests(unittest.TestCase):
    def test_retryable_audit_gives_up_after_n(self):
        calls = []
        sleeps = []

        def always_fail():
            calls.append(1)
            raise AuditError("坏输出")

        with self.assertRaises(AuditFailed):
            retryable_audit(always_fail, retries=3, sleep=sleeps.append)
        # 约定与 modelclient.chat 一致：retries=3 → 共调用 4 次、睡 3 次。
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(sleeps), 3)

    def test_retryable_audit_succeeds_without_sleeping(self):
        self.assertEqual(retryable_audit(lambda: "ok", retries=3,
                                         sleep=lambda s: self.fail()), "ok")

    def test_retryable_audit_wraps_model_error_and_value_error(self):
        sleeps = []
        for exc in (ModelError("模型挂了"), ValueError("JSON 坏了"),
                    AuditError("输出坏了")):
            state = {"n": 0}

            def flaky(exc=exc, state=state):
                state["n"] += 1
                if state["n"] < 2:
                    raise exc
                return "ok"

            self.assertEqual(retryable_audit(flaky, retries=1,
                                             sleep=sleeps.append), "ok")

    def test_retryable_audit_does_not_swallow_other_exceptions(self):
        def boom():
            raise KeyError("与审计无关")
        with self.assertRaises(KeyError):
            retryable_audit(boom, retries=2, sleep=lambda s: None)


class SourceHashTests(unittest.TestCase):
    def test_source_hash_order_insensitive(self):
        self.assertEqual(source_hash(["b", "a", "c"]),
                         source_hash(["c", "b", "a"]))
        self.assertEqual(source_hash([]), source_hash([]))
        # 单一事实来源：必须与 store._source_hash 同函数、同结果。
        self.assertIs(source_hash, store._source_hash)
        sample = ["msg_001.db/message_1/0", "msg_001.db/message_1/2"]
        self.assertEqual(source_hash(sample), store._source_hash(sample))


class DefaultPromptTemplateTests(unittest.TestCase):
    def test_judge_template_has_separator_and_placeholders(self):
        tpl = (PROMPTS_DIR / "judge.md").read_text(encoding="utf-8")
        self.assertIn("\n===\n", tpl)
        system, user = split_prompt(tpl)
        for ph in ("{watchitems}", "{group}", "{batch}"):
            self.assertIn(ph, user)
        self.assertNotIn("{", system.replace("{watchitems}", ""))
        self.assertIn('"hits"', tpl)

    def test_extract_template_has_separator_and_placeholders(self):
        tpl = (PROMPTS_DIR / "extract.md").read_text(encoding="utf-8")
        self.assertIn("\n===\n", tpl)
        system, user = split_prompt(tpl)
        for ph in ("{watchitems}", "{group}", "{batch}", "{hits}", "{context}"):
            self.assertIn(ph, user)
        self.assertIn('"items"', tpl)


class EndToEndTemplateTests(unittest.TestCase):
    def test_judge_with_real_template_and_fake_chat(self):
        tpl = (PROMPTS_DIR / "judge.md").read_text(encoding="utf-8")
        c = fake_chat('{"hits": []}')
        self.assertEqual(judge_batch(ROLE, tpl, ITEMS, BATCH, chat=c), [])
        _, system, user, _ = c.calls[0]
        self.assertIn("消息过滤器", system)
        for ph in ("{watchitems}", "{group}", "{batch}"):
            self.assertNotIn(ph, user)
        self.assertIn("[#1][2026-11-24T10:01:00] u1:", user)
        self.assertIn("price", user)

    def test_extract_with_real_template_and_fake_chat(self):
        tpl = (PROMPTS_DIR / "extract.md").read_text(encoding="utf-8")
        c = fake_chat(extract_reply([
            {"item_id": "price", "relevant": True, "title": "报价上调", "detail": "A 报价涨到 100",
             "deadline": None, "action": None,
             "excerpts": ["原始一句话0 内容"]}]))
        hits = [Hit("price", (0,), "报价")]
        intel = extract_intel(ROLE, tpl, ITEMS, BATCH, hits, [], chat=c)
        self.assertEqual(intel[0].source_keys, ("msg_001.db/message_1/0",))
        _, system, user, _ = c.calls[0]
        self.assertIn("{", user)  # 输出示例里的字面 JSON 花括号必须原样保留
        self.assertIn('"items"', user)
        self.assertIn("price", user)
        self.assertNotIn("{hits}", user)
        self.assertNotIn("{context}", user)


if __name__ == "__main__":
    unittest.main()
