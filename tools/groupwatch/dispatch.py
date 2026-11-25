"""groupwatch 推送安全层：前台预检闸门 + wxsend 退出码状态机 + 批次策略。

安全合同（Review Focus 5，"never blind-resend"）在结构上落于两处：

1. 每批只跑一次 `foreground_ok` 预检；不过 → `log_dispatch(kind,"pre_fail",…)`
   后直接返回，**零** wxsend 调用（闸门在 job 循环之前，无法绕过）。
   baseline 缺失同理：`push.require_enroll`（默认 True）下按 pre_fail 处置
   ——未跑 enroll 绝不盲发；显式设 False 才恢复旧"放行带警告"（accept-fix 2）。
2. 一切"GUI 已可能执行"的歧义结局（needs_check / gui_partial / 超时）都走
   `mark_status(id,"hold")` 并**立即中止本批**——hold 路径的唯一出口是 return，
   自动重发在本模块内不可达；只有 sent / pre_refuse / crash / lock_busy 四种
   结局不产生 hold，其中前两种才动重试点位（`bump_retry`，达
   `limits.max_retries` → suppressed），lock_busy 明确**不** bump。

wxsend 事实（tools/wechat-send/wxsend.py，只读核对）：
- 退出常量 :61-64 —— 0 OK / 2 REFUSE / 3 NEEDS_CHECK / 4 LOCK_BUSY；
  锁等待硬编码 120s 不可配置，默认等待模式下 4 只在超时后出现。
- REFUSE/NEEDS_CHECK 行打印到 **stdout**（:258 起），`classify_wxsend` 因此
  吃 stdout+stderr 合并文本以防后续改道。
- rc=2 且含 `"REFUSE: aborted at step"` 表示 GUI 已执行过若干步（但发送键
  未按下）→ gui_partial：状态部分已知，仍按歧义处理 hold，绝不自动重试。

`subprocess timeout=420` ≫ 锁等待 120s：真 TimeoutExpired 意味着 wxsend 早已
越过锁阶段、深入 GUI → 归入 needs_check(hold)，不当 crash（crash 会被自动
重试，超时后恰恰不可信）。

预检点位 `PROBE_POINTS` 为 1440×900 全屏微信标定坐标；enroll（后续任务）据此
生成 `home/probe_points.json`（`[{"name","x","y","r","g","b","tol"}]`）。逐点判定
口径：逐通道 `abs(cur-cached) <= tol` 全真才算通过（边界含等号）。

`push_max_per_tick` 由调用方以 `store.pending(cfg.limits.push_max_per_tick)`
把守；`push_texts` 处理且只处理交进来的 jobs。jobs 每项
`(intel_id|None, text)`，digest 批 id 为 None（无 intelligence 行可迁移，只记
dispatch 流水）。文本文件**身份命名**（终审 I3）：`home/out/realtime-<id>.txt` /
`digest-<YYYYMMDD-HHMM>.txt`（目录 0700、文件 0600，先于 wxsend 启动落盘）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from PIL import Image

# 1440×900 全屏微信坐标系（已标定）。
PROBE_POINTS = (
    {"name": "标题带", "x": 60, "y": 40},
    {"name": "侧栏", "x": 60, "y": 500},
    {"name": "输入区", "x": 700, "y": 880},
    {"name": "发送钮", "x": 1391, "y": 866},
)


def pixel_probe(png, x: int, y: int) -> tuple[int, int, int]:
    """读 png 在 (x,y) 处的 RGB 三元组。"""
    with Image.open(str(png)) as im:
        r, g, b = im.convert("RGB").getpixel((x, y))[:3]
    return (int(r), int(g), int(b))


def load_probe_baseline(home) -> list[dict] | None:
    """读 home/probe_points.json；文件缺失 → None（未配置）。"""
    p = Path(home) / "probe_points.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, list):
        raise ValueError(f"probe_points.json 顶层必须是数组，收到 {type(data).__name__}")
    return data


def default_shot(png) -> None:
    """真截图：gnome-screenshot 覆盖写 png；失败/超时由调用方按截图失败处理。"""
    subprocess.run(["gnome-screenshot", "-f", str(png)],
                   capture_output=True, timeout=15, check=True)


def foreground_ok(home, *, shot: Callable[[Path], None] = default_shot,
                  now: float = time.time) -> tuple[bool, str]:
    """一次前台预检。baseline 缺失 → 放行带警告（不阻塞）；截图失败 →
    (False,"截图失败")；逐点逐通道 abs≤tol 全真 → (True,"ok")，否则报点名。
    `now` 为接口位（预检时刻占位，当前实现不消费）。"""
    baseline = load_probe_baseline(Path(home))
    if baseline is None:
        return (True, "预检未配置，跳过（建议跑 groupwatch enroll）")
    png = Path(home) / "probe.png"
    try:
        shot(png)
    except Exception:
        return (False, "截图失败")
    if not png.exists():  # shot 声称成功但没落盘 → 不放行
        return (False, "截图失败")
    for pt in baseline:
        try:
            cur = pixel_probe(png, pt["x"], pt["y"])
        except Exception:
            return (False, "截图失败")
        if not all(abs(c - e) <= pt["tol"]
                   for c, e in zip(cur, (pt["r"], pt["g"], pt["b"]))):
            return (False, f"点位 {pt['name']} 不匹配")
    return (True, "ok")


def classify_wxsend(rc: int, output: str) -> str:
    """wxsend 退出码状态机（六路）。output = stdout+stderr 合并文本。"""
    if rc == 0:
        return "sent"
    if rc == 2:
        # aborted-at-step 型 REFUSE：GUI 已跑过若干步（wxsend.py:258）
        if "REFUSE: aborted at step" in output:
            return "gui_partial"
        return "pre_refuse"
    if rc == 3:
        return "needs_check"
    if rc == 4:
        return "lock_busy"
    # 负数 rc = 子进程被信号杀死（OOM killer / SIGKILL 等）：wxsend 可能死在
    # 按下发送键之后、NEEDS_CHECK 打印之前——输出截断、真实状态未知。未知一律
    # needs_check（→hold，绝不自动重试），不得降级为 crash（crash 会计重试点、
    # 后续 tick 自动重发，即 blind-resend）。审查裁决 2026-11-24。
    # 正数未知码（解释器自身炸了、没跑到 GUI）与 rc 缺失仍按 crash。
    if isinstance(rc, int) and rc < 0:
        return "needs_check"
    return "crash"


def _write_0600(path: Path, text: str) -> None:
    """O_WRONLY|O_CREAT|O_TRUNC + 显式 chmod：os.open 的 mode 会被 umask
    削位，落盘后 chmod 兜底保证恰 0600，且先于 wxsend 启动完成。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(str(path), 0o600)


