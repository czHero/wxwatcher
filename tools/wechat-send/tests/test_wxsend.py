"""wxsend 编排测试（Task 6c 钉死序列：置顶行 row 模式 + final-review 修复）。

6b 联机事实：搜索胶囊不可点击（标题拖拽带）、Enter 无键名、发送=点击发送
按钮 → GUI 序列为 focus → open_search(=行点击, {IDENT}=str(row)) →
paste(text="")（模板 no-op）→ select_first(=点输入框+repeat backspace 清空)
→ paste(text=body) → send(=按钮点击 + 行点击收尾关闭会话, {IDENT}=str(row))。
2026-11-21 联机追加：行点击是 toggle（点开着的会话=关闭它），故 send 末尾
的行点击在「会话已开+选中」态下确定性关闭会话；GUI 序列开始后的一切退出
（含成功）都尽力清空剪贴板（数据形 argv 经 runner，rc/异常一律吞掉）。
假件全注入：假 roster/calib/wechat_running/runner/lock；find_remark、
latest_local_id 与 postcheck 经模块属性打桩。
"""
import fcntl
import io
import json
import os
import subprocess as sp
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wxsend
from dbverify import expected_table
from gui_driver import Calib
from roster import Target, load_roster
from sqlcipher_db import SQLCipherError

CONTACT_HEX = "11" * 32
MESSAGE_HEX = "22" * 32

# 与真实 ~/.wechat-monitor/calib.json（6b 校准产物）同形的测试假件。
CLAMP = ["sudo", "ydotool", "mousemove", "--", "-5000", "-5000"]
CALIB = Calib(
    (1440, 900),
    {
        "focus": [["/usr/bin/true"]],
        "open_search": [CLAMP, ["sudo", "ydotool", "mousemove", "190", "{IDENT}"],
                        ["sudo", "ydotool", "click", "1"]],
        "paste": [["wl-copy", "{TEXT}"], ["sudo", "ydotool", "key", "ctrl+v"]],
        "select_first": [CLAMP, ["sudo", "ydotool", "mousemove", "600", "805"],
                         ["sudo", "ydotool", "click", "1"],
                         ["sudo", "ydotool", "key", "--repeat", "60", "backspace"]],
        # send 末尾追加行点击（{IDENT}=str(row)）：发送时会话处于打开+选中态，
        # toggle 行为使该行点击确定性关闭会话（final-review ADDENDUM #1）。
        "send": [CLAMP, ["sudo", "ydotool", "mousemove", "1391", "866"],
                 ["sudo", "ydotool", "click", "1"],
                 CLAMP, ["sudo", "ydotool", "mousemove", "190", "{IDENT}"],
                 ["sudo", "ydotool", "click", "1"]],
    },
)

BACKSPACE = ["sudo", "ydotool", "key", "--repeat", "60", "backspace"]
SEND_CLICK = ["sudo", "ydotool", "click", "1"]
SEND_MOVE = ["sudo", "ydotool", "mousemove", "1391", "866"]
# 剪贴板清理（报告隐私）：数据形 argv 经 run_steps/runner，best-effort。
CLEAR = ["sh", "-c", "wl-copy --clear >/dev/null 2>&1"]

ROSTER = {
    # 无 wxid 的 user：保留备注唯一性前置（ident 只用于 find_remark）
    "zhang": Target("zhang", "user", "RPT-张三", row=141),
    # 无 row：必须拒发
    "norow": Target("norow", "user", "RPT-无行"),
    # wxid 直连表名：find_remark 绝不调用
    "filehelper": Target("filehelper", "user", "文件传输助手", row=141,
                         wxid="filehelper"),
    # group：同样 row 制，不查 contact.db
    "team": Target("team", "group", "123@chatroom", row=300),
    "team_norow": Target("team_norow", "group", "456@chatroom"),
    # row 越出屏幕高度：roster 只管类型，联机拒绝归 wxsend（calib.screen）
    "tall": Target("tall", "user", "RPT-高", row=2000),
}


