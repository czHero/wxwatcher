"""groupwatch 守护主循环：tail→ingest→audit→store→dispatch 一条龙 + digest 排程 + 单实例锁。

游标约定：`"jsonl"` 键 = ingest.jsonl 的消费位点（EOF 偏移）；`"bad_lines"` 键
= 坏行累计计数（持久可见，status 子命令展示）；`"audited:<group>"` 键 = 该群
已发起审计的 inbox 最大 rowid（终审 C2 的恢复水位）。崩溃模型按诚实口径记账：

- 落在 ingest 两句之间：`accept_message` 单事务同生死，重放由 is_seen 兜底；
- 落在攒批中途（批未出）：jsonl 游标已越过它们，**重启由 build_deps 沿
  audited 水位从 inbox 找回、prime 回缓冲**——易失缓冲不再是丢消息的理由；
- 落在审计中途：本批若已 insert_intelligence 落库，重审被 source_hash UNIQUE
  拦下（insert 返回 None → 不产生新 pending 行）；审计尝试一结束水位即前进；
- 落在审计与推送之间：情报行已入库、pushed='pending'，每 tick 的 pending 续拉
  兜住（零丢失）；已标 sent/hold 的行不在 pending 里——**已尝试（尤其 hold）
  的消息永远不会被自动重发**。

审计阶段用 `retryable_audit(retries=1)` 外层再包一次（内层 chat 自带 3 次指数
退避，最坏 2×4=8 请求/批）；AuditFailed 只落 audit_log（audit_failed /
extract_failed），游标照常推进——单批审计失败不得堵死摄取。

游标超出文件实际大小说明 jsonl 被截断/轮转：回 0 重放，并在 result 里留
`"note"`。digest 排程按"到点 且 当日有情报 且 首次 fire"三闸；当日情报为空时
**不占 fire**（下个 tick 情报到了仍能补上当天这一班）。终审 C1 加两闸：
本 tick realtime 推送带回非 ok → digest 整段跳过（slot 不碰）；digest 推送
自身失败 → digest_unfire 还位重试，只有 "ok" 才真正占用 slot。M8 限流：
digest_times 升序遍历，一 tick 至多试火一个 slot（停机恢复日不叠发多条汇总）。

group_names 是内存态映射（cid→群显示名）：build_deps 启动时经 contacts.py
从 contact.db 预载真名（best-effort，失败={}），one_tick 只接受
conversation_name 非空**且不等于裸 cid** 的记录来补——上游 jsonl 的
conversation_name 常与 gid 同值（monitor name2id 缺陷的外溢），不得让它
冲掉 contact 预载名。两者皆无时 render 层的 `or gid` 兜底负责。
"""
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import contacts
import dispatch
import modelclient
from audit import AuditFailed, extract_intel, judge_batch, retryable_audit
from config import (Config, ConfigError, WatchItem, load_config, load_groups,
                    load_watchitems)
from ingest import BatchBuffer, ingest_records, tail_jsonl
from render import render_digest, render_intel
from store import Store, open_store

CURSOR_KEY = "jsonl"
BAD_LINES_KEY = "bad_lines"
AUDITED_KEY_PREFIX = "audited:"  # + group = 已发起审计的 inbox 最大 rowid（C2）
AUDIT_RETRIES = 1  # 外层重试次数；内层 chat 各带 retries=3 指数退避
RECOVER_PAGE_LIMIT = 200  # 重启恢复单页行数
RECOVER_MAX_PAGES = 5  # 恢复上限 = 1000 行/启动；更多残余下次重启续接

_ROLES = ("judge", "extract")


@dataclass
class Deps:
    """one_tick 的全部注入面。sleep 是重试间隔注入位（默认 time.sleep；
    测试传 no-op，失败路径不得真睡）。"""

    cfg: Config
    store: Store
    items: tuple[WatchItem, ...]
    groups: frozenset[str]
    buf: BatchBuffer
    judge_tpl: str
    extract_tpl: str
    chat: Callable
    push: Callable[[list[tuple], str], str]
    now: Callable[[], float]
    group_names: dict[str, str]
    sleep: Callable[[float], None] = time.sleep
    recovered: int = 0  # build_deps 重启恢复进缓冲的 inbox 行数（C2 可观测）
    # 决策协议注入面（accept-fix §4）：build_deps 恒填（真=modelclient 模块
    # 属性、dry=noul 0.0 桩）；直接构造 Deps 的既有 chat 路径测试留 None 无碍
    # ——judge_batch 仅在 role.api=="systemone" 时才触碰它。
    systemone: Callable = None


