"""groupwatch 命令行入口面（init / groups / enroll / status / test-model / once / run）。

约定：
- 一切副作用只落在运行时根 `home`（`--home` 或 env `GROUPWATCH_HOME`，默认
  `~/.groupwatch`）；home 目录 0700，其下文件一律 0600（含 config/prompts/keys）。
- 面向用户的文案全中文；子命令退出码只有 0（成功/信息性报告）与 2（用法/上游
  错误）。**永不退 1**：意外异常在 `main` 顶层统一转成中文一行 + 退 2。
- 可测性接缝（模块级属性，测试用 mock.patch.object 替换）：
  `_list_groups_impl(home)`（monitor 群发现）、`_screenshot(png, *, run)`
  （gnome-screenshot）；其余走被依赖模块的属性（`dispatch.pixel_probe`、
  `modelclient.chat`、`watchd.build_deps/acquire_lock/one_tick/run_forever`），
  均以 `模块.属性` 形式调用以便打桩。

`status` 是诊断命令：它报告而非校验——坏配置打印 ConfigError 消息后仍退 0。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import config
import contacts
import dispatch
import modelclient
import store
import watchd

# init 产物模板：占位符只在"值"里（base_url），结构一律合法，令 init 产物
# 立即可过 load_config；真调用（test-model）对占位 key 会失败退 2，语义正确。
_REPO_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_KEY_PLACEHOLDER = "FILL_ME_在此粘贴百炼APIKey"
_PROBE_SYSTEM = "你是链路探针。"
_PROBE_USER = "回复 OK"
# 决策协议（/systemone）探针：一问一答看概率，验证 key/端点/模型名三件事。
_PROBE_SYSONE_STATE = "测试"
_PROBE_SYSONE_QUESTIONS = {
    "ping": {"type": "noul", "instructions": "是否命中",
             "criteria": {"true": "是", "false": "否"}},
}
_JSONL_STALE_S = 300  # jsonl mtime 距今超过此秒数 → 疑似监听端停跑

_INIT_CONFIG = {
    "models": {
        # api：调用协议，"chat"（/chat/completions，缺省）或 "systemone"
        # （百炼决策模型 /systemone；decision-model-preview 只支持它）。
        # 模板一律给 "chat"——结构可见即旋钮可达，用决策模型的角色自己改。
        "judge": {"base_url": "<在此填百炼 base_url>",
                  "model": "decision-model-preview",
                  "api_key_file": "judge.key", "api": "chat"},
        "extract": {"base_url": "<在此填百炼 base_url>",
                    "model": "qwen-max",
                    "api_key_file": "extract.key", "api": "chat"},
    },
    "ingest": {"jsonl": "~/.wechat-monitor/messages.jsonl",
               "poll_interval_s": 5, "batch_max_msgs": 40,
               "batch_max_wait_s": 60},
    "push": {"target_roster_key": "filehelper", "realtime": True,
             # accept-fix 2：未跑 enroll（无预检基线）拒绝盲发；确认要旧
             # "放行带警告"行为再自行设 false 自担风险
             "require_enroll": True,
             # M7：默认对齐设计 §5.5 的"一晚一条"示例（20:30）；要多班自己加
             "digest": {"enabled": True, "times": ["20:30"]}},
    "limits": {"max_retries": 3, "push_max_per_tick": 10,
               "inbox_retention_days": 14,
               # systemone 判定的 noul 命中阈值（≥ 算命中）；chat 判定忽略
               "judge_threshold": 0.5},
}
_INIT_WATCHITEMS = [
    {"id": "homework", "name": "作业",
     "desc": "孩子各科作业的提交内容与截止时间，以及老师的具体要求"},
]


def _default_home() -> Path:
    return Path(os.environ.get("GROUPWATCH_HOME", "~/.groupwatch")).expanduser()


def _write_0600(path, text) -> None:
    """写文本并显式 chmod 0600（os.open mode 会被 umask 削位，落盘后补一次）。"""
    path = Path(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(str(path), 0o600)


# ---- 可注入接缝 ----

def _screenshot(png, *, run=subprocess.run) -> None:
    """真截图：gnome-screenshot 覆盖写 png。测试替换本函数即可脱离子进程。"""
    run(["gnome-screenshot", "-f", str(png)],
        capture_output=True, timeout=15, check=True)


def _list_groups_impl(home):
    """借 sys.path 引 wechat-monitor 的 monitor，列出全部群 (id, 显示名)。

    只读本地库文件，**不要求 monitor 进程在跑**。上游失败（库目录不存在、
    keys/passphrase 读不到或解不开密文等）都以异常/`SystemExit` 抛出，由
    `_cmd_groups` 兜成中文提示 + 退 2。测试整体打桩本函数。

    显示名三级来源：contact.db 的 nick_name（contacts.chatroom_names，绕开
    monitor load_maps 的 name2id first-wins 缺陷）> load_maps 的 name > 裸 gid。
    """
    monitor_dir = Path(__file__).resolve().parents[1] / "wechat-monitor"
    sys.path.insert(0, str(monitor_dir))
    import monitor
    db_storage = monitor.choose_db_storage(None, None)
    dbs = monitor.discover_message_dbs(db_storage)
    keys, _src = monitor.load_key_material(db_storage, dbs)
    aux = [db_storage / "session" / "session.db",
           db_storage / "contact" / "contact.db"]
    all_keys = dict(keys)
    all_keys.update(monitor.load_aux_keys(aux))
    groups, _contacts, _sources = monitor.load_maps(db_storage, all_keys)
    names = contacts.chatroom_names(monitor, db_storage, all_keys)
    return [(gid, names.get(gid) or info.get("name") or gid)
            for gid, info in groups.items()]


# ---- 子命令 ----

def _cmd_init(home, args) -> int:
    home = Path(home)
    targets = [home / "config.json", home / "watchitems.json",
               home / "groups.json", home / "judge.key", home / "extract.key",
               home / "prompts" / "judge.md", home / "prompts" / "extract.md",
               home / "prompts" / "digest.md"]
    existing = [p for p in targets if p.exists()]
    if existing:
        for p in existing:
            print(f"拒绝覆盖已存在文件: {p}")
        print("（init 不做破坏性重置；如需重来请先删除上述文件后重跑）")
        return 2

    config.ensure_home(home)
    prompts_dir = home / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(prompts_dir), 0o700)

    _write_0600(home / "config.json",
                json.dumps(_INIT_CONFIG, ensure_ascii=False, indent=2))
    _write_0600(home / "watchitems.json",
                json.dumps(_INIT_WATCHITEMS, ensure_ascii=False, indent=2))
    _write_0600(home / "groups.json",
                json.dumps({"subscribe": []}, ensure_ascii=False, indent=2))
    _write_0600(home / "judge.key", _KEY_PLACEHOLDER)
    _write_0600(home / "extract.key", _KEY_PLACEHOLDER)

    for md in ("judge.md", "extract.md", "digest.md"):
        src = _REPO_PROMPTS_DIR / md
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as e:
            print(f"拷贝提示词模板失败 {md}: {e}")
            return 2
        _write_0600(prompts_dir / md, text)

    print(f"已在 {home} 建立运行环境（0700），写入占位符配置与三件套提示词。")
    print("下一步：")
    print("  1) 编辑 config.json 把 <在此填…> 换成真实 base_url；按需改 jsonl/推送设置；")
    print("  2) 把 judge.key / extract.key 里的 FILL_ME 换成真实百炼 APIKey；")
    print("  3) 跑 groupwatch groups 选群、编辑 groups.json 的 subscribe；")
    print("  4) 跑 groupwatch test-model judge 验链路，再 groupwatch enroll，最后 groupwatch run。")
    return 0


def _cmd_groups(home, args) -> int:
    home = Path(home)
    try:
        rows = _list_groups_impl(home)
    except (Exception, SystemExit) as e:
        print("读取群列表失败：请确认微信本地数据库存在、keys/passphrase "
              "可读可解密后重试（本命令只读库，不需要监听端进程在跑）"
              f"（错误: {type(e).__name__}: {e}）")
        return 2
    try:
        subscribed = config.load_groups(home)
    except config.ConfigError as e:
        print(f"（groups.json 暂不可读，全部按未订阅显示: {e}）")
        subscribed = frozenset()
    if not rows:
        print("未发现任何群（监听端是否已抓到消息？）")
        return 0
    for gid, name in rows:
        mark = "订阅" if gid in subscribed else "未订阅"
        print(f"[{mark}] {name} — {gid}")
    return 0


def _cmd_enroll(home, args) -> int:
    home = Path(home)
    png = home / "enroll.png"
    if not args.yes:
        if not sys.stdin.isatty():
            print("非交互环境：请确认微信窗口在前台且无遮挡后，加 --yes 重跑 enroll")
            return 2
        ans = input("请确认微信在前台、无遮挡、非最小化，开始截图标定 (输入 yes 继续): ")
        if ans.strip().lower() not in ("yes", "y"):
            print("已取消 enroll")
            return 0
    try:
        _screenshot(png)
    except (Exception, SystemExit) as e:
        print(f"截图失败（gnome-screenshot 不可用？）: {type(e).__name__}: {e}")
        return 2
    if not png.exists():
        print("截图未生成，请检查 gnome-screenshot 与目录权限")
        return 2
    points = []
    try:
        for pt in dispatch.PROBE_POINTS:
            r, g, b = dispatch.pixel_probe(png, pt["x"], pt["y"])
            points.append({"name": pt["name"], "x": pt["x"], "y": pt["y"],
                           "r": r, "g": g, "b": b, "tol": 28})
    except (Exception,) as e:
        print(f"像素采样失败: {type(e).__name__}: {e}")
        return 2
    out = home / "probe_points.json"
    _write_0600(out, json.dumps(points, ensure_ascii=False, indent=2))
    print(f"已写入 {out}（{len(points)} 个预检点位，tol=28）。")
    return 0


def _cmd_status(home, args) -> int:
    home = Path(home)
    try:
        cfg = config.load_config(home)
    except config.ConfigError as e:
        print(f"配置无法加载（status 仅报告不修复）: {e}")
        return 0
    st = store.open_store(cfg.home / "groupwatch.db")
    c = st.counts()
    off = st.cursor_get(watchd.CURSOR_KEY)
    bad = st.cursor_get(watchd.BAD_LINES_KEY)

    print(f"home: {cfg.home}")
    print(f"计数: 收件箱 {c['inbox']} / 情报 {c['intelligence']} / "
          f"待推 {c['pending']} / 搁置 {c['hold']} / 抑制 {c['suppressed']} / "
          f"流水 {c['dispatches']}")
    print(f"游标: {watchd.CURSOR_KEY} 偏移 {off} / "
          f"{watchd.BAD_LINES_KEY} {bad}")

    j = Path(cfg.ingest.jsonl)
    try:
        age = time.time() - j.stat().st_mtime
        if age > _JSONL_STALE_S:
            print(f"jsonl: {j} 最后更新 {int(age)} 秒前  ⚠ 监听端疑似停跑")
        else:
            print(f"jsonl: {j} 最后更新 {int(age)} 秒前")
    except OSError:
        print(f"jsonl: {j} 尚不存在（监听端还没产生消息，或路径未配好）")

    disp = st.recent_dispatches(8)
    print("最近推送（至多 8 条）:")
    if not disp:
        print("  （无）")
    for d in disp:
        print(f"  #{d['id']} [{d['kind']}] {d['status']} · {d['detail']}")

    pend = st.pending(20)
    print(f"待推明细（{len(pend)} 条）:")
    for r in pend:
        print(f"  id={r['id']} 群={r['group']} {r['title']}")

    today = time.strftime("%Y-%m-%d", time.localtime())
    dayrows = st.day_intels(today)
    holds = [r for r in dayrows if r["pushed"] == "hold"]
    supps = [r for r in dayrows if r["pushed"] == "suppressed"]
    print(f"今日搁置 {len(holds)} 条 / 今日抑制 {len(supps)} 条:")
    for r in holds + supps:
        print(f"  id={r['id']} [{r['pushed']}] 群={r['group']} {r['title']}")
    return 0


def _cmd_test_model(home, args) -> int:
    home = Path(home)
    which = args.role
    try:
        cfg = config.load_config(home)
        if which not in cfg.models:
            raise config.ConfigError(
                f"config.json 的 models 缺少角色 '{which}'")
        role = cfg.models[which]
        if role.api == "systemone":
            # 决策协议探针：无文本生成，直接打印 answers 概率（紧凑 JSON，
            # 只含概率不含 key）。
            answers = modelclient.systemone(role, _PROBE_SYSONE_STATE,
                                            dict(_PROBE_SYSONE_QUESTIONS))
            print(f"模型 {which} 正常（决策协议 /systemone），answers：")
            print(json.dumps(answers, ensure_ascii=False,
                             separators=(",", ":")))
            return 0
        resp = modelclient.chat(role, _PROBE_SYSTEM, _PROBE_USER)
    except (config.ConfigError, modelclient.ModelError) as e:
        print(f"模型 {which} 链路探针失败: {e}")
        return 2
    print(f"模型 {which} 正常，响应前 200 字：")
    print(resp[:200])
    return 0


def _acquire_lock_or_exit(home):
    try:
        return watchd.acquire_lock(home)
    except SystemExit as e:
        print(f"无法获取单实例锁（另一 groupwatchd 在跑？）: {e}")
        return None


def _cmd_once(home, args) -> int:
    deps = _build_or_exit(home, args)
    if deps is None:
        return 2
    # flock 生命周期 = fd 生命周期 = 进程生命周期（裸 int 出作用域不会自动
    # close、锁不随函数返回释放）。once 进程跑完即退，锁随退出解开——语义正确。
    fd = _acquire_lock_or_exit(home)
    if fd is None:
        return 2
    # once 的语义是"这一轮看到的全部判完"：flush_all 连不足一批的残余也送审
    # （C2；run 常驻仍按攒批参数走）。
    result = watchd.one_tick(deps, flush_all=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_run(home, args) -> int:
    deps = _build_or_exit(home, args)
    if deps is None:
        return 2
    # 常驻期间 fd 一直绑在作用域里（flock 实际持到进程退出）：杜绝并发实例。
    fd = _acquire_lock_or_exit(home)
    if fd is None:
        return 2
    return watchd.run_forever(deps)


def _build_or_exit(home, args):
    try:
        return watchd.build_deps(home, dry_run=args.dry_run)
    except config.ConfigError as e:
        print(f"依赖装配失败: {e}")
        return None


# ---- 解析器与顶层 ----

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="groupwatch", description="微信群情报监听守护（只读监听 + 推送）")
    parser.add_argument("--home", help="运行时根目录（默认 $GROUPWATCH_HOME 或 ~/.groupwatch）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="在 home 建立带占位符的运行环境")
    sub.add_parser("groups", help="列出监听端发现的所有群并标注是否已订阅")

    p_enroll = sub.add_parser("enroll", help="截图标定前台预检点位，写 probe_points.json")
    p_enroll.add_argument("--yes", action="store_true", help="跳过交互确认")

    sub.add_parser("status", help="打印计数/游标/jsonl 新鲜度/推送流水/待推明细")

    p_tm = sub.add_parser("test-model", help="对真实配置发一条链路探针")
    p_tm.add_argument("role", choices=["judge", "extract"])

    p_once = sub.add_parser("once", help="跑一轮 tick 并打印结果 JSON")
    p_once.add_argument("--dry-run", action="store_true", help="不外发、不联网")

    p_run = sub.add_parser("run", help="常驻主循环（Ctrl-C 退出）")
    p_run.add_argument("--dry-run", action="store_true", help="不外发、不联网")
    return parser


_HANDLERS = {
    "init": _cmd_init, "groups": _cmd_groups, "enroll": _cmd_enroll,
    "status": _cmd_status, "test-model": _cmd_test_model,
    "once": _cmd_once, "run": _cmd_run,
}


def main(argv=None, *, home=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if home is not None:
        resolved = Path(home)
    elif args.home:
        resolved = Path(args.home).expanduser()
    else:
        resolved = _default_home()
    try:
        return _HANDLERS[args.cmd](resolved, args)
    except Exception as e:  # 顶层兜底：意外异常 → 中文一行 + 退 2，绝不退 1
        print(f"groupwatch: 未预期错误: {type(e).__name__}: {e}")
        return 2
