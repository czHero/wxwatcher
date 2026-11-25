"""wxsend：一条命令安全地给指定目标发消息，并给出数据库侧回执。

编排顺序（顺序本身是安全设计）：
    参数/目标校验 → [--dry-run 打印计划即退] → wechat_running 检查 →
    非阻塞自旋 flock（串行化所有发送）→ 前置校验（wxid 直连表 /
    备注→唯一 wxid → expected_table）→ 消息库 local_id 基线探测
    （latest_local_id，对每种目标都验证消息密钥；锁内、GUI 之前）→
    GUI 步骤（只经 expand_steps/run_steps 构造与执行）→
    后置校验（postcheck 轮询落库，要求 local_id > 基线）。

退出码：0 成功（有回执）；2 拒发-前置（含 --force 之外的降级入口拒绝）；
3 NEEDS_CHECK（消息可能已发出但回执超时——绝不自动重发）；4 锁占用/nowait。

GUI 阶段序列（row 模式，Task 6b 联机结论——搜索胶囊在标题拖拽带内点不中、
Enter 无可用键名，搜索路线出局）：
    focus → open_search(ident=str(row)，即点击置顶会话行) →
    paste(text="")（paste 模板的无害空跑）→ select_first（点输入框并
    repeat backspace 清空残留草稿）→ paste(text=body) → send（发送按钮
    点击 + 行点击收尾：send 模板同样代入 ident=str(row)，发送时会话处于
    打开+选中态，行点击的 toggle 行为确定性关闭会话——每次成功发送结束
    时会话为关闭态）。send 按钮之后不再有任何键（收尾只是鼠标点击）。
GUI 序列一旦开始，任何退出路径（含成功）都尽力清空剪贴板（报告隐私）：
数据形 argv ["sh","-c","wl-copy --clear >/dev/null 2>&1"] 经 run_steps
执行，其 rc/异常一律吞掉，绝不改变退出码。
所有可发送目标必须带 roster row（缺 → exit 2，先于任何 runner 调用）。
表标识：user 有 wxid → 直连 expected_table(wxid)，contact.db 不碰；
user 无 wxid → 仍走备注唯一性前置（RPT- 备注 → 唯一 wxid）；group 表
标识即 chatroom id 本身。roster 'search' 词已退役。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TOOLS = _HERE.parent
for _p in (str(_HERE), str(_TOOLS / "wechat-monitor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dbverify import (  # noqa: E402
    expected_table,
    find_remark,
    latest_local_id,
    postcheck,
)
from gui_driver import expand_steps, load_calib, run_steps  # noqa: E402
from paths import default_wechat_files  # noqa: E402
from roster import load_roster, resolve  # noqa: E402
from sqlcipher_db import SQLCipherError  # noqa: E402

EXIT_OK = 0
EXIT_REFUSE = 2          # 拒发：前置校验/参数/环境不满足，一条命令都没执行
EXIT_NEEDS_CHECK = 3     # GUI 已执行但回执超时/失败，须人工核对，禁止自动重发
EXIT_LOCK_BUSY = 4       # 锁被占用且 --nowait（或等待超时）

DEFAULT_BASE = Path.home() / ".wechat-monitor"


class MissingDB(RuntimeError):
    """contact.db / message_0.db / db_keys.json 等必需数据缺失。"""


class LockBusy(RuntimeError):
    """wxsend 锁无法获得（--nowait 立即失败，或等待超时）。"""


@dataclass
class Paths:
    db_keys: Path
    contact_db: Path
    message_db: Path
    lock: Path


def build_paths(base: Path) -> Paths:
    """从 db_keys.json + default_wechat_files() 组装运行期路径。

    db_keys.json 的 'keys' 以库文件 basename（contact.db / message_0.db）
    为键，这里按同名 basename 在 xwechat_files/<account>/db_storage 下定位
    实际库文件；取同时含两库、db_storage mtime 最新的账号目录（与
    monitor 的 find_db_storages 同一判据，不是字典序）。
    """
    base = Path(base)
    db_keys = base / "db_keys.json"
    if not db_keys.is_file():
        raise MissingDB(f"db_keys.json 缺失: {db_keys}（先运行 verify_key.py）")
    wf = default_wechat_files()
    contacts: dict[str, Path] = {}
    messages: dict[str, Path] = {}
    if wf.is_dir():
        for p in wf.glob("*/db_storage/contact/contact.db"):
            contacts[p.parts[-4]] = p
        for p in wf.glob("*/db_storage/message/message_0.db"):
            messages[p.parts[-4]] = p
    common = contacts.keys() & messages.keys()
    if not common:
        raise MissingDB(f"contact.db / message_0.db 未找到（{wf}/*/*/ 下）")
    acct = max(common, key=lambda a: messages[a].parents[1].stat().st_mtime)
    return Paths(db_keys=db_keys, contact_db=contacts[acct],
                 message_db=messages[acct], lock=base / "wxsend.lock")


def wechat_running_probe() -> bool:
    """生产默认探测：/proc 中有进程 exe 命中 /opt/wechat/wechat* 即在线。"""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for pid in entries:
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if exe.startswith("/opt/wechat/wechat"):
            return True
    return False


class FileLock:
    """wxsend 全局发送锁：非阻塞 flock 自旋 + 小睡。

    --nowait（nowait=True）只作为“立即失败”模式；默认在 max_wait_s 内
    等待，超时报 LockBusy（→ exit 4），避免无人值守时永久挂死。
    """

    def __init__(self, path: Path, *, nowait: bool = False, poll_s: float = 0.2,
                 max_wait_s: float = 120.0, sleep=time.sleep,
                 clock=time.monotonic):
        self.path = Path(path)
        self.nowait = nowait
        self.poll_s = poll_s
        self.max_wait_s = max_wait_s
        self.sleep = sleep
        self.clock = clock
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            deadline = self.clock() + self.max_wait_s
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fh = fh
                    return self
                except OSError:
                    if self.nowait:
                        raise LockBusy(f"{self.path} 已被占用（--nowait）") from None
                    if self.clock() >= deadline:
                        raise LockBusy(
                            f"{self.path} 等待超过 {self.max_wait_s}s") from None
                    self.sleep(self.poll_s)
        except BaseException:
            fh.close()
            raise

    def __exit__(self, *exc):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


def _plan_steps(calib, row: int, body: str):
    """(阶段名, argv 列表) 序列——一切步骤只经 expand_steps 构造（rule 3），
    绝不手工拼 argv（Task 4 的加载期 Return 禁令由此必然生效）。

    row 模式：{IDENT} 承载置顶行的 y 坐标字符串，open_search 与 send 两相
    都代入 ident=str(row)——send 模板末尾的行点击在「会话已开+选中」态下
    借 toggle 行为确定性关闭会话（ADDENDUM #1）；open_search 之后紧跟的
    paste(text="") 是钉死 paste 模板的无害空跑（schema 兼容保留）。
    """
    return [
        ("focus", expand_steps(calib, "focus")),
        ("open_search", expand_steps(calib, "open_search", ident=str(row))),
        ("paste(ident)", expand_steps(calib, "paste", text="")),
        ("select_first", expand_steps(calib, "select_first")),
        ("paste(text)", expand_steps(calib, "paste", text=body)),
        ("send", expand_steps(calib, "send", ident=str(row))),
    ]


def _result_rc(result) -> int:
    """把 runner 返回值归一为返回码：None（假 runner）/无 returncode → 0。"""
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    rc = getattr(result, "returncode", None)
    return 0 if rc is None else int(rc)


def _run_plan(plan, runner):
    """逐条经 run_steps 执行；首个非零返回码即截断（不再打出后续键）。

    返回 None = 全部成功；否则 (phase, argv, step_index, rc)。
    """
    idx = 0
    for phase, step_list in plan:
        for argv in step_list:
            rc = _result_rc(run_steps([argv], runner)[0])
            if rc != 0:
                return (phase, argv, idx, rc)
            idx += 1
    return None


# 剪贴板清理（报告隐私）：数据形 argv 经 run_steps/runner 执行，与 GUI 步骤
# 同一路径；wl-copy 守护进程的 stdout/stderr 重定向掉，管道上下文不挂起。
CLEAR_CLIPBOARD_ARGV = ["sh", "-c", "wl-copy --clear >/dev/null 2>&1"]


def _clear_clipboard(runner):
    """best-effort 清空剪贴板：rc 与异常一律吞掉，绝不改变退出码。"""
    try:
        run_steps([list(CLEAR_CLIPBOARD_ARGV)], runner)
    except Exception:
        pass


def _run_plan_with_clear(plan, runner):
    """执行 GUI 序列；序列一旦开始，任何退出（成功/截断/异常）都清剪贴板。

    返回 (fail, exc)：至多一个非 None；双 None = 全部成功。
    """
    try:
        return _run_plan(plan, runner), None
    except Exception as exc:
        return None, exc
    finally:
        _clear_clipboard(runner)


def _report_step_failure(fail) -> int:
    """失败步上报：send 之前失败=未发出任何东西→exit 2；send 阶段失败=送达
    状态未知→exit 3（绝不重发）。步号对用户 1-based；两路都提示残留草稿。"""
    phase, argv, idx, rc = fail
    if phase == "send":
        print(f"NEEDS_CHECK: send 阶段命令失败 — aborted at step {idx + 1}: {argv} "
              f"rc={rc}；消息可能已发出，请人工核对，切勿盲目重发；"
              "输入框可能残留草稿，请清理草稿后再人工发送")
        return EXIT_NEEDS_CHECK
    print(f"REFUSE: aborted at step {idx + 1} ({phase}): {argv} rc={rc}；"
          "发送键尚未执行，未发出任何消息；输入框可能残留草稿，请清理草稿后再人工发送")
    return EXIT_REFUSE


def _needs_check_unexpected(exc: Exception) -> int:
    print(f"NEEDS_CHECK: unexpected error after partial execution — "
          f"do NOT blindly resend; verify manually（{type(exc).__name__}: {exc}）；"
          "输入框可能残留草稿，请清理草稿后再人工发送")
    return EXIT_NEEDS_CHECK


def _load_keys(db_keys_path: Path) -> dict[str, str] | None:
    try:
        obj = json.loads(Path(db_keys_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"REFUSE: 无法读取 db_keys.json（{db_keys_path}）: {exc}")
        return None
    keys = obj.get("keys") if isinstance(obj, dict) else None
    if not isinstance(keys, dict):
        print("REFUSE: db_keys.json 缺少 'keys' 对象")
        return None
    for name, value in keys.items():
        # 64 位 hex 形状前置校验：坏条目在这里拒发（2），而不是深入
        # run_sqlcipher 后以 ValueError traceback 逃逸成 exit 1。
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            print(f"REFUSE: db_keys.json 密钥 {name!r} 非法（须为 64 位 hex 字符串）")
            return None
    return keys


def _precheck(target, paths: Paths, keys: dict[str, str]) -> tuple[int | None, str | None]:
    """返回 (拒发退出码或 None, 消息表名)。绝不吞 SQLCipherError→exit 2 明示。"""
    msg_key = keys.get(Path(paths.message_db).name)
    if msg_key is None:
        print(f"REFUSE: db_keys.json 缺少 {Path(paths.message_db).name} 密钥")
        return EXIT_REFUSE, None
    if target.kind == "group":
        # group 的表标识就是 chatroom id 本身（rule 1），跳过 find_remark。
        return None, expected_table(target.ident)
    if target.wxid:
        # roster 直供 wxid（6b 校准产物形状）：备注查询与 contact.db 全跳过。
        return None, expected_table(target.wxid)
    contact_key = keys.get(Path(paths.contact_db).name)
    if contact_key is None:
        print(f"REFUSE: db_keys.json 缺少 {Path(paths.contact_db).name} 密钥")
        return EXIT_REFUSE, None
    try:
        hits = find_remark(paths.contact_db, contact_key, target.ident)
    except SQLCipherError as exc:
        print(f"REFUSE: 密钥已失效，前置校验无法解密 contact.db: {exc}"
              "（确要跳过全部校验请用 --force）")
        return EXIT_REFUSE, None
    if not hits:
        print(f"REFUSE: 备注 {target.ident!r} 在通讯录中查无此人，无法定位会话表")
        return EXIT_REFUSE, None
    if len(hits) > 1:
        print(f"REFUSE: 备注 {target.ident!r} 命中 {len(hits)} 个 id {hits}，"
              "多义即误发风险，拒绝猜测")
        return EXIT_REFUSE, None
    return None, expected_table(hits[0])


def send(target_key, text, *, paths, roster, calib,
         wechat_running=wechat_running_probe,
         acquire_lock=None, runner=subprocess.run,
         force=False, nowait=False, dry_run=False,
         postcheck_timeout=15.0, postcheck_poll=1.5,
         clock=time.monotonic, sleep=time.sleep) -> int:
    """编排一次安全发送，返回退出码（0/2/3/4，见模块 docstring）。

    acquire_lock: 零参工厂 → 上下文管理器；缺省（None）用 paths.lock 上的
    FileLock；如要完全跳过加锁（仅测试用），注入返回 None 的工厂。
    """
    try:
        target = resolve(roster, target_key)
    except KeyError:
        print(f"REFUSE: 目标 {target_key!r} 不在花名册中")
        return EXIT_REFUSE
    # 只去尾随空白（rstrip）：前导空白是正文的一部分（Review-Focus #1）；
    # 空判定仍按 strip（纯空白即空）。
    body = (text or "").rstrip()
    if not body.strip():
        print("REFUSE: 消息文本为空（strip 后无内容）")
        return EXIT_REFUSE
    # row 模式（6b）：无置顶行坐标即无法点击会话行——先于任何 runner 调用、
    # 也先于 dry-run/--force 拒发；越出校准屏幕高度的 row 必然打偏，同样拒发。
    if target.row is None:
        print(f"REFUSE: 目标 {target.key!r} 缺少 row（置顶会话行 y 坐标），"
              "请先在微信中置顶目标，从新截图量取置顶行的 y 坐标填入 roster "
              "的 row 字段，拒发")
        return EXIT_REFUSE
    if target.row >= calib.screen[1]:
        print(f"REFUSE: 目标 {target.key!r} 的 row={target.row} 超出校准屏幕"
              f"高度 {calib.screen[1]}，行点击必然打偏，拒发")
        return EXIT_REFUSE

    table: str | None = None
    if dry_run:
        # rule 7：dry-run 零 runner 调用、零加锁；但拒发条件照常判定，
        # user 目标的只读前置（find_remark 定表）照做以便打印完整计划。
        if not force:
            keys = _load_keys(paths.db_keys)
            if keys is None:
                return EXIT_REFUSE
            err, table = _precheck(target, paths, keys)
            if err is not None:
                return err
        plan = _plan_steps(calib, target.row, body)
        print("DRY-RUN 计划（零 GUI 注入命令、零加锁；非 --force 时仍执行只读查询定表）:")
        print(f"  目标: {target.key} ({target.kind}) ident={target.ident}")
        print(f"  置顶行 row: {target.row}")
        print(f"  消息表: {table if table is not None else '—（--force 跳过校验）'}")
        print("  阶段序列: " + " → ".join(name for name, _ in plan))
        for name, step_list in plan:
            print(f"    {name}:")
            for argv in step_list:
                print(f"      {argv}")
        return EXIT_OK

    if not wechat_running():
        print("REFUSE: 微信未在运行（/opt/wechat/wechat* 进程不存在），"
              "GUI 步骤必然打偏，拒发")
        return EXIT_REFUSE
    lock_factory = acquire_lock
    if lock_factory is None:
        lock_factory = lambda: FileLock(paths.lock, nowait=nowait,
                                        sleep=sleep, clock=clock)
    try:
        cm = lock_factory()
        with cm if cm is not None else contextlib.nullcontext():
            if force:
                # rule 4：--force 跳过前置与后置，GUI 序列照常，警示不宣称回执。
                print("WARNING: --force 已指定：跳过前置/后置校验，误发保护已禁用，"
                      "本命令不承诺任何数据库回执。")
                plan = _plan_steps(calib, target.row, body)
                fail, exc = _run_plan_with_clear(plan, runner)
                if exc is not None:
                    return _needs_check_unexpected(exc)
                if fail is not None:
                    return _report_step_failure(fail)
                print("WARNING: --force 模式：GUI 序列已执行，未做回执校验")
                return EXIT_OK
            keys = _load_keys(paths.db_keys)
            if keys is None:
                return EXIT_REFUSE
            err, table = _precheck(target, paths, keys)
            if err is not None:
                return err
            # FP#3 全覆盖 + spec「local_id 增长」：锁内、GUI 之前对每种目标
            # 做消息库只读基线探测——坏密钥在此拒发（2，零 GUI 命令），
            # 基线传给 postcheck 作增长要求（表不存在/空表 → None=旧行为）。
            try:
                baseline = latest_local_id(paths.message_db,
                                           keys[Path(paths.message_db).name],
                                           table)
            except SQLCipherError as exc:
                print(f"REFUSE: 密钥已失效，前置基线探测无法解密消息库: {exc}"
                      "（确要跳过全部校验请用 --force）")
                return EXIT_REFUSE
            plan = _plan_steps(calib, target.row, body)
            # rule 2/exit-3：GUI 一旦开始执行，任何失败（非零 rc / 异常）都
            # 不得伪装成可安全重发：send 前截断→2，send 起→3。
            fail, exc = _run_plan_with_clear(plan, runner)
            if exc is not None:
                return _needs_check_unexpected(exc)
            if fail is not None:
                return _report_step_failure(fail)
            try:
                ok = postcheck(paths.message_db,
                               keys[Path(paths.message_db).name],
                               table, body,
                               timeout_s=postcheck_timeout,
                               poll_s=postcheck_poll,
                               clock=clock, sleep=sleep,
                               after_id=baseline)
            except SQLCipherError as exc:
                print(f"NEEDS_CHECK: 后置校验无法解密消息库: {exc}；"
                      "消息可能已发出，请人工核对，切勿盲目重发")
                return EXIT_NEEDS_CHECK
            except Exception as exc:
                return _needs_check_unexpected(exc)
            if ok:
                print(f"RECEIPT: 已在 {table} 最新消息确认落库，发送成功")
                return EXIT_OK
            print(f"NEEDS_CHECK: {table} 轮询 {postcheck_timeout}s 未确认回执；"
                  "不会自动重发，请人工核对微信窗口")
            return EXIT_NEEDS_CHECK
    except LockBusy as exc:
        print(f"REFUSE: {exc}")
        return EXIT_LOCK_BUSY


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="wxsend",
        description="安全地向花名册目标发送微信消息（前置校验→锁→GUI→回执）")
    ap.add_argument("--to", required=True, help="roster 中的目标 key")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="消息文本")
    src.add_argument("--file", help="从 UTF-8 文件读取消息文本")
    ap.add_argument("--roster", help=f"roster.json 路径（默认 {DEFAULT_BASE / 'roster.json'}）")
    ap.add_argument("--calib", help=f"calib.json 路径（默认 {DEFAULT_BASE / 'calib.json'}）")
    ap.add_argument("--force", action="store_true",
                    help="密钥失效等场景降级：跳过前置/后置校验（警示后仍发送）")
    ap.add_argument("--nowait", action="store_true",
                    help="锁被占用时立即以退出码 4 失败，不自旋等待")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印完整计划（目标/表/阶段序列），不执行不加锁")
    args = ap.parse_args(argv)

    base = DEFAULT_BASE
    roster_path = Path(args.roster) if args.roster else base / "roster.json"
    calib_path = Path(args.calib) if args.calib else base / "calib.json"
    try:
        roster = load_roster(roster_path)
    except (OSError, ValueError) as exc:
        print(f"REFUSE: 花名册加载失败（{roster_path}）: {exc}")
        return EXIT_REFUSE
    try:
        calib = load_calib(calib_path)
    except (OSError, ValueError) as exc:
        print(f"REFUSE: 校准文件加载失败（{calib_path}）: {exc}")
        return EXIT_REFUSE
    try:
        target = resolve(roster, args.to)
    except KeyError:
        print(f"REFUSE: 目标 {args.to!r} 不在花名册中")
        return EXIT_REFUSE
    # row 的有无/合法性统一由 send() 判定（含 dry-run），此处不再读 raw JSON。
    if args.text is not None:
        text = args.text
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:  # ValueError 含 UnicodeDecodeError
            print(f"REFUSE: 无法读取消息文件（{args.file}，须为 UTF-8）: {exc}")
            return EXIT_REFUSE
    try:
        paths = build_paths(base)
    except MissingDB as exc:
        print(f"REFUSE: {exc}")
        return EXIT_REFUSE
    return send(args.to, text, paths=paths, roster=roster, calib=calib,
                force=args.force, nowait=args.nowait, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