def one_tick(deps: Deps, flush_all: bool = False) -> dict:
    """跑一整轮：摄取→攒批→审计→入库→实时推送→digest 排程。返回摘要 dict。

    flush_all=True（once 子命令）：把每个群的不足一批残余也整批判掉，不再
    等 batch_max_msgs/batch_max_wait_s；run_forever 恒用默认 False。
    """
    cfg, st = deps.cfg, deps.store
    now = deps.now()
    path = Path(cfg.ingest.jsonl)

    # ---- 截断守卫（游标比文件还大 = jsonl 被截断/轮转）----
    offset = st.cursor_get(CURSOR_KEY)
    cursor_reset = False
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if offset > size:
        st.cursor_set(CURSOR_KEY, 0)
        offset = 0
        cursor_reset = True

    # ---- 摄取：tail 先于 cursor 落库（见模块 docstring 的崩溃窗口排序）----
    tail = tail_jsonl(path, offset)
    if tail.skipped:
        st.cursor_set(BAD_LINES_KEY, st.cursor_get(BAD_LINES_KEY) + tail.skipped)

    accepted = ingest_records(st, deps.groups, tail.records)
    st.cursor_set(CURSOR_KEY, tail.new_offset)

    by_group: dict[str, list[dict]] = {}
    for rec in accepted:
        cid = rec["conversation_id"]
        name = rec.get("conversation_name")
        # 群名只进内存。gid 等值的 conversation_name（上游缺陷外溢）不收录，
        # 以免冲掉 build_deps 从 contact.db 预载的真名。
        if name and name != cid:
            deps.group_names[cid] = name
        by_group.setdefault(cid, []).append(rec)

    batches = []
    for gid, recs in by_group.items():
        batches.extend(deps.buf.add(gid, recs, now))
    batches.extend(deps.buf.flush_stale(now))
    if flush_all:
        batches.extend(deps.buf.drain_all())  # once 语义：残余也判掉（C2）

    # ---- 审计 ----
    item_names = {it.id: it.name for it in deps.items}
    hits_total = new_total = dup_total = 0
    for batch in batches:
        h, n, d = _audit_batch(deps, batch)
        hits_total += h
        new_total += n
        dup_total += d
        _advance_audited(st, batch)  # 尝试即前进（成功或 AuditFailed，C2）

    # ---- 实时推送：每个 tick 都拉 pending（§5.5：lock_busy/pre_refuse-crash
    # 后整队保持 pending，条件恢复即自动续流——续流不依赖新情报到达）。
    # 空拉零成本；pending 只含 pushed='pending' 行，成功即翻 sent，结构上
    # 不可能双推同一条。----
    push_status: str | None = None
    if cfg.push.realtime:
        rows = st.pending(cfg.limits.push_max_per_tick)
        if rows:
            jobs = [(r["id"], render_intel(
                r, item_names.get(r["item_id"]) or r["item_id"],
                deps.group_names.get(r["group"]) or r["group"]))
                for r in rows]
            push_status = deps.push(jobs, "realtime")

    # ---- digest 排程 ----
    # C1 两道闸：
    # ① 本 tick 的 realtime 推送一旦带回非 "ok"（pre_fail / stopped:*），
    #    环境已证不可用，digest 整段跳过——不 try_fire（slot 从未被碰）、
    #    不 unfire，下个 tick 再议；push_status 为 None（realtime 关闭或
    #    本 tick 无 pending）不算触发此闸。
    # ② digest 自身推送失败 ≠ 已发：digest_unfire 还位，让后续 tick 重试；
    #    成功（"ok"）才保留占位。失败那笔 dispatch 流水照记（证据链）。
    realtime_down = push_status is not None and push_status != "ok"
    if cfg.push.digest_enabled and not realtime_down:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        for hhmm in cfg.push.digest_times:
            due = time.mktime(time.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"))
            if now < due:
                continue
            rows = st.day_intels(day)
            if not rows:
                continue  # 空跑不占 fire：当日情报晚到仍能补上这一班
            if not st.try_fire_digest(day, hhmm):
                continue  # 今日该 slot 已推过（前次尝试且推送 ok）
            # I4（§5.5）：累计抑制计数进汇总——counts()["suppressed"] 口径。
            text = render_digest(day, rows, item_names, deps.group_names,
                                 st.counts()["suppressed"])
            if text is None:
                continue
            push_status = deps.push([(None, text)], "digest")
            if push_status != "ok":
                st.digest_unfire(day, hhmm)  # 失败退位，后续 tick 重试（C1）
            break  # M8：一 tick 至多试火一个 slot——成功即占用；失败已退位，
            # 环境坏时不再拿第二个 slot 连撞（catch-up 逐 tick 进行）

    result = {"ingested": len(accepted), "batches": len(batches),
              "hits": hits_total, "intel_new": new_total,
              "intel_dup": dup_total, "skipped": tail.skipped,
              "push": push_status,
              "pre": None if push_status is None
                     else push_status != "pre_fail"}
    if cursor_reset:
        result["note"] = ("游标超出 jsonl 实际大小（文件被截断/轮转），"
                          "已重置为 0 重放；去重键保证零重复")
    return result


def _advance_audited(st: Store, batch) -> None:
    """审计尝试结束（成功或失败皆算）即推进该群 audited 水位到批内最大 inbox
    rowid——恢复只找回"从未发起审计"的行（C2）。双保险：即便重放同批，
    source_hash UNIQUE 也不产生新 pending 行，sent/hold 行不入 pending 续拉。
    手工注入的批（无 rowid，如测试/回放工具）不移动水位——没有坐标可依。
    水位单调：只前进不后退。"""
    rowids = [m["rowid"] for m in batch.msgs if m.get("rowid") is not None]
    if not rowids:
        return
    key = AUDITED_KEY_PREFIX + batch.group
    st.cursor_set(key, max(st.cursor_get(key), max(rowids)))


def _audit_batch(deps: Deps, batch) -> tuple[int, int, int]:
    """单批两阶段审计 + 入库。返回 (hits, intel_new, intel_dup)。

    AuditFailed / extract_failed 走内部专路（同 audit_failed 语义）；I2
    保险带：judge/extract 区域若穿出**任何**其它异常（SDK 内部 RuntimeError、
    模板 KeyError、假件违约……）也只记 audit_failed 返回——游标已推进的
    tick 绝不因单批坏料中断（续推与水位在主循环里）。
    """
    cfg, st = deps.cfg, deps.store
    gid = batch.group
    try:
        return _audit_batch_body(deps, batch, cfg, st, gid)
    except Exception as e:
        st.log_audit(gid, len(batch.msgs), 0, "audit_failed")
        print(f"groupwatchd: {gid} 批审计未预期异常（记 audit_failed，"
              f"游标照常推进）: {type(e).__name__}: {e}", file=sys.stderr)
        return 0, 0, 0


def _audit_batch_body(deps: Deps, batch, cfg, st, gid) -> tuple[int, int, int]:
    first_epoch = int(batch.msgs[0].get("epoch") or 0)
    context = st.recent(gid, before_ts=first_epoch,
                        limit=cfg.limits.context_msgs)

    try:
        hits = retryable_audit(
            lambda: judge_batch(cfg.models["judge"], deps.judge_tpl,
                                deps.items, batch, chat=deps.chat,
                                systemone=deps.systemone,
                                threshold=cfg.limits.judge_threshold),
            retries=AUDIT_RETRIES, sleep=deps.sleep)
    except AuditFailed as e:
        st.log_audit(gid, len(batch.msgs), 0, "audit_failed")
        print(f"groupwatchd: {gid} 批审计失败（游标已推进，不重审）: {e}",
              file=sys.stderr)
        return 0, 0, 0

    if not hits:
        st.log_audit(gid, len(batch.msgs), 0, "ok")
        return 0, 0, 0

    try:
        intels = retryable_audit(
            lambda: extract_intel(cfg.models["extract"], deps.extract_tpl,
                                  deps.items, batch, hits, context,
                                  chat=deps.chat),
            retries=AUDIT_RETRIES, sleep=deps.sleep)
    except AuditFailed as e:
        st.log_audit(gid, len(batch.msgs), len(hits), "extract_failed")
        print(f"groupwatchd: {gid} 批情报提取失败（命中 {len(hits)} 条作废）: {e}",
              file=sys.stderr)
        return len(hits), 0, 0

    new = dup = 0
    for intel in intels:
        row = {"group": gid, "item_id": intel.item_id, "title": intel.title,
               "detail": intel.detail, "deadline": intel.deadline,
               "action": intel.action, "source_keys": intel.source_keys,
               "raw_json": json.dumps(
                   {"item_id": intel.item_id, "title": intel.title,
                    "detail": intel.detail, "deadline": intel.deadline,
                    "action": intel.action, "excerpts": list(intel.excerpts),
                    "source_keys": list(intel.source_keys)},
                   ensure_ascii=False)}
        if st.insert_intelligence(row) is None:
            dup += 1  # source_hash UNIQUE：同批重审兜底
        else:
            new += 1
    st.log_audit(gid, len(batch.msgs), len(hits), "ok")
    return len(hits), new, dup


def _default_chatroom_names() -> dict[str, str]:
    """开机群名预载的缺省路：sys.path 引 monitor 原语 + contacts 查
    contact.db nick_name。best-effort：monitor 目录缺失、无微信数据、key
    不可读/解不开（choose_db_storage 等以 SystemExit 报失败）一律 {}，
    守护路径绝不被群名解析拖崩。dry_run 同样尝试——失败自然为 {}。"""
    try:
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
        return contacts.chatroom_names(monitor, db_storage, all_keys)
    except (Exception, SystemExit) as e:
        print(f"群名预载跳过：{type(e).__name__}", file=sys.stderr)
        return {}


def build_deps(home, *, dry_run: bool = False, chat=None, push=None,
               now: Callable[[], float] = time.time,
               contacts_loader=None) -> Deps:
    """从 home 装配全套依赖。dry_run：chat 恒空 hits、push 只记 dry 流水。

    contacts_loader：群名预载接缝（返回 {cid: 真名} 的可调用）；缺省走
    _default_chatroom_names 真查 contact.db。任何异常吞掉回落 {}——
    测试注入假件即可零触库。
    """
    home = Path(home)
    cfg = load_config(home)
    items = load_watchitems(home)
    groups = load_groups(home)

    # 角色闸门：judge/extract 缺一个就 ConfigError，daemon 路径绝不留裸 KeyError
    missing = [r for r in _ROLES if r not in cfg.models]
    if missing:
        raise ConfigError(
            "config.json 的 models 缺少角色：" + "、".join(missing)
            + "（groupwatchd 需要 judge 与 extract 两个角色）")

    # 模板读 home 副本（cfg.prompts_dir），不是仓库 prompts/——用户改模板
    # 不碰 git；缺文件点名完整路径。
    tpls: dict[str, str] = {}
    for role in _ROLES:
        p = cfg.prompts_dir / f"{role}.md"
        try:
            tpls[role] = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigError(f"提示词模板不存在: {p}（请先跑 groupwatch init）")
        except OSError as e:
            raise ConfigError(f"无法读取提示词模板 {p}: {e.strerror}")

    # 配置闸门全过才碰 db：坏配置不创建半成品 groupwatch.db
    st = open_store(cfg.home / "groupwatch.db")

    if chat is not None:
        chat_fn = chat
    elif dry_run:
        def chat_fn(role, system, user, **kwargs):
            return '{"hits": []}'
    else:
        chat_fn = modelclient.chat

    if dry_run:
        # noul=0.0 恒不命中（< 阈值下限 0.5；阈值配 0 时等号命中属病态配置，
        # dry-run 语义仍由"hits 空 → 零情报"兜住）。answers 键按 questions 现构，
        # 保证 qid 永远对得上，桩自身不可能触发 AuditError。
        def systemone_fn(role, state, questions, **kwargs):
            return {qid: {"type": "noul", "noul": 0.0} for qid in questions}
    else:
        systemone_fn = modelclient.systemone  # 模块属性读取位（测试可打桩）

    if push is not None:
        push_fn = push
    elif dry_run:
        def push_fn(jobs, kind):
            st.log_dispatch(kind, "dry", f"{len(jobs)} 条（dry-run，未外发）")
            return "ok"
    else:
        def push_fn(jobs, kind):
            return dispatch.push_texts(cfg, st, jobs, kind=kind)

    buf = BatchBuffer(cfg.ingest.batch_max_msgs, cfg.ingest.batch_max_wait_s)
    recovered = _recover_buffer(st, groups, buf)
    if recovered:
        print(f"groupwatchd: 从 inbox 恢复 {recovered} 条未审计消息"
              "（audited 水位回放，重启零丢失；原始 epoch 保留）",
              file=sys.stderr)
    # 群名预载（accept-fix-names）：开机即有可读群名，通知不必等 jsonl 喂名
    # （而 jsonl 的 conversation_name 常是裸 gid）。best-effort：任何异常 {}。
    loader = _default_chatroom_names if contacts_loader is None else contacts_loader
    try:
        loaded_names = loader() or {}
    except Exception as e:
        print(f"群名预载跳过：{type(e).__name__}", file=sys.stderr)
        loaded_names = {}
    deps = Deps(cfg, st, items, groups, buf, tpls["judge"], tpls["extract"],
                chat_fn, push_fn, now, dict(loaded_names))
    deps.recovered = recovered
    deps.systemone = systemone_fn
    return deps


def _recover_buffer(st: Store, groups: frozenset[str],
                    buf: BatchBuffer) -> int:
    """重启恢复（终审 C2）：把"已摄取、未审计"的 inbox 行重建成
    ingest_records 同形状的字典（epoch=ts、source_key、conversation_id、
    rowid）prime 进缓冲——**只读不写**，seen/inbox 一概不重插。恢复的记录
    ts 为 int（inbox 不存 monitor 的 ISO 串），审计提示词里时间显示为 epoch
    数字，时序语义不变。分页读，单次启动至多 RECOVER_MAX_PAGES ×
    RECOVER_PAGE_LIMIT 行；更多残余在后续审计推进水位后由下次重启续接。"""
    total = 0
    for gid in sorted(groups):
        watermark = st.cursor_get(AUDITED_KEY_PREFIX + gid)
        recs: list[dict] = []
        for _page in range(RECOVER_MAX_PAGES):
            rows = st.inbox_after(gid, watermark, RECOVER_PAGE_LIMIT)
            if not rows:
                break
            for r in rows:
                recs.append({
                    "db": r["db"], "table": r["table"],
                    "local_id": r["local_id"], "ts": r["ts"], "epoch": r["ts"],
                    "group": gid, "conversation_id": gid,
                    "sender_name": r["sender_name"], "text": r["text"],
                    "source_key": f"{r['db']}/{r['table']}/{r['local_id']}",
                    "rowid": r["rowid"],
                })
            watermark = rows[-1]["rowid"]  # 页游标仅内存态，审计后才持久化
            if len(rows) < RECOVER_PAGE_LIMIT:
                break
        if recs:
            buf.prime(gid, recs, min(r["epoch"] for r in recs))
            total += len(recs)
    return total


def acquire_lock(home) -> int:
    """flock LOCK_EX|LOCK_NB on home/groupwatchd.lock；返回持有的 fd。

    锁的生命周期 = 该 fd 的操作系统生命周期：进程退出自动释放；调用方（daemon
    入口）只要不把 fd close 掉即可，fd 本身是裸 int，不会被 GC"回收"。
    """
    p = Path(home) / "groupwatchd.lock"
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit("另一 groupwatchd 在跑")
    return fd


def run_forever(deps: Deps, *, sleep: Callable[[float], None] = time.sleep,
                ticks: int | None = None) -> int:
    """主循环：每 tick 间隔 poll_interval_s；ticks 限轮数（测试用）。

    单 tick 异常打印一行中文继续下一轮（崩一轮不杀守护）；Ctrl-C 干净退出 0。
    """
    interval = deps.cfg.ingest.poll_interval_s
    count = 0
    try:
        while ticks is None or count < ticks:
            try:
                one_tick(deps)
            except Exception as e:
                print(f"groupwatchd: 本轮异常，跳过继续: {e}", file=sys.stderr)
            count += 1
            if ticks is not None and count >= ticks:
                break
            sleep(interval)
    except KeyboardInterrupt:
        pass
    return 0