def _classify_outcome(cfg, store, kind: str, oid, cls: str,
                      detail: str) -> str | None:
    """单条迁移。返回 None=继续本批；返回 stopped:<status>=中止本批。
    hold（gui_partial/needs_check）路径只有 mark+log+return，绝无重试点位、
    绝无继续发送的可能——'绝不自动重发' 在此函数结构上成立。"""
    if cls == "sent":
        if oid is not None:
            store.mark_status(oid, "sent")
        store.log_dispatch(kind, "ok", detail)
        return None
    if cls in ("gui_partial", "needs_check"):
        if oid is not None:
            store.mark_status(oid, "hold")
        store.log_dispatch(kind, cls, detail)
        return f"stopped:{cls}"
    if cls == "lock_busy":
        # 保持 pending、不 bump_retry（锁竞争不是本条情报的错）
        store.log_dispatch(kind, "lock_busy", detail)
        return "stopped:lock_busy"
    # pre_refuse / crash：计额度；新值 ≥ max_retries → suppressed，否则 pending
    if oid is not None:
        new_retries = store.bump_retry(oid)
        if new_retries >= cfg.limits.max_retries:
            store.mark_status(oid, "suppressed")
            store.log_dispatch(kind, "suppressed", detail)
        else:
            store.log_dispatch(kind, cls, detail)
    else:
        store.log_dispatch(kind, cls, detail)
    return f"stopped:{cls}"


