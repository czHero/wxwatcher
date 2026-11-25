"""dispatch.py 预检闸门 / wxsend 退出码状态机 / 批次策略测试。

不碰真实截图与子进程：`shot`/`run` 全部注入；按 brief Step 1 口径直接
monkeypatch `dispatch.pixel_probe` 与 `dispatch.load_probe_baseline`。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch
import store as store_mod
from config import Config, IngestCfg, Limits, ModelRole, PushCfg

# 单点 baseline：1440×900 标题带坐标，缓存色 (100,100,100)，tol=10。
BASE = [{"name": "标题带", "x": 60, "y": 40,
         "r": 100, "g": 100, "b": 100, "tol": 10}]
BASE2 = BASE + [{"name": "侧栏", "x": 60, "y": 500,
                 "r": 50, "g": 60, "b": 70, "tol": 3}]


def _cfg(home, max_retries=2, push_max=10, require_enroll=False):
    # require_enroll 测试夹具刻意取 False（legacy pin 的一站式更新点）：套内
    # 多数用例走 tmp home（无 probe_points.json），依赖旧"缺失放行"路径抵达
    # job 循环。生产默认 True 的行为由 TestRequireEnrollGate 专项钉住，加载
    # 默认值由 test_config.PushRequireEnrollTests 钉住。
    return Config(
        home=home,
        models={"extract": ModelRole("https://x.example/v1", "m", "k")},
        ingest=IngestCfg(home / "in.jsonl", 5, 10, 5),
        push=PushCfg("班级群", True, False, (), home / "fake-wxsend.py",
                     require_enroll),
        limits=Limits(max_retries, push_max, 7, 20),
        prompts_dir=home / "prompts",
    )


def _mkintels(st, n):
    return [st.insert_intelligence({
        "group": "g1", "item_id": "hw", "title": f"标题{i}",
        "detail": f"正文{i}", "deadline": "", "action": "a",
        "source_keys": [f"k{i}"], "raw_json": "{}"}) for i in range(n)]


def _proc(rc, stdout=""):
    # 假返回对象：只有 rc/stdout（stderr 缺 → 实现按 getattr ""），
    # 与控制器核对的注入接口口径一致。
    return types.SimpleNamespace(rc=rc, stdout=stdout)


def _fake_run(*results):
    """results 每项为 _proc 或异常；耗尽后一律 rc=0。调用时刻 stat
    `--file` 记录 mode——同时钉住 "wxsend 启动前文本文件已写 0600"。"""
    calls = []
    seq = list(results)

    def _run(argv, **kw):
        rec = {"argv": argv, "kw": kw}
        path = argv[argv.index("--file") + 1]
        rec["mode_at_call"] = ((os.stat(path).st_mode & 0o777)
                               if os.path.exists(path) else None)
        calls.append(rec)
        r = seq.pop(0) if seq else _proc(0)
        if isinstance(r, BaseException):
            raise r
        return r
    _run.calls = calls
    return _run


def _noop_shot(png):
    pass


def _writing_shot(png):
    Path(png).write_bytes(b"\x89PNG fake")


def _row_of(st, intel_id):
    for status in ("pending", "sent", "hold", "suppressed"):
        for r in st._conn.execute(
                "SELECT * FROM intelligence WHERE id=? AND pushed=?",
                (intel_id, status)):
            return dict(r)
    return None


class DispatchCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.cfg = _cfg(self.home)
        self.st = store_mod.open_store(self.home / "groupwatch.db")
        self.addCleanup(self.st._conn.close)


class TestClassify(DispatchCase):
    def test_classify_wxsend_six_way(self):
        self.assertEqual(dispatch.classify_wxsend(0, "SENT: ok"), "sent")
        # rc2 + aborted-at-step（GUI 已跑过若干步，wxsend.py:258）→ gui_partial
        self.assertEqual(dispatch.classify_wxsend(
            2, "REFUSE: aborted at step 2 (click-input): [...] rc=1"),
            "gui_partial")
        # 普通 REFUSE = 前置拒绝，一条命令都没执行
        self.assertEqual(dispatch.classify_wxsend(
            2, "REFUSE: 未找到目标群 xxx"), "pre_refuse")
        self.assertEqual(dispatch.classify_wxsend(
            3, "NEEDS_CHECK: 状态未知"), "needs_check")
        self.assertEqual(dispatch.classify_wxsend(4, "LOCK_BUSY"), "lock_busy")
        self.assertEqual(dispatch.classify_wxsend(1, "Traceback ..."), "crash")
        self.assertEqual(dispatch.classify_wxsend(127, ""), "crash")


class TestForegroundOk(DispatchCase):
    def test_foreground_ok_baseline_missing_passthrough_with_warning(self):
        # tmp home 里真的没有 probe_points.json → 文件缺失口径：None → 放行带警告
        self.assertIsNone(dispatch.load_probe_baseline(self.home))
        ok, msg = dispatch.foreground_ok(self.home, shot=_noop_shot)
        self.assertTrue(ok)
        self.assertIn("预检未配置", msg)
        self.assertIn("enroll", msg)

    def test_foreground_ok_tolerance_math(self):
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE):
            # 精确命中
            with mock.patch.object(dispatch, "pixel_probe",
                                   return_value=(100, 100, 100)):
                self.assertEqual(
                    dispatch.foreground_ok(self.home, shot=_writing_shot),
                    (True, "ok"))
            # 边界：逐通道 abs 差恰等于 tol → 仍通过（abs≤tol 含等号）
            with mock.patch.object(dispatch, "pixel_probe",
                                   return_value=(90, 110, 100)):
                self.assertEqual(
                    dispatch.foreground_ok(self.home, shot=_writing_shot),
                    (True, "ok"))
            # 超一格：任一通道 tol+1 → 不匹配，报点名
            with mock.patch.object(dispatch, "pixel_probe",
                                   return_value=(111, 100, 100)):
                ok, msg = dispatch.foreground_ok(self.home,
                                                 shot=_writing_shot)
                self.assertFalse(ok)
                self.assertEqual(msg, "点位 标题带 不匹配")
            # 多点：第一点过、第二点不过 → 报第二点的名
            def probe(png, x, y):
                return (100, 100, 100) if (x, y) == (60, 40) else (0, 0, 0)
            with mock.patch.object(dispatch, "load_probe_baseline",
                                   return_value=BASE2), \
                 mock.patch.object(dispatch, "pixel_probe", side_effect=probe):
                ok, msg = dispatch.foreground_ok(self.home,
                                                 shot=_writing_shot)
                self.assertFalse(ok)
                self.assertEqual(msg, "点位 侧栏 不匹配")

    def test_foreground_ok_shot_failure_or_missing_png(self):
        def boom(png):
            raise subprocess.TimeoutExpired("gnome-screenshot", 15)
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE):
            self.assertEqual(dispatch.foreground_ok(self.home, shot=boom),
                             (False, "截图失败"))
            # shot 声称成功但没落盘 → 同样按截图失败，绝不放行
            self.assertEqual(
                dispatch.foreground_ok(self.home, shot=_noop_shot),
                (False, "截图失败"))


class TestPushTexts(DispatchCase):
    def test_push_texts_zero_wxsend_calls_on_precheck_fail(self):
        # Review Focus 2：预检不过 → 零 wxsend、pending 原样、有 pre_fail 行
        ids = _mkintels(self.st, 2)
        run = _fake_run()
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE), \
             mock.patch.object(dispatch, "pixel_probe",
                               return_value=(0, 0, 0)):
            res = dispatch.push_texts(
                self.cfg, self.st, [(ids[0], "A"), (ids[1], "B")],
                kind="realtime", shot=_writing_shot, run=run)
        self.assertEqual(res, "pre_fail")
        self.assertEqual(len(run.calls), 0)
        rows = {r["id"]: r for r in self.st.pending(10)}
        self.assertEqual(sorted(rows), sorted(ids))
        self.assertEqual([rows[i]["retries"] for i in ids], [0, 0])
        ds = self.st.recent_dispatches(5)
        self.assertTrue([d for d in ds
                         if d["kind"] == "realtime" and d["status"] == "pre_fail"])

    def test_pre_fail_dispatch_logged_once_on_state_change(self):
        # M5：pre_fail 是状态不是事件——连续被闸门挡下只记一笔（否则遮挡/锁屏
        # 时段每 tick 一行灌爆 journal）；状态真变化（成功推送）后，下一次
        # pre_fail 重新记一行——证明是"仅记变化"而非"永久抑制"。
        ids = _mkintels(self.st, 1)
        jobs = [(ids[0], "A")]
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE), \
             mock.patch.object(dispatch, "pixel_probe",
                               return_value=(0, 0, 0)), \
             redirect_stderr(StringIO()):
            dispatch.push_texts(self.cfg, self.st, jobs, kind="realtime",
                                shot=_writing_shot, run=_fake_run())
            dispatch.push_texts(self.cfg, self.st, jobs, kind="realtime",
                                shot=_writing_shot, run=_fake_run())
        first_pre = [d for d in self.st.recent_dispatches(10)
                     if d["status"] == "pre_fail"]
        self.assertEqual(len(first_pre), 1)  # 两次被挡、只记一笔（M5）
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE), \
             mock.patch.object(dispatch, "pixel_probe",
                               return_value=(100, 100, 100)), \
             redirect_stderr(StringIO()):
            dispatch.push_texts(self.cfg, self.st, jobs, kind="realtime",
                                shot=_writing_shot, run=_fake_run(_proc(0)))
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE), \
             mock.patch.object(dispatch, "pixel_probe",
                               return_value=(0, 0, 0)), \
             redirect_stderr(StringIO()):
            # 恢复成功（状态变化）后再被挡 → 新记一笔，证明"仅记变化"
            dispatch.push_texts(self.cfg, self.st, jobs, kind="realtime",
                                shot=_writing_shot, run=_fake_run())
        pre = [d for d in self.st.recent_dispatches(10)
               if d["status"] == "pre_fail"]
        self.assertEqual(len(pre), 2)

    def test_push_texts_warns_baseline_missing_to_stderr(self):
        # M6：baseline 缺失的"放行带警告"必须可见——每个因此放行的 push 调用
        # 往守护日志（stderr）打一行中文，不再静默跳预检。
        ids = _mkintels(self.st, 1)
        err = StringIO()
        with redirect_stderr(err):
            res = dispatch.push_texts(self.cfg, self.st, [(ids[0], "A")],
                                      kind="realtime", shot=_noop_shot,
                                      run=_fake_run(_proc(0)))
        self.assertEqual(res, "ok")
        self.assertIn("预检未配置", err.getvalue())
        self.assertIn("groupwatchd", err.getvalue())

    def test_push_texts_success_marks_sent_and_tmp_0600(self):
        ids = _mkintels(self.st, 1)
        run = _fake_run(_proc(0, "SENT: ok"))
        res = dispatch.push_texts(self.cfg, self.st, [(ids[0], "【作业】正文")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "ok")
        self.assertEqual(_row_of(self.st, ids[0])["pushed"], "sent")
        self.assertTrue([d for d in self.st.recent_dispatches(5)
                         if d["status"] == "ok"])
        txts = list((self.home / "out").glob("*.txt"))
        self.assertEqual(len(txts), 1)
        # I3：身份命名——按 hold 行的 id 就能对上 out/ 文件，无位置序名。
        self.assertEqual(txts[0].name, f"realtime-{ids[0]}.txt")
        # brief 原文 "mode & 0o077 == 0o600" 不可满足（0o077 只有 6 bit，
        # 取不到 owner 位）——权限校验正确口径是 & 0o777。
        self.assertEqual(os.stat(txts[0]).st_mode & 0o777, 0o600)
        # 调用时刻（= wxsend 启动前）文件已存在且 0600
        self.assertEqual(run.calls[0]["mode_at_call"], 0o600)
        argv = run.calls[0]["argv"]
        self.assertEqual(argv[:2], [sys.executable,
                                    str(self.cfg.push.wxsend_py)])
        self.assertEqual(argv[2:4], ["--to", "班级群"])
        self.assertEqual(argv[4:6], ["--file", str(txts[0])])
        kw = run.calls[0]["kw"]
        self.assertEqual(kw["timeout"], 420)
        self.assertTrue(kw["capture_output"] and kw["text"])
        # out 目录本身 0700
        self.assertEqual(os.stat(self.home / "out").st_mode & 0o777, 0o700)

    def test_push_texts_realtime_out_files_identity_named(self):
        # I3：多条 realtime 批不再写 1.txt/2.txt——各写 realtime-<oid>.txt，
        # 与 intelligence.id 一一对应（hold 恢复食谱按 id 找文件）。
        ids = _mkintels(self.st, 2)
        run = _fake_run(_proc(0, "SENT: ok"), _proc(0, "SENT: ok"))
        res = dispatch.push_texts(self.cfg, self.st,
                                  [(ids[0], "A"), (ids[1], "B")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "ok")
        names = sorted(p.name for p in (self.home / "out").iterdir())
        self.assertEqual(names,
                         [f"realtime-{ids[0]}.txt", f"realtime-{ids[1]}.txt"])

    def test_push_texts_digest_out_file_labeled(self):
        # I3：digest 批（oid None）写 digest-<YYYYMMDD-HHMM>.txt——时刻取
        # push_texts 的 now（可注入），不再占用 <i>.txt 位置名。
        run = _fake_run(_proc(0, "SENT: ok"))
        stamp_now = 1764000000.0
        res = dispatch.push_texts(self.cfg, self.st, [(None, "2026-11-24 汇总")],
                                  kind="digest", shot=_noop_shot, run=run,
                                  now=stamp_now)
        self.assertEqual(res, "ok")
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(stamp_now))
        names = [p.name for p in (self.home / "out").iterdir()]
        self.assertEqual(names, [f"digest-{stamp}.txt"])

    def test_push_texts_needs_check_holds_and_stops_batch(self):
        ids = _mkintels(self.st, 2)
        run = _fake_run(_proc(3, "NEEDS_CHECK: 状态未知，勿盲目重发"))
        res = dispatch.push_texts(self.cfg, self.st,
                                  [(ids[0], "A"), (ids[1], "B")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:needs_check")
        self.assertEqual(len(run.calls), 1)  # 立刻中止，本批不再 wxsend
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "hold")  # 绝不自动重发
        self.assertEqual(row["retries"], 0)      # hold 不占重试点
        self.assertEqual(_row_of(self.st, ids[1])["pushed"], "pending")

    def test_push_texts_gui_partial_holds_not_retried(self):
        ids = _mkintels(self.st, 2)
        run = _fake_run(_proc(2, "REFUSE: aborted at step 2 (click-input): "
                                 "[...] rc=1；发送键尚未执行"))
        res = dispatch.push_texts(self.cfg, self.st,
                                  [(ids[0], "A"), (ids[1], "B")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:gui_partial")
        self.assertEqual(len(run.calls), 1)
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "hold")
        self.assertEqual(row["retries"], 0)  # 不 bump：状态部分已知也禁止自动重发

    def test_push_texts_pre_refuse_bumps_then_suppresses_at_cap(self):
        ids = _mkintels(self.st, 1)
        jobs = [(ids[0], "A")]
        run1 = _fake_run(_proc(2, "REFUSE: 未找到目标群"))
        res1 = dispatch.push_texts(self.cfg, self.st, jobs,
                                   kind="realtime", shot=_noop_shot, run=run1)
        self.assertEqual(res1, "stopped:pre_refuse")
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "pending")  # 未达上限：保持 pending
        self.assertEqual(row["retries"], 1)
        run2 = _fake_run(_proc(2, "REFUSE: 未找到目标群"))
        res2 = dispatch.push_texts(self.cfg, self.st,
                                   [(ids[0], "A")], kind="realtime",
                                   shot=_noop_shot, run=run2)
        self.assertEqual(res2, "stopped:pre_refuse")
        self.assertEqual(_row_of(self.st, ids[0])["pushed"], "suppressed")
        self.assertEqual(_row_of(self.st, ids[0])["retries"], 2)
        self.assertTrue([d for d in self.st.recent_dispatches(5)
                         if d["status"] == "suppressed"])

    def test_push_texts_lock_busy_keeps_pending_without_retry_bump(self):
        ids = _mkintels(self.st, 2)
        run = _fake_run(_proc(4, "LOCK_BUSY: 锁被占用"))
        res = dispatch.push_texts(self.cfg, self.st,
                                  [(ids[0], "A"), (ids[1], "B")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:lock_busy")
        self.assertEqual(len(run.calls), 1)  # 中止本批
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "pending")
        self.assertEqual(row["retries"], 0)  # 不 bump_retry

    def test_push_texts_respects_push_max_per_tick(self):
        # 上限由调用方 pending(limit) 把守：11 pending → 单批恰取 cfg 上限条；
        # push_texts 本身处理且只处理交进来的 jobs。
        ids = _mkintels(self.st, 11)
        self.assertEqual(self.cfg.limits.push_max_per_tick, 10)
        jobs = [(r["id"], f"正文{r['id']}")
                for r in self.st.pending(self.cfg.limits.push_max_per_tick)]
        self.assertEqual(len(jobs), 10)
        run = _fake_run()  # 全部 rc=0
        res = dispatch.push_texts(self.cfg, self.st, jobs, kind="realtime",
                                  shot=_noop_shot, run=run)
        self.assertEqual(res, "ok")
        self.assertEqual(len(run.calls), 10)
        self.assertEqual([r["id"] for r in self.st.pending(100)], [ids[10]])

    def test_push_texts_empty_text_treated_as_crash(self):
        ids = _mkintels(self.st, 1)
        run = _fake_run()
        res = dispatch.push_texts(self.cfg, self.st, [(ids[0], "")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:crash")
        self.assertEqual(len(run.calls), 0)
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "pending")
        self.assertEqual(row["retries"], 1)
        self.assertTrue([d for d in self.st.recent_dispatches(5)
                         if d["status"] == "crash"])

    def test_push_texts_run_oserror_treated_as_crash(self):
        ids = _mkintels(self.st, 1)
        run = _fake_run(OSError(2, "No such file or directory"))
        res = dispatch.push_texts(self.cfg, self.st, [(ids[0], "A")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:crash")
        self.assertEqual(_row_of(self.st, ids[0])["retries"], 1)

    def test_push_texts_missing_rc_attr_treated_as_crash(self):
        ids = _mkintels(self.st, 1)
        run = _fake_run(types.SimpleNamespace(stdout="没有 rc 属性"))
        res = dispatch.push_texts(self.cfg, self.st, [(ids[0], "A")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:crash")
        self.assertEqual(_row_of(self.st, ids[0])["retries"], 1)

    def test_push_texts_real_completedprocess_returncode(self):
        # 默认 run=subprocess.run 返回的是 CompletedProcess（.returncode 而非
        # .rc）：双属性口径必须都能走通，且 stdout 上的 REFUSE 行按合并文本判型。
        ids = _mkintels(self.st, 1)

        def _run(argv, **kw):
            return subprocess.CompletedProcess(argv, 2,
                                               stdout="REFUSE: 参数错误\n",
                                               stderr="")
        res = dispatch.push_texts(self.cfg, self.st, [(ids[0], "A")],
                                  kind="realtime", shot=_noop_shot, run=_run)
        self.assertEqual(res, "stopped:pre_refuse")
        self.assertEqual(_row_of(self.st, ids[0])["retries"], 1)

    def test_push_texts_signal_killed_routes_to_needs_check(self):
        # 审查裁决（2026-11-24）：wxsend 被信号杀死（rc<0，OOM/SIGKILL）可能
        # 死在按发送键之后、NEEDS_CHECK 打印之前——输出截断、状态未知，必须
        # hold 中止本批，绝不降级成 crash 让后续 tick 自动重发。
        ids = _mkintels(self.st, 2)
        run = _fake_run(_proc(-9, "[step 3/5] click send-input done\n"))
        res = dispatch.push_texts(self.cfg, self.st,
                                  [(ids[0], "A"), (ids[1], "B")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:needs_check")
        self.assertEqual(len(run.calls), 1)  # 第二任务本批绝不被碰
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "hold")
        self.assertEqual(row["retries"], 0)
        self.assertEqual(_row_of(self.st, ids[1])["pushed"], "pending")
        # 六路表补钉：负数 → needs_check；正数未知 / None 仍 crash
        self.assertEqual(dispatch.classify_wxsend(-9, "任意截断输出"),
                         "needs_check")
        self.assertEqual(dispatch.classify_wxsend(7, ""), "crash")
        self.assertEqual(dispatch.classify_wxsend(None, ""), "crash")

    def test_push_texts_timeout_expired_holds_not_retried(self):
        # 安全合同：420s 超时远超 wxsend 内部 120s 锁等待（wxsend.py 硬编码），
        # 说明已深入 GUI 阶段、状态未知 → 按 needs_check hold，禁止盲重发。
        ids = _mkintels(self.st, 2)
        run = _fake_run(subprocess.TimeoutExpired(
            [sys.executable, "wxsend.py"], 420))
        res = dispatch.push_texts(self.cfg, self.st,
                                  [(ids[0], "A"), (ids[1], "B")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "stopped:needs_check")
        self.assertEqual(len(run.calls), 1)
        row = _row_of(self.st, ids[0])
        self.assertEqual(row["pushed"], "hold")
        self.assertEqual(row["retries"], 0)

    def test_push_texts_digest_none_id_logs_only(self):
        before = {d["id"] for d in self.st.recent_dispatches(50)}
        run = _fake_run(_proc(0, "SENT: ok"))
        res = dispatch.push_texts(self.cfg, self.st, [(None, "2026-11-24 汇总")],
                                  kind="digest", shot=_noop_shot, run=run)
        self.assertEqual(res, "ok")
        self.assertEqual(self.st.counts()["intelligence"], 0)
        new = [d for d in self.st.recent_dispatches(50)
               if d["id"] not in before]
        self.assertEqual([(d["kind"], d["status"]) for d in new],
                         [("digest", "ok")])


class TestRequireEnrollGate(DispatchCase):
    """accept-fix 2：require_enroll=True（生产默认）时 baseline 缺失的
    "放行带警告"改判为拒绝盲发；foreground_ok 本身不动（观察归观察，
    决策归 push_texts——它才看得见 cfg.push）。"""

    def test_push_texts_require_enroll_blocks_baseline_missing(self):
        cfg = _cfg(self.home, require_enroll=True)
        ids = _mkintels(self.st, 1)
        run = _fake_run()
        res = dispatch.push_texts(cfg, self.st, [(ids[0], "A")],
                                  kind="realtime", shot=_noop_shot, run=run)
        self.assertEqual(res, "pre_fail")
        self.assertEqual(len(run.calls), 0)  # 零 wxsend，与 pre_fail 同构
        ds = [d for d in self.st.recent_dispatches(5)
              if d["status"] == "pre_fail"]
        self.assertEqual(len(ds), 1)
        self.assertIn("未 enroll", ds[0]["detail"])
        # 情报行原样 pending，绝不 hold/绝不 suppressed（闸门在 wxsend 之前）
        self.assertEqual(_row_of(self.st, ids[0])["pushed"], "pending")

    def test_push_texts_require_enroll_pre_fail_dedup_holds(self):
        # 被闸门连挡两批只记一笔（M5 语义在 require_enroll 路径同样成立）。
        cfg = _cfg(self.home, require_enroll=True)
        ids = _mkintels(self.st, 1)
        jobs = [(ids[0], "A")]
        dispatch.push_texts(cfg, self.st, jobs, kind="realtime",
                            shot=_noop_shot, run=_fake_run())
        dispatch.push_texts(cfg, self.st, jobs, kind="realtime",
                            shot=_noop_shot, run=_fake_run())
        pre = [d for d in self.st.recent_dispatches(10)
               if d["status"] == "pre_fail"]
        self.assertEqual(len(pre), 1)

    def test_push_texts_enrolled_baseline_match_unaffected_by_flag(self):
        # 已 enroll 且逐点匹配 → reason=="ok"，require_enroll 完全不参与。
        cfg = _cfg(self.home, require_enroll=True)
        ids = _mkintels(self.st, 1)
        run = _fake_run(_proc(0, "SENT: ok"))
        with mock.patch.object(dispatch, "load_probe_baseline",
                               return_value=BASE), \
             mock.patch.object(dispatch, "pixel_probe",
                               return_value=(100, 100, 100)):
            res = dispatch.push_texts(cfg, self.st, [(ids[0], "A")],
                                      kind="realtime", shot=_writing_shot,
                                      run=run)
        self.assertEqual(res, "ok")
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(_row_of(self.st, ids[0])["pushed"], "sent")


class TestProbePoints(DispatchCase):
    def test_probe_points_are_calibrated_1440x900_coords(self):
        self.assertEqual(
            dispatch.PROBE_POINTS,
            ({"name": "标题带", "x": 60, "y": 40},
             {"name": "侧栏", "x": 60, "y": 500},
             {"name": "输入区", "x": 700, "y": 880},
             {"name": "发送钮", "x": 1391, "y": 866}))

    def test_load_probe_baseline_missing_returns_none(self):
        self.assertIsNone(dispatch.load_probe_baseline(self.home))

    def test_load_probe_baseline_reads_file(self):
        (self.home / "probe_points.json").write_text(
            json.dumps(BASE2), encoding="utf-8")
        self.assertEqual(dispatch.load_probe_baseline(self.home), BASE2)


if __name__ == "__main__":
    unittest.main()