# 完整 row 模式 GUI 序列（18 条 argv + 末尾剪贴板清理 = 19）：行 y 坐标经
# open_search 与 send 收尾行点击的 {IDENT} 进入，paste(ident) 以 text=""
# 空跑模板，body 只在最后一次 wl-copy 出现；成功后会话为关闭态。
def expected_sequence(row, body):
    return [
        ["/usr/bin/true"],
        CLAMP,
        ["sudo", "ydotool", "mousemove", "190", str(row)],
        ["sudo", "ydotool", "click", "1"],
        ["wl-copy", ""],
        ["sudo", "ydotool", "key", "ctrl+v"],
        CLAMP,
        ["sudo", "ydotool", "mousemove", "600", "805"],
        ["sudo", "ydotool", "click", "1"],
        BACKSPACE,
        ["wl-copy", body],
        ["sudo", "ydotool", "key", "ctrl+v"],
        CLAMP,
        SEND_MOVE,
        SEND_CLICK,
        CLAMP,
        ["sudo", "ydotool", "mousemove", "190", str(row)],
        SEND_CLICK,
        CLEAR,
    ]


class SendTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        db_keys = self.dir / "db_keys.json"
        db_keys.write_text(
            json.dumps({"keys": {"contact.db": CONTACT_HEX, "message_0.db": MESSAGE_HEX}}),
            encoding="utf-8",
        )
        self.paths = wxsend.Paths(
            db_keys=db_keys,
            contact_db=self.dir / "contact.db",   # 不存在没关系：桩接管全部 db 访问
            message_db=self.dir / "message_0.db",
            lock=self.dir / "wxsend.lock",
        )
        self.post_calls = []
        self.baseline_calls = []

    def fake_postcheck(self, result=True):
        def _pc(message_db, key_hex, table, sent_text, **kw):
            self.post_calls.append(
                {"db": Path(message_db), "key": key_hex, "table": table,
                 "text": sent_text, "after_id": kw.get("after_id")}
            )
            return result
        return _pc

    def fake_latest(self, result=7):
        def _ll(message_db, key_hex, table):
            self.baseline_calls.append(
                {"db": Path(message_db), "key": key_hex, "table": table}
            )
            return result
        return _ll

    def invoke(self, *, key="zhang", text="你好 世界", force=False,
               nowait=False, dry_run=False, wechat_running=lambda: True,
               find_remark=lambda *a: ["wxid_u1"], postcheck=None,
               latest=None, acquire_lock=lambda: None):
        calls = []
        runner = calls.append
        if postcheck is None:
            postcheck = self.fake_postcheck(True)
        if latest is None:
            latest = self.fake_latest(7)
        buf = io.StringIO()
        with mock.patch.object(wxsend, "find_remark", find_remark), \
             mock.patch.object(wxsend, "postcheck", postcheck), \
             mock.patch.object(wxsend, "latest_local_id", latest), \
             redirect_stdout(buf):
            rc = wxsend.send(
                key, text,
                paths=self.paths, roster=ROSTER, calib=CALIB,
                wechat_running=wechat_running,
                acquire_lock=acquire_lock, runner=runner,
                force=force, nowait=nowait, dry_run=dry_run,
            )
        return rc, calls, buf.getvalue()

    # ---- the pinned tests ---------------------------------------------------

    def test_happy_path_sequence(self):
        # 序列之心：逐条 argv 精确钉死（clamp/190/"141"/click、wl-copy ""、
        # repeat-backspace、wl-copy body、send 按钮点击）。
        rc, calls, out = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, expected_sequence(141, "你好 世界"))
        # 表名来自 find_remark 唯一命中（无 wxid 的 user 仍走备注前置），
        # 密钥取 message_0.db 项；回执要求 local_id 增长（基线 7 传入）
        self.assertEqual(
            self.post_calls,
            [{"db": self.paths.message_db, "key": MESSAGE_HEX,
              "table": expected_table("wxid_u1"), "text": "你好 世界",
              "after_id": 7}],
        )
        self.assertEqual(
            self.baseline_calls,
            [{"db": self.paths.message_db, "key": MESSAGE_HEX,
              "table": expected_table("wxid_u1")}],
        )
        self.assertIn("RECEIPT", out)
        # send 收尾行点击带 {IDENT}=str(row)，成功后剪贴板被清理
        self.assertEqual(calls[-4:], [CLAMP,
                                      ["sudo", "ydotool", "mousemove", "190", "141"],
                                      SEND_CLICK, CLEAR])

    def test_user_without_row_refuses_before_any_runner_call(self):
        rc, calls, out = self.invoke(key="norow")
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("row", out)
        self.assertIn("REFUSE", out)
        # 拒发指引必须指向「新截图量取置顶行 y → roster row」，
        # 不再指向 calibrate（它没有量 row 的能力）
        self.assertIn("截图", out)
        self.assertNotIn("calibrate", out)

    def test_group_without_row_refuses(self):
        # group 的 'search' 词故事已退役：与 user 一样必须带 row。
        rc, calls, out = self.invoke(key="team_norow")
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("row", out)

    def test_group_with_row_uses_row_sequence(self):
        def boom(*a):
            raise AssertionError("find_remark must not run for group targets")
        rc, calls, out = self.invoke(key="team", find_remark=boom)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, expected_sequence(300, "你好 世界"))
        self.assertEqual(
            self.post_calls[0]["table"], expected_table("123@chatroom"))
        # group 同样做消息库基线探测（FP#3 全覆盖），send 收尾用 group 的 row
        self.assertEqual(
            self.baseline_calls,
            [{"db": self.paths.message_db, "key": MESSAGE_HEX,
              "table": expected_table("123@chatroom")}],
        )
        self.assertEqual(calls[-4:-1],
                         [CLAMP, ["sudo", "ydotool", "mousemove", "190", "300"],
                          SEND_CLICK])

    def test_row_beyond_screen_height_refuses(self):
        rc, calls, out = self.invoke(key="tall")
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("row", out)

    def test_wxid_bypasses_find_remark(self):
        # roster 带 wxid 的 user：contact.db 完全不碰，表=expected_table(wxid)。
        def boom(*a):
            raise AssertionError("find_remark must not run when wxid is set")
        rc, calls, out = self.invoke(key="filehelper", find_remark=boom)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, expected_sequence(141, "你好 世界"))
        self.assertEqual(
            self.post_calls,
            [{"db": self.paths.message_db, "key": MESSAGE_HEX,
              "table": expected_table("filehelper"), "text": "你好 世界",
              "after_id": 7}],
        )
        # wxid 直连目标同样做消息库基线探测（FP#3：密钥前置验证全覆盖）
        self.assertEqual(
            self.baseline_calls,
            [{"db": self.paths.message_db, "key": MESSAGE_HEX,
              "table": expected_table("filehelper")}],
        )

    def test_bad_row_type_in_roster_raises_valueerror(self):
        path = self.dir / "roster.json"
        path.write_text(json.dumps(
            {"targets": {"x": {"kind": "user", "ident": "R", "row": "141"}}}),
            encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_roster(path)
        self.assertIn("row", str(cm.exception))

    def test_refuses_when_wechat_not_running(self):
        # FP#4：微信未运行 → 拒发，且不碰锁、不碰库、零命令执行
        def boom(*a):
            raise AssertionError("find_remark must not run before wechat check")
        rc, calls, _ = self.invoke(wechat_running=lambda: False, find_remark=boom)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_stale_keys_refuse_without_force(self):
        # FP#3：前置解密失败原样上抛的 SQLCipherError → 拒发 exit 2
        def stale(*a):
            raise SQLCipherError("file is not a database")
        rc, calls, out = self.invoke(find_remark=stale)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("REFUSE", out)

        def must_not_run(*a):
            raise AssertionError("find_remark must not run with --force")
        def pc_must_not_run(*a, **k):
            raise AssertionError("postcheck must not run with --force")
        rc, calls, out = self.invoke(force=True, find_remark=must_not_run,
                                     postcheck=pc_must_not_run)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, expected_sequence(141, "你好 世界"))  # GUI 照常
        self.assertIn("WARN", out)
        self.assertNotIn("RECEIPT", out)  # 不宣称回执

    def test_ambiguous_remark_refuses(self):
        # FP#2：无 wxid 的 user 仍受备注唯一性保护——撞名多义 → 宁可拒发也不猜
        rc, calls, out = self.invoke(find_remark=lambda *a: ["wxid_a1", "wxid_a2"])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("REFUSE", out)

    def test_missing_remark_refuses(self):
        rc, calls, out = self.invoke(find_remark=lambda *a: [])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("REFUSE", out)

    def test_postcheck_timeout_exits_3_no_retry(self):
        rc, calls, out = self.invoke(postcheck=self.fake_postcheck(False))
        self.assertEqual(rc, 3)
        # GUI 序列只执行一遍：send 阶段移动只出现一次，绝不自动重发
        # （click 1 在多个 phase 各出现一次，不能用来数 send）
        self.assertEqual(calls.count(SEND_MOVE), 1)
        self.assertEqual(calls, expected_sequence(141, "你好 世界"))
        self.assertEqual(calls[-1], CLEAR)  # 回执超时退出前也清剪贴板
        self.assertIn("NEEDS_CHECK", out)

    def test_lock_serializes_and_nowait_exits(self):
        # FP#5：真 flock 在临时锁文件上（绝不碰 ~/.wechat-monitor）
        held = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(lambda: (fcntl.flock(held, fcntl.LOCK_UN), os.close(held)))
        rc, calls, _ = self.invoke(nowait=True, acquire_lock=None)
        self.assertEqual(rc, 4)
        self.assertEqual(calls, [])
        # 阻塞模式：0.1s 后另一线程释放 → 等到锁并成功发送
        threading.Timer(0.1, lambda: fcntl.flock(held, fcntl.LOCK_UN)).start()
        rc, calls, _ = self.invoke(acquire_lock=None)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, expected_sequence(141, "你好 世界"))

    def test_multiline_text_sent_as_single_message(self):
        # FP#1：多行文本（含空行与尾随空格）作为单个 wl-copy argv；
        # 只去尾随空白（rstrip），前导空白必须保留（Review-Focus #1）。
        # row 模式下有两次 wl-copy：模板 no-op（""）+ 消息本体。
        raw = "  第一行\n\n第二行   "
        want = "  第一行\n\n第二行"
        rc, calls, _ = self.invoke(text=raw)
        self.assertEqual(rc, 0)
        copies = [c for c in calls if "wl-copy" in c]
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0], ["wl-copy", ""])  # ident 粘贴 = 无害空跑
        self.assertEqual(copies[1], ["wl-copy", want])  # 整段文本恰好一个 argv
        self.assertEqual(self.post_calls[0]["text"], want)

    def test_dry_run_prints_row_plan_executes_nothing(self):
        lock_used = []
        rc, calls, out = self.invoke(
            dry_run=True, acquire_lock=lambda: lock_used.append(True))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertEqual(lock_used, [])
        self.assertIn("DRY-RUN", out)
        self.assertIn("'141'", out)                    # {IDENT} = str(row)
        self.assertIn(expected_table("wxid_u1"), out)  # 计划含目标表
        self.assertEqual(self.baseline_calls, [])      # dry-run 不做基线探测
        # 无 row 的目标：dry-run 也照常拒发（拒发条件不因 dry-run 豁免）
        rc, calls, out = self.invoke(key="norow", dry_run=True)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("row", out)


    # ---- review fix round 1: per-command rc + unexpected exception taxonomy --

    def _send_with_runner(self, runner, **kw):
        buf = io.StringIO()
        key = kw.pop("key", "zhang")
        text = kw.pop("text", "你好 世界")
        with mock.patch.object(wxsend, "find_remark", lambda *a: ["wxid_u1"]), \
             mock.patch.object(wxsend, "postcheck", self.fake_postcheck(True)), \
             mock.patch.object(wxsend, "latest_local_id", self.fake_latest(7)), \
             redirect_stdout(buf):
            rc = wxsend.send(
                key, text,
                paths=self.paths, roster=ROSTER, calib=CALIB,
                wechat_running=lambda: True,
                acquire_lock=lambda: None, runner=runner, **kw)
        return rc, buf.getvalue()

    def test_failing_middle_step_aborts_before_send_exit_2(self):
        # Important 1：send 阶段之前的命令失败（这里选清空输入的回退键）
        # → 截断序列，绝不把键打进未知界面。
        calls = []

        def runner(argv):
            calls.append(argv)
            return sp.CompletedProcess(argv, 1 if argv == BACKSPACE else 0)

        seq = expected_sequence(141, "你好 世界")
        rc, out = self._send_with_runner(runner)
        self.assertEqual(rc, 2)
        # 截断在失败步（含）+ 末尾 best-effort 剪贴板清理（GUI 已开始）
        self.assertEqual(calls, seq[:10] + [CLEAR])
        self.assertNotIn(SEND_MOVE, calls)             # send 阶段从未开始
        self.assertIn("aborted at step 10", out)       # 用户消息 1-based
        self.assertIn("rc=1", out)
        self.assertIn("草稿", out)                     # 残留草稿警示
        self.assertEqual(self.post_calls, [])          # 未发送 → 不做回执校验

    def test_failing_send_step_exits_3_needs_check(self):
        # Important 1：失败发生在 send 阶段本身 → 送达状态未知 → exit 3
        calls = []

        def runner(argv):
            calls.append(argv)
            return sp.CompletedProcess(argv, 1 if argv == SEND_MOVE else 0)

        rc, out = self._send_with_runner(runner)
        self.assertEqual(rc, 3)
        self.assertEqual(calls, expected_sequence(141, "你好 世界")[:14] + [CLEAR])
        self.assertIn("NEEDS_CHECK", out)
        self.assertIn("aborted at step 14", out)       # 用户消息 1-based
        self.assertIn("草稿", out)                     # 残留草稿警示

    def test_runner_exception_exits_3_no_traceback(self):
        # Important 2：runner 中途抛异常（如 wl-copy 缺失）→ exit 3 + NEEDS_CHECK，
        # 不得以 traceback 形式逃逸成 Python 退出码 1。
        calls = []

        def runner(argv):
            calls.append(argv)
            if len(calls) == 3:
                raise FileNotFoundError(2, "No such file or directory", "wl-copy")
            return None

        rc, out = self._send_with_runner(runner)
        self.assertEqual(rc, 3)
        self.assertIn("NEEDS_CHECK: unexpected error after partial execution", out)
        self.assertIn("do NOT blindly resend", out)
        self.assertIn("草稿", out)                     # 残留草稿警示
        self.assertEqual(len(calls), 4)                # 3 步 + 剪贴板清理
        self.assertEqual(calls[-1], CLEAR)
        self.assertEqual(self.post_calls, [])

    # ---- final-review fix wave: baseline probe / clipboard hygiene ----------

    def test_baseline_probe_inside_lock_right_before_gui(self):
        events = []

        class CM:
            def __enter__(self):
                events.append("lock")

            def __exit__(self, *exc):
                events.append("unlock")
                return False

        def latest(db, key, table):
            events.append("probe")
            return 7

        def runner(argv):
            events.append("gui")

        buf = io.StringIO()
        with mock.patch.object(wxsend, "find_remark", lambda *a: ["wxid_u1"]), \
             mock.patch.object(wxsend, "postcheck", self.fake_postcheck(True)), \
             mock.patch.object(wxsend, "latest_local_id", latest), \
             redirect_stdout(buf):
            rc = wxsend.send(
                "zhang", "你好 世界",
                paths=self.paths, roster=ROSTER, calib=CALIB,
                wechat_running=lambda: True,
                acquire_lock=lambda: CM(), runner=runner)
        self.assertEqual(rc, 0)
        # 探测在锁内、第一条 GUI 命令之前；锁最后释放
        self.assertEqual(events[:3], ["lock", "probe", "gui"])
        self.assertEqual(events[-1], "unlock")
        self.assertEqual(events.count("gui"), 19)      # 18 步 + 清理
        self.assertEqual(self.post_calls[0]["after_id"], 7)

    def test_probe_sqlcipher_error_refuses_exit2_zero_gui_all_kinds(self):
        # FP#3 恢复：wxid 直连 / group 此前完全跳过消息库密钥验证，
        # 坏密钥直到 postcheck 才暴露（exit 3 after send）。现在基线探测
        # 对每种目标都在 GUI 之前验证密钥 → REFUSE 2，零 runner 调用。
        def stale(*a):
            raise SQLCipherError("file is not a database")

        for key in ("zhang", "filehelper", "team"):
            self.post_calls.clear()
            rc, calls, out = self.invoke(key=key, latest=stale)
            self.assertEqual(rc, 2, key)
            self.assertEqual(calls, [], key)
            self.assertIn("REFUSE", out)
            self.assertEqual(self.post_calls, [], key)

    def test_probe_none_wires_after_id_none(self):
        # 新会话表尚未建出/空表 → 基线 None → postcheck 保持旧行为
        rc, calls, out = self.invoke(latest=lambda *a: None)
        self.assertEqual(rc, 0)
        self.assertIsNone(self.post_calls[0]["after_id"])

    def test_force_skips_baseline_probe(self):
        def boom(*a):
            raise AssertionError("latest_local_id must not run with --force")

        def pc_boom(*a, **k):
            raise AssertionError("postcheck must not run with --force")

        rc, calls, out = self.invoke(force=True, latest=boom, postcheck=pc_boom)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, expected_sequence(141, "你好 世界"))
        self.assertIn("WARN", out)

    def test_clipboard_clear_rc_failure_does_not_flip_exit_code(self):
        def runner(argv):
            return sp.CompletedProcess(argv, 1 if argv == CLEAR else 0)

        rc, out = self._send_with_runner(runner)
        self.assertEqual(rc, 0)                        # 清理 rc≠0 被吞掉
        self.assertIn("RECEIPT", out)

    def test_clipboard_clear_exception_does_not_flip_exit_code(self):
        def runner(argv):
            if argv == CLEAR:
                raise OSError("wl-copy missing")
            return None

        rc, out = self._send_with_runner(runner)
        self.assertEqual(rc, 0)
        self.assertIn("RECEIPT", out)

    def test_clipboard_clear_failure_keeps_abort_exit_codes(self):
        def runner(argv):
            if argv == CLEAR:
                return sp.CompletedProcess(argv, 9)
            return sp.CompletedProcess(argv, 1 if argv == BACKSPACE else 0)

        rc, out = self._send_with_runner(runner)
        self.assertEqual(rc, 2)                        # 清理失败不改判 2

    def test_postcheck_decrypt_failure_exits_3_with_clear(self):
        # 解密失败 ≠ 超时（保留项）：postcheck 抛 SQLCipherError → 3，
        # 且 GUI 已开始 → 剪贴板照常清理。
        def pc(*a, **k):
            raise SQLCipherError("file is not a database")

        rc, calls, out = self.invoke(postcheck=pc)
        self.assertEqual(rc, 3)
        self.assertEqual(calls[-1], CLEAR)
        self.assertIn("NEEDS_CHECK", out)

    def test_malformed_key_hex_refuses_exit2(self):
        # db_keys 条目必须是 64 位 hex；坏条目 → REFUSE 2（不是 ValueError
        # traceback exit 1），零 runner 调用。
        for bad in ("zz-not-hex", "11" * 31, 123, None):
            self.paths.db_keys.write_text(
                json.dumps({"keys": {"contact.db": CONTACT_HEX,
                                     "message_0.db": bad}}),
                encoding="utf-8",
            )
            rc, calls, out = self.invoke()
            self.assertEqual(rc, 2, repr(bad))
            self.assertEqual(calls, [], repr(bad))
            self.assertIn("REFUSE", out)
            self.assertIn("hex", out)