def _out_name(oid, kind: str, i: int, now) -> str:
    """out/ 文件身份命名（终审 I3）：realtime 按 intel 行 id 命名
    `realtime-<oid>.txt`——hold 行的恢复食谱按 id 直接对上文本，不再有跨批
    失义的位置序名（旧 `1.txt`/`2.txt` 废除）。digest 批（oid None，
    kind="digest"）→ `digest-<YYYYMMDD-HHMM>.txt`（本机时刻，取自 now；
    同刻多条时 -2/-3 后缀防互相覆盖）。其余形态（理论不可达的防御分支）
    回落 `<kind>-<序号>.txt`。"""
    if oid is not None:
        return f"realtime-{oid}.txt"
    if kind == "digest":
        ts = now() if callable(now) else now
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(ts))
        return f"digest-{stamp}.txt" if i == 1 else f"digest-{stamp}-{i}.txt"
    return f"{kind}-{i}.txt"


def _pre_fail(store, kind: str, reason: str) -> str:
    """M5：pre_fail 是状态不是事件——连续被挡只记首笔（遮挡/锁屏时段每
    tick 一行会灌爆 dispatches/journal）；状态真变化后重新记。所有
    pre_fail 出口共用此路（真预检不过 / require_enroll 拒绝盲发）。"""
    last = store.recent_dispatches(1)
    if not (last and last[0]["status"] == "pre_fail"):
        store.log_dispatch(kind, "pre_fail", reason)
    return "pre_fail"


def push_texts(cfg, store, jobs: list[tuple], *, kind: str,
               shot: Callable[[Path], None] = default_shot,
               run=subprocess.run, now=time.time) -> str:
    """推送一批文本。返回 ok | pre_fail | stopped:<status>。
    批次策略：预检一次；sent 继续；其余结局一律中止本批（环境坏了别连撞）。"""
    ok, reason = foreground_ok(cfg.home, shot=shot, now=now)
    if ok and reason != "ok":
        # require_enroll 闸门（accept-fix 2）：baseline 缺失＝从未 enroll。
        # 生产默认 True——与 pre_fail 完全同构处置（零 wxsend），实况教训是
        # "测试"句盲发撞 rc3。False 才走旧"放行带警告"。决策在此处（看得见
        # cfg），foreground_ok 只观察不改。
        if getattr(cfg.push, "require_enroll", True):
            return _pre_fail(
                store, kind,
                "未 enroll：拒绝盲发（跑 groupwatch enroll，"
                "或设 push.require_enroll=false 自担风险）")
        # M6：baseline 缺失的"放行带警告"不再静默——每个因此放行的 push 调用
        # 往守护日志打一行（生产 journalctl 可见"预检被跳过"这件事）。
        print(f"groupwatchd: {reason}", file=sys.stderr)
    if not ok:
        return _pre_fail(store, kind, reason)  # ← 零 wxsend 的前置出口族

    out = Path(cfg.home) / "out"
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(str(out), 0o700)

    for i, (oid, text) in enumerate(jobs, 1):
        if not text:
            # crash 分支必返回非空 stopped 串：直接透传中止
            return _classify_outcome(cfg, store, kind, oid, "crash",
                                     f"#{i} 空文本 job，拒发（按 crash 计额度）")
        path = out / _out_name(oid, kind, i, now)
        try:
            _write_0600(path, text)
        except OSError as e:
            return _classify_outcome(cfg, store, kind, oid, "crash",
                                     f"#{i} 写 {path.name} 失败: {e}")
        argv = [sys.executable, str(cfg.push.wxsend_py), "--to",
                cfg.push.target_roster_key, "--file", str(path)]
        try:
            proc = run(argv, capture_output=True, text=True, timeout=420)
        except subprocess.TimeoutExpired:
            # 420s ≫ wxsend 内部 120s 锁等待：能超时说明已深入 GUI，状态歧义
            # → 按 needs_check hold，绝不降级成可自动重试的 crash。
            return _classify_outcome(cfg, store, kind, oid, "needs_check",
                                     f"#{i} wxsend 超时>420s，状态歧义，hold 待人工")
        except OSError as e:
            return _classify_outcome(cfg, store, kind, oid, "crash",
                                     f"#{i} wxsend 无法启动: {e}")
        rc = getattr(proc, "rc", None)
        if rc is None:
            rc = getattr(proc, "returncode", None)
        output = ((getattr(proc, "stdout", None) or "")
                  + (getattr(proc, "stderr", None) or ""))
        cls = classify_wxsend(rc, output)
        stop = _classify_outcome(cfg, store, kind, oid, cls,
                                 f"#{i} id={oid} rc={rc} {output.strip()[:200]}")
        if stop:
            return stop
    return "ok"
