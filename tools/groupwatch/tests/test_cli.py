"""cli.py 子命令面测试（init/groups/enroll/status/test-model/once/run）。

全部走 tmp home；上游依赖一律 monkeypatch 模块属性：
`cli._list_groups_impl`、`cli._screenshot`、`dispatch.pixel_probe`、
`modelclient.chat`、`watchd.build_deps`/`acquire_lock`/`one_tick`/`run_forever`。
不触真实网络/子进程/截图/monitor。
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 前提：本地 cli.py 先于 site-packages 同名包（sys.path[0] 顺序保证）；若某入口
# 先 import 了外部 cli 包，sys.modules 按名缓存会遮蔽本地——与 groupwatch.py 同源约定。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import config
import contacts
import dispatch
import modelclient
import store
import watchd


def _run(argv, home):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(argv, home=str(home))
    return code, buf.getvalue()


def _seed_valid_config(home, judge_api=None, digest_enabled=None,
                       digest_times=None, jsonl=None):
    """写一份能过 load_config 的最小配置（真值版，非占位符）。
    judge_api="systemone" 时给 judge 角色写 api 键（accept-fix §5）；
    digest_enabled/digest_times 可选覆盖汇总排程（round-4 status 点名用），
    缺省保持旧种子行为（enabled=False, times=["12:00"]）；
    jsonl 可选覆盖监听端 jsonl 路径（round-5 健康判定用，缺省为不存在文件）。"""
    home = Path(home)
    judge = {"base_url": "https://x/v1",
             "model": "decision-model-preview",
             "api_key_file": "judge.key"}
    if judge_api:
        judge["api"] = judge_api
    cfg = {
        "models": {
            "judge": judge,
            "extract": {"base_url": "https://e/v1", "model": "qwen-max",
                        "api_key_file": "extract.key"},
        },
        "ingest": {"jsonl": jsonl or str(home / "nope.jsonl"),
                   "poll_interval_s": 5, "batch_max_msgs": 40,
                   "batch_max_wait_s": 60},
        "push": {"target_roster_key": "filehelper", "realtime": True,
                 "digest": {
                     "enabled": False if digest_enabled is None else digest_enabled,
                     "times": digest_times or ["12:00"]}},
        "limits": {"max_retries": 3, "push_max_per_tick": 10,
                   "inbox_retention_days": 14},
    }
    (home / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    (home / "judge.key").write_text("judge-secret", encoding="utf-8")
    (home / "extract.key").write_text("extract-secret", encoding="utf-8")
    (home / "groups.json").write_text(
        json.dumps({"subscribe": []}, ensure_ascii=False), encoding="utf-8")


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    # ---- init ----

    def test_init_writes_placeholders_and_chmods_0600_refuses_overwrite(self):
        code, out = _run(["init"], self.home)
        self.assertEqual(code, 0)
        # key 文件占位符 + 0600
        for name in ("judge.key", "extract.key"):
            p = self.home / name
            self.assertTrue(p.exists())
            self.assertIn("FILL_ME", p.read_text(encoding="utf-8"))
            self.assertIn("粘贴", p.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        # config.json 的 base_url 占位符（值里，不在结构上）
        cfgtext = (self.home / "config.json").read_text(encoding="utf-8")
        self.assertIn("在此填", cfgtext)
        # M7：init 默认汇总时点对齐设计 §5.5 的"一晚一条"示例
        self.assertEqual(json.loads(cfgtext)["push"]["digest"]["times"],
                         ["20:30"])
        # accept-fix 2：init 模板默认 require_enroll=true——新环境未跑
        # enroll 前绝不真发送（结构可见即旋钮可达）
        self.assertIs(json.loads(cfgtext)["push"]["require_enroll"], True)
        # groups.json 订阅表初始空
        g = json.loads((self.home / "groups.json").read_text(encoding="utf-8"))
        self.assertEqual(g, {"subscribe": []})
        # watchitems 内置一条示例
        w = json.loads((self.home / "watchitems.json").read_text(encoding="utf-8"))
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["id"], "homework")
        # 三件套提示词拷贝到 home
        for md in ("judge.md", "extract.md", "digest.md"):
            self.assertTrue((self.home / "prompts" / md).exists())
            self.assertEqual(
                stat.S_IMODE((self.home / "prompts" / md).stat().st_mode),
                0o600)
        # 再跑一次：逐个拒绝覆盖并 exit 2，且产物不被改动
        before = (self.home / "config.json").read_text(encoding="utf-8")
        code2, out2 = _run(["init"], self.home)
        self.assertEqual(code2, 2)
        self.assertIn("拒绝覆盖", out2)
        self.assertEqual(before,
                         (self.home / "config.json").read_text(encoding="utf-8"))

    def test_init_created_config_loads_and_reports_placeholder(self):
        code, out = _run(["init"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("占位", out)  # 报告需填占位符
        cfg = config.load_config(self.home)  # 结构合法 → 不抛
        self.assertIn("judge", cfg.models)
        self.assertIn("extract", cfg.models)
        self.assertEqual(cfg.models["judge"].api_key,
                         "FILL_ME_在此粘贴百炼APIKey")

    def test_init_template_carries_api_and_judge_threshold(self):
        # accept-fix §5：init 模板必须让新旋钮结构性可见（api 两角色、
        # limits.judge_threshold），且产物原样 round-trip 过 load_config。
        code, _out = _run(["init"], self.home)
        self.assertEqual(code, 0)
        data = json.loads((self.home / "config.json").read_text(
            encoding="utf-8"))
        self.assertEqual(data["models"]["judge"]["api"], "chat")
        self.assertEqual(data["models"]["extract"]["api"], "chat")
        self.assertEqual(data["limits"]["judge_threshold"], 0.5)
        cfg = config.load_config(self.home)
        self.assertEqual(cfg.models["judge"].api, "chat")
        self.assertEqual(cfg.models["extract"].api, "chat")
        self.assertEqual(cfg.limits.judge_threshold, 0.5)

    # ---- groups ----

    def test_groups_prints_subscription_markers(self):
        (self.home / "groups.json").write_text(
            json.dumps({"subscribe": ["cidA@chatroom"]}, ensure_ascii=False),
            encoding="utf-8")
        with mock.patch.object(
                cli, "_list_groups_impl",
                return_value=[("cidA@chatroom", "三年(2)班"),
                              ("cidB@chatroom", "家长闲聊")]):
            code, out = _run(["groups"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("[订阅] 三年(2)班 — cidA@chatroom", out)
        self.assertIn("[未订阅] 家长闲聊 — cidB@chatroom", out)

    def test_groups_failure_maps_to_exit2_with_hint(self):
        # 提示语真值（M 文档束）：groups 只读本地库，排障方向 = keys 可读/库
        # 存在，**不得**再让操作者去找"监听端在跑没跑"（那是无关条件）。
        with mock.patch.object(cli, "_list_groups_impl",
                               side_effect=OSError("no keys dir")):
            code, out = _run(["groups"], self.home)
        self.assertEqual(code, 2)
        self.assertIn("数据库", out)
        self.assertIn("keys", out)
        self.assertIn("不需要监听端进程在跑", out)

    def test_list_groups_impl_overlays_contact_names(self):
        # accept-fix-names：显示名优先 contact.db nick_name（contacts.
        # chatroom_names），次取 load_maps name，两者皆无回落裸 gid。
        # 假 monitor 经 sys.modules 注入——正是 _list_groups_impl 的
        # import 接缝；不触真库/子进程。
        groups = {"g1@chatroom": {"name": None, "table": "name2id"},
                  "g2@chatroom": {"name": "monitor名", "table": "contact2"},
                  "g3@chatroom": {"name": None, "table": "name2id"}}
        fake = SimpleNamespace(
            choose_db_storage=lambda a, b: Path("/tmp/no-such-dbs"),
            discover_message_dbs=lambda p: [],
            load_key_material=lambda p, d: ({}, "fake"),
            load_aux_keys=lambda aux: {},
            load_maps=lambda p, k: (groups, {}, {}),
        )
        with mock.patch.dict(sys.modules, {"monitor": fake}), \
                mock.patch.object(contacts, "chatroom_names",
                                  return_value={"g1@chatroom": "家庭"}):
            rows = cli._list_groups_impl(self.home)
        self.assertEqual(rows, [("g1@chatroom", "家庭"),
                                ("g2@chatroom", "monitor名"),
                                ("g3@chatroom", "g3@chatroom")])

    # ---- enroll ----

    def test_enroll_writes_baseline_points_file(self):
        def fake_shot(png):
            Path(png).write_bytes(b"\x89PNG\r\n\x1a\n")
        with mock.patch.object(cli, "_screenshot", side_effect=fake_shot), \
                mock.patch.object(dispatch, "pixel_probe",
                                  return_value=(11, 22, 33)):
            code, out = _run(["enroll", "--yes"], self.home)
        self.assertEqual(code, 0)
        p = self.home / "probe_points.json"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(len(data), len(dispatch.PROBE_POINTS))
        names = {pt["name"] for pt in dispatch.PROBE_POINTS}
        self.assertEqual({d["name"] for d in data}, names)
        for d in data:
            for k in ("name", "x", "y", "r", "g", "b", "tol"):
                self.assertIn(k, d)
            self.assertEqual((d["r"], d["g"], d["b"]), (11, 22, 33))
            self.assertEqual(d["tol"], 28)

    # ---- status ----

    def test_status_shape_after_seed(self):
        _seed_valid_config(self.home)
        st = store.open_store(self.home / "groupwatch.db")
        self.addCleanup(st._conn.close)
        id1 = st.insert_intelligence({
            "group": "cidA", "item_id": "homework", "title": "作业一",
            "detail": "d", "deadline": "明早", "action": "a",
            "source_keys": ["k1"], "raw_json": "{}"})
        st.insert_intelligence({
            "group": "cidA", "item_id": "homework", "title": "作业二",
            "detail": "d", "deadline": "", "action": "a",
            "source_keys": ["k2"], "raw_json": "{}"})
        st.mark_status(id1, "hold")
        st.log_dispatch("realtime", "ok", "已发")
        st.log_dispatch("realtime", "needs_check", "待人工")
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("计数", out)
        self.assertIn("游标", out)
        self.assertIn("jsonl", out)
        self.assertIn("待推", out)
        # 计数数字来自 counts()：情报 2 / 待推 1 / 搁置 1
        self.assertIn("情报 2", out)
        self.assertIn("待推 1", out)
        self.assertIn("搁置 1", out)
        # recent_dispatches 展示
        self.assertIn("needs_check", out)

    def test_status_surfaces_pending_digest_queue(self):
        # F2：存在未送达汇总必须肉眼可见——status 露 pending_digest_count，
        # 且 >0 时点名最旧待补发槽位（day/hhmm）。
        _seed_valid_config(self.home)
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("未送达汇总 0", out)
        self.assertIn("待人工裁决汇总 0", out)
        self.assertNotIn("最旧待补发", out)
        self.assertNotIn("待裁决:", out)
        st = store.open_store(self.home / "groupwatch.db")
        self.addCleanup(st._conn.close)
        st.enqueue_digest("2026-11-25", "20:30", "二十五日的汇总", 100)
        st.enqueue_digest("2026-11-25", "21:00", "二十五日二班", 120)
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("未送达汇总 2", out)
        self.assertIn("最旧待补发: 2026-11-25 20:30", out)

    def test_status_surfaces_held_digests(self):
        # F2-round2：needs_check 类歧义结局被 hold——status 必须数出 held 笔数、
        # 点名最旧待裁决槽位，并把人工指引指向 README 的两条裁决 SQL。
        _seed_valid_config(self.home)
        st = store.open_store(self.home / "groupwatch.db")
        self.addCleanup(st._conn.close)
        st.enqueue_digest("2026-11-25", "20:30", "在队一条", 100)
        st.enqueue_digest("2026-11-26", "09:12", "歧义一条", 200)
        st.enqueue_digest("2026-11-26", "09:13", "又歧义一条", 300)
        st.hold_digest("2026-11-26", "09:12")
        st.hold_digest("2026-11-26", "09:13")
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("未送达汇总 1", out)  # pending 计数不含 held
        self.assertIn("待人工裁决汇总 2", out)
        self.assertIn("待裁决: 2026-11-26 09:12", out)  # 最旧 held 点名
        self.assertIn("README", out)  # 指向裁决 SQL 文档

    def test_status_names_today_digest_slots(self):
        # F2-round4：今日时隙逐条点名——已发送/排队待发/待人工裁决三种终态
        # 可分辨（优先级 fired > held > pending），"空队列"不再等于"不知道坏没坏"。
        _seed_valid_config(self.home, digest_enabled=True,
                           digest_times=["08:00", "12:00", "20:30"])
        today = time.strftime("%Y-%m-%d", time.localtime())
        st = store.open_store(self.home / "groupwatch.db")
        self.addCleanup(st._conn.close)
        st.resolve_digest(today, "08:00")  # 定稿=已发送（contract：经 resolve 种子）
        st.enqueue_digest(today, "12:00", "午间汇总", 100)
        st.enqueue_digest(today, "20:30", "晚间汇总", 200)
        st.hold_digest(today, "20:30")
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("今日汇总安排: 时隙 [08:00, 12:00, 20:30]", out)
        self.assertIn("08:00 已发送", out)
        self.assertIn("12:00 排队待发", out)
        self.assertIn("20:30 待人工裁决", out)

    def test_status_today_slot_will_fire_names_intel_count(self):
        # 无队列无定稿、当日有情报 → 预告"到点将发"并点名条数。
        _seed_valid_config(self.home, digest_enabled=True,
                           digest_times=["20:30"])
        st = store.open_store(self.home / "groupwatch.db")
        self.addCleanup(st._conn.close)
        for k in ("k1", "k2"):
            st.insert_intelligence({
                "group": "cidA", "item_id": "homework", "title": "作业",
                "detail": "d", "deadline": "", "action": "a",
                "source_keys": [k], "raw_json": "{}"})
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("20:30 到点将发(当日情报 2 条)", out)
        self.assertNotIn("当日暂无情报", out)

    def test_status_today_slot_empty_db_says_no_intel(self):
        # 空库（一切皆零）必须显式说"当日暂无情报,到点不会发"——
        # 这正是"没什么可总结"与"坏了"的分界句。
        _seed_valid_config(self.home, digest_enabled=True,
                           digest_times=["20:30"])
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("今日汇总安排: 时隙 [20:30]", out)
        self.assertIn("20:30 当日暂无情报,到点不会发", out)
        self.assertNotIn("到点将发", out)

    def test_status_digest_schedule_block_absent_when_disabled(self):
        # digest_enabled=False：没有"安排"可言，整块不印（印了反而撒谎）。
        _seed_valid_config(self.home)
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertNotIn("今日汇总安排", out)

    def test_status_reports_config_error_exit0(self):
        # 无 config.json → load_config 抛 ConfigError，status 只报不崩，退出码 0
        code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("config.json", out)

    # ---- status 健康块：监听端按服务 STATE 判定，不按 jsonl 缺席（round-5）----

    def _fake_svc(self, stdout="", returncode=0, raise_exc=False):
        """注入 cli.svc_probe 的假 subprocess.run；记录调用命令，绝不触真 systemctl。"""
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if raise_exc:
                raise FileNotFoundError("systemctl not found")
            return SimpleNamespace(returncode=returncode, stdout=stdout)
        return run, calls

    def _jsonl_with_age(self, age_s):
        """tmp home 里造一个 mtime 距今 age_s 秒的 jsonl，返回路径。"""
        p = self.home / "messages.jsonl"
        p.write_text("{}", encoding="utf-8")
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def test_status_health_active_stale_says_quiet_not_broken(self):
        # active + mtime>300s：静默期是"没消息"，不是"停跑"——不得出现 ⚠/疑似停跑
        p = self._jsonl_with_age(400)
        _seed_valid_config(self.home, jsonl=str(p))
        run, calls = self._fake_svc(stdout="active\n", returncode=0)
        with mock.patch.object(cli, "svc_probe", run):
            code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("监听端: 服务在跑，近期无新消息", out)
        self.assertNotIn("疑似停跑", out)
        self.assertNotIn("⚠", out)
        self.assertIn("wechat-monitor", " ".join(calls[0]))

    def test_status_health_active_fresh(self):
        # active + 新鲜：一句话"服务在跑"
        p = self._jsonl_with_age(10)
        _seed_valid_config(self.home, jsonl=str(p))
        run, calls = self._fake_svc(stdout="active\n", returncode=0)
        with mock.patch.object(cli, "svc_probe", run):
            code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("监听端: 服务在跑", out)
        self.assertNotIn("近期无新消息", out)
        self.assertNotIn("疑似停跑", out)

    def test_status_health_not_active_fresh_mentions_manual(self):
        # 非 active（is-active 退非零、stdout "inactive"）+ jsonl 尚新 → 手动跑的提示
        p = self._jsonl_with_age(10)
        _seed_valid_config(self.home, jsonl=str(p))
        run, calls = self._fake_svc(stdout="inactive\n", returncode=3)
        with mock.patch.object(cli, "svc_probe", run):
            code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("监听端: 未在 systemd 下运行（jsonl 尚新，手动跑的请忽略）", out)
        self.assertNotIn("疑似停跑", out)

    def test_status_health_not_active_stale_warns_with_start_hint(self):
        # 非 active + stale：唯一真报警场景——点名疑似停跑并给启动命令
        p = self._jsonl_with_age(400)
        _seed_valid_config(self.home, jsonl=str(p))
        run, calls = self._fake_svc(stdout="failed\n", returncode=1)
        with mock.patch.object(cli, "svc_probe", run):
            code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("监听端: 疑似停跑 ⚠ 启动: systemctl --user start "
                      "wechat-monitor（或手动拉起 monitor.py）", out)

    def test_status_health_probe_exception_counts_as_not_active(self):
        # 接缝抛异常（systemctl 不存在等）→ 视为非 active（contract 规则 1）
        p = self._jsonl_with_age(10)
        _seed_valid_config(self.home, jsonl=str(p))
        run, calls = self._fake_svc(raise_exc=True)
        with mock.patch.object(cli, "svc_probe", run):
            code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("监听端: 未在 systemd 下运行（jsonl 尚新，手动跑的请忽略）", out)

    def test_status_health_no_jsonl_never_probes_service(self):
        # jsonl 不存在 → 旧"尚不存在"一行，且绝不调用 svc_probe（测试环境无 systemd，
        # 任何真实 systemctl 调用都会从这里漏出去）。
        _seed_valid_config(self.home)
        run, calls = self._fake_svc(stdout="active\n", returncode=0)
        with mock.patch.object(cli, "svc_probe", run):
            code, out = _run(["status"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("尚不存在", out)
        self.assertEqual(calls, [])

    # ---- test-model ----

    def test_test_model_ok_and_fail_paths(self):
        _seed_valid_config(self.home)
        with mock.patch.object(modelclient, "chat",
                               return_value="OK 一切正常") as m:
            code, out = _run(["test-model", "judge"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("OK 一切正常", out)
        # 探针文案按合同：system/user 固定
        args = m.call_args.args
        self.assertEqual(args[1], "你是链路探针。")
        self.assertEqual(args[2], "回复 OK")
        # 失败路径
        with mock.patch.object(modelclient, "chat",
                               side_effect=modelclient.ModelError("挂了")):
            code2, out2 = _run(["test-model", "extract"], self.home)
        self.assertEqual(code2, 2)
        self.assertIn("失败", out2)

    def test_test_model_chat_api_never_touches_systemone(self):
        # 路由回归：api 缺省 chat 的角色仍走原探针，决策协议注入位不得被碰。
        _seed_valid_config(self.home)
        with mock.patch.object(modelclient, "chat",
                               return_value="OK") as chatm, \
                mock.patch.object(modelclient, "systemone",
                                  side_effect=AssertionError("不该走决策协议")):
            code, out = _run(["test-model", "judge"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        chatm.assert_called_once()

    def test_test_model_systemone_routes_probe_and_prints_answers(self):
        _seed_valid_config(self.home, judge_api="systemone")
        answers = {"ping": {"type": "noul", "noul": 0.42}}
        with mock.patch.object(
                modelclient, "chat",
                side_effect=AssertionError("systemone 角色不该走 chat")), \
                mock.patch.object(modelclient, "systemone",
                                  return_value=answers) as sone:
            code, out = _run(["test-model", "judge"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("0.42", out)
        self.assertIn("ping", out)  # answers 概率原样可见（紧凑单行 JSON）
        self.assertNotIn("judge-secret", out)  # key 不进输出
        role, state, questions = sone.call_args.args
        self.assertEqual(role.api, "systemone")
        self.assertEqual(state, "测试")
        self.assertEqual(questions, {"ping": {
            "type": "noul", "instructions": "是否命中",
            "criteria": {"true": "是", "false": "否"}}})

    def test_test_model_systemone_failure_exit2_chinese(self):
        _seed_valid_config(self.home, judge_api="systemone")
        with mock.patch.object(modelclient, "systemone",
                               side_effect=modelclient.ModelError("决策模型拒了")):
            code, out = _run(["test-model", "judge"], self.home)
        self.assertEqual(code, 2)
        self.assertIn("失败", out)
        self.assertIn("决策模型拒了", out)

    # ---- once / run ----

    def test_once_prints_result_json_line(self):
        _seed_valid_config(self.home)
        fake_deps = object()
        with mock.patch.object(watchd, "build_deps",
                               return_value=fake_deps) as bd, \
                mock.patch.object(watchd, "acquire_lock", return_value=99), \
                mock.patch.object(watchd, "one_tick",
                                  return_value={"ingested": 3, "push": "ok"}) as ot:
            code, out = _run(["once", "--dry-run"], self.home)
        self.assertEqual(code, 0)
        self.assertTrue(bd.call_args.kwargs.get("dry_run"))
        # C2：once 必须整批清空（不足一批也判掉），run_forever 不得沿用
        self.assertTrue(ot.call_args.kwargs.get("flush_all"))
        last = out.strip().splitlines()[-1]
        self.assertEqual(json.loads(last)["ingested"], 3)

    def test_run_calls_run_forever(self):
        _seed_valid_config(self.home)
        with mock.patch.object(watchd, "build_deps", return_value=object()), \
                mock.patch.object(watchd, "acquire_lock", return_value=99), \
                mock.patch.object(watchd, "run_forever", return_value=0) as rf:
            code, out = _run(["run", "--dry-run"], self.home)
        self.assertEqual(code, 0)
        self.assertEqual(rf.call_count, 1)

    def test_run_lock_conflict_exit2(self):
        _seed_valid_config(self.home)
        with mock.patch.object(watchd, "build_deps", return_value=object()), \
                mock.patch.object(watchd, "acquire_lock",
                                  side_effect=SystemExit("另一 groupwatchd 在跑")):
            code, out = _run(["run"], self.home)
        self.assertEqual(code, 2)
        self.assertIn("groupwatchd 在跑", out)


if __name__ == "__main__":
    unittest.main()