class BuildPathsTest(unittest.TestCase):
    def test_missing_db_raises_missingdb(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with self.assertRaises(wxsend.MissingDB):
                wxsend.build_paths(base)          # 无 db_keys.json
            (base / "db_keys.json").write_text('{"keys": {}}', encoding="utf-8")
            # db_keys 有了，但 default_wechat_files() 下无该账号目录结构时仍 MissingDB
            # （CI 机器上 xwechat_files 不存在 → 天然覆盖；存在则跳过断言）
            if not wxsend.default_wechat_files().is_dir():
                with self.assertRaises(wxsend.MissingDB):
                    wxsend.build_paths(base)

    def test_picks_newest_account_by_mtime_not_name(self):
        # 「最新账号」按 db_storage mtime（与 monitor find_db_storages 一致），
        # 不是字典序：字典序最大的账号若更旧，必须选 mtime 更新的那个。
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wf = base / "xwechat_files"
            (base / "db_keys.json").write_text('{"keys": {}}', encoding="utf-8")
            for acct in ("a_older_name", "z_newer_name"):
                d = wf / acct / "db_storage"
                (d / "contact").mkdir(parents=True)
                (d / "message").mkdir(parents=True)
                (d / "contact" / "contact.db").write_bytes(b"")
                (d / "message" / "message_0.db").write_bytes(b"")
            os.utime(wf / "z_newer_name" / "db_storage", (1000, 1000))
            os.utime(wf / "a_older_name" / "db_storage", (2000, 2000))
            with mock.patch.object(wxsend, "default_wechat_files", lambda: wf):
                paths = wxsend.build_paths(base)
            self.assertEqual(
                paths.message_db,
                wf / "a_older_name" / "db_storage" / "message" / "message_0.db")
            self.assertEqual(
                paths.contact_db,
                wf / "a_older_name" / "db_storage" / "contact" / "contact.db")


class FileLockClockTest(unittest.TestCase):
    def test_timeout_uses_injected_clock_and_sleep(self):
        # clock 与 sleep 对称可注入：假时钟推进即超时，绝不真实自旋。
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "wxsend.lock"
            held = os.open(lock, os.O_RDWR | os.O_CREAT)
            fcntl.flock(held, fcntl.LOCK_EX)
            self.addCleanup(lambda: (fcntl.flock(held, fcntl.LOCK_UN), os.close(held)))
            times = [0.0, 0.5, 1.0]
            sleeps = []
            fl = wxsend.FileLock(
                lock, nowait=False, poll_s=0.2, max_wait_s=1.0,
                sleep=sleeps.append, clock=lambda: times.pop(0))
            with self.assertRaises(wxsend.LockBusy):
                fl.__enter__()
            self.assertEqual(sleeps, [0.2])
            self.assertEqual(times, [])            # 时钟恰好消费完


class MainFileTest(unittest.TestCase):
    def test_non_utf8_file_refuses_exit2(self):
        # --file 非 UTF-8 → UnicodeDecodeError（ValueError 子类）必须被
        # 捕获为 REFUSE 2，而不是 traceback exit 1。
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            roster = d / "roster.json"
            roster.write_text(json.dumps(
                {"targets": {"zhang": {"kind": "user", "ident": "RPT-张三",
                                       "row": 141}}}), encoding="utf-8")
            calib = d / "calib.json"
            calib.write_text(json.dumps(
                {"screen": list(CALIB.screen), "steps": CALIB.steps}),
                encoding="utf-8")
            msg = d / "msg.txt"
            msg.write_bytes(b"\xff\xfe\x00 not utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = wxsend.main(["--to", "zhang", "--file", str(msg),
                                  "--roster", str(roster), "--calib", str(calib)])
            self.assertEqual(rc, 2)
            self.assertIn("REFUSE", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
