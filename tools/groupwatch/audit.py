"""groupwatch 两阶段 LLM 审计：judge（找命中）→ extract（抽情报）。

模板为 system/user 两段，以 ``\\n===\\n`` 分隔（``split_prompt``）；占位符
``{watchitems}`` ``{group}`` ``{batch}``（extract 另加 ``{hits}`` ``{context}``）
用字面 replace 填充——正文里的示例 JSON 含字面花括号，不能用 str.format。
judge 按 role.api 分双协议：chat=提示词批量判定；systemone=百炼决策模型逐消息
noul 概率判定（≥limits.judge_threshold 算命中）。extract 恒走 chat 不受影响。
校验红线：item_id 必须在关注项列表内；msg_ids 为本批下标（0 起）且不越界；
excerpts 必须逐字出自原文（批次或前文）；title/detail 非空；每个命中项必须
显式给出布尔 relevant（缺失/非 bool 判失败——强迫模型表态），relevant=false
整条丢弃不入库（提取器否决权，accept-fix 2）。
`source_hash` 直接复用 store._source_hash，保证与 intelligence 去重列同口径。
"""
from dataclasses import dataclass
from typing import Any, Callable, Sequence
import re
import time

from config import ModelRole, WatchItem
from ingest import Batch
from modelclient import ModelError, chat, extract_json, systemone
from store import _source_hash as source_hash

__all__ = ["AuditError", "AuditFailed", "Hit", "Intel", "source_hash",
           "split_prompt", "render_judge_prompt", "render_extract_prompt",
           "judge_batch", "extract_intel", "retryable_audit"]

_SEPARATOR = "\n===\n"


class AuditError(Exception):
    """审计输出不合法（模板/JSON/字段校验失败），可被 retryable_audit 重试。"""


class AuditFailed(Exception):
    """retryable_audit 重试耗尽；watchd 捕获后记 audit_failed，不崩溃。"""


@dataclass(frozen=True)
class Hit:
    item_id: str
    msg_ids: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class Intel:
    item_id: str
    title: str
    detail: str
    deadline: str | None
    action: str | None
    excerpts: tuple[str, ...]
    source_keys: tuple[str, ...]


def split_prompt(tpl: str, name: str = "提示词模板") -> tuple[str, str]:
    """按首个 ``\\n===\\n`` 拆 system/user；缺分隔符 → AuditError（中文、点名模板）。"""
    if _SEPARATOR not in tpl:
        # 消息里展示字面 `\n===\n`（单行、与 _SEPARATOR 同源；!r 把真换行
        # 转义成两字符序列，不再手抄反斜杠——抄错一个就碎成多行）。
        raise AuditError(
            f"{name} 缺少 {_SEPARATOR!r} 分隔符，无法拆分 system/user 两段")
    system, user = tpl.split(_SEPARATOR, 1)
    return system, user


def _render_items(items: Sequence[WatchItem]) -> str:
    return "\n".join(f"- [{it.id}] {it.name}: {it.desc}" for it in items)


def _msg_line(prefix: str, msg: dict) -> str:
    ts = msg.get("ts", msg.get("epoch", ""))
    return f"{prefix}{ts}] {msg.get('sender_name', '')}: {msg.get('text', '')}"


def _render_batch(batch: Batch) -> str:
    return "\n".join(_msg_line(f"[#{i}][", msg)
                     for i, msg in enumerate(batch.msgs))


def _render_context(context: Sequence[dict]) -> str:
    if not context:
        return "（无）"
    return "\n".join(_msg_line("[", msg) for msg in context)


def _render_hits(hits: Sequence[Hit]) -> str:
    if not hits:
        return "（无）"
    return "\n".join(f"- item_id={h.item_id} msg_ids={list(h.msg_ids)} "
                     f"reason={h.reason}" for h in hits)


def _fill(text: str, mapping: dict[str, str]) -> str:
    """单遍占位符替换（终审 M1）：一次 alternation 正则扫全程，替换值直接查
    表——**注入的聊天正文里若恰有 "{hits}"/"{context}" 这类字面文本，不会被
    后续键二次展开**（逐键 str.replace 的多遍语义会让用户内容反向污染提示词
    结构）。未知花括号（模板内示例 JSON）原样存活，故仍不能是 str.format。"""
    if not mapping:
        return text
    pattern = "|".join(re.escape("{" + k + "}") for k in mapping)
    return re.sub(pattern, lambda m: mapping[m.group(0)[1:-1]], text)


def _split_and_fill(tpl: str, mapping: dict[str, str],
                    name: str) -> tuple[str, str]:
    system, user = split_prompt(tpl, name)
    return _fill(system, mapping), _fill(user, mapping)


def _judge_mapping(items: Sequence[WatchItem], batch: Batch) -> dict[str, str]:
    return {"watchitems": _render_items(items),
            "group": batch.group,
            "batch": _render_batch(batch)}


def render_judge_prompt(tpl: str, items: Sequence[WatchItem],
                        batch: Batch) -> str:
    """渲染 judge 模板的 user 段（{watchitems}/{group}/{batch}）。"""
    _, user = _split_and_fill(tpl, _judge_mapping(items, batch), "judge")
    return user


def _extract_mapping(items, batch, hits, context) -> dict[str, str]:
    mapping = _judge_mapping(items, batch)
    mapping["hits"] = _render_hits(hits)
    mapping["context"] = _render_context(context)
    return mapping


def render_extract_prompt(tpl: str, items: Sequence[WatchItem], batch: Batch,
                          hits: Sequence[Hit],
                          context: Sequence[dict]) -> str:
    """渲染 extract 模板的 user 段（judge 三件套之外另加 {hits}/{context}）。"""
    _, user = _split_and_fill(tpl, _extract_mapping(items, batch, hits, context),
                               "extract")
    return user


def _call_stage(stage: str, role: ModelRole, tpl: str,
                mapping: dict[str, str], chat: Callable,
                retries: int) -> Any:
    """拆分/填充模板 → chat → extract_json，统一把各类失败归为 AuditError。"""
    system, user = _split_and_fill(tpl, mapping, stage)
    try:
        text = chat(role, system, user, retries=retries)
    except ModelError as e:
        raise AuditError(f"{stage} 阶段模型调用失败: {e}") from e
    try:
        return extract_json(text)
    except ValueError as e:
        raise AuditError(f"{stage} 阶段模型输出不是合法 JSON: {e}") from e


def _require_str(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditError(f"{where} 必须是非空字符串，收到 {value!r}")
    return value


_MAX_NOUL_QUESTIONS = 16  # /systemone 单请求问题上限（决策模型协议）


def _noul_questions(items: Sequence[WatchItem]) -> dict:
    """关注项表 → noul 问题表：qid=item.id，type/criteria 结构不变。

    instructions 取**部分相关**措辞（联机标定 2026-11-25）：整体主题式问法
    下决策模型把"真作业句夹活动通知尾巴"压到 noul 0.1、噪声「测试」反给
    0.72；改"任何部分涉及即 true"后 0.84 / 0.42 / 无关 0.0（3 连跑稳定）。
    """
    return {
        it.id: {
            "type": "noul",
            "instructions": f"这条消息里是否【包含】「{it.name}」的内容"
                            f"（{it.desc}）？只要原文任何部分涉及即答 true，"
                            f"即使消息同时混杂其他话题。",
            "criteria": {
                "true": "消息的某一部分涉及上述事项",
                "false": "整条消息从头到尾没有任何相关内容",
            },
        }
        for it in items
    }


def _judge_noul(role: ModelRole, items: Sequence[WatchItem], batch: Batch,
                systemone_fn: Callable, threshold: float) -> list[Hit]:
    """systemone 判定路径：逐消息一次决策请求，noul 概率 ≥ threshold（含
    边界）即 Hit。ModelError 原样穿透（上游 retryable_audit 整批重投，重判
    幂等、单次仅毫秒级）。"""
    if len(items) > _MAX_NOUL_QUESTIONS:
        raise AuditError(
            f"决策模型单请求问题上限 {_MAX_NOUL_QUESTIONS}，"
            f"当前关注项 {len(items)} 个")
    questions = _noul_questions(items)
    out: list[Hit] = []
    for idx, msg in enumerate(batch.msgs):
        answers = systemone_fn(role, msg.get("text", ""), questions)
        if not isinstance(answers, dict):
            raise AuditError(
                f"决策模型 answers 必须是对象，收到 {type(answers).__name__}")
        for qid in questions:
            ans = answers.get(qid)
            if not isinstance(ans, dict) or "noul" not in ans:
                raise AuditError(
                    f"决策模型回答缺少问题 {qid!r} 的 noul 结构")
            p = ans["noul"]
            # bool 是 int 实例——同 _require_int 纪律点名拒（True 不得伪装成
            # 概率 1.0 造命中）。
            if isinstance(p, bool) or not isinstance(p, (int, float)):
                raise AuditError(
                    f"决策模型问题 {qid!r} 的 noul 必须是数值，收到 {p!r}")
            if p >= threshold:
                out.append(Hit(qid, (idx,), f"决策概率 {p:.2f}"))
    return out


def judge_batch(role: ModelRole, tpl: str, items: Sequence[WatchItem],
                batch: Batch, *, chat: Callable = chat,
                systemone: Callable = systemone,
                retries: int = 3, threshold: float = 0.5) -> list[Hit]:
    """第一阶段：判定批内哪些消息命中哪些关注项。输出严格校验后返回 Hit 列表。

    协议按 role.api 路由（accept-fix 契约 §3）："systemone" 走决策模型逐消息
    noul 判定（threshold 含边界；tpl/retries 不参与）；其余保持原 chat 提示词
    判定——旧路径语义零改动。extract_intel 恒走 chat，不在此列。
    """
    if role.api == "systemone":
        return _judge_noul(role, items, batch, systemone, threshold)
    data = _call_stage("judge", role, tpl, _judge_mapping(items, batch),
                       chat, retries)
    if not isinstance(data, dict) or "hits" not in data:
        raise AuditError(f"judge 输出缺少 'hits' 字段: {type(data).__name__}")
    raw_hits = data["hits"]
    if not isinstance(raw_hits, list):
        raise AuditError("judge 输出的 'hits' 必须是数组")
    valid_ids = {it.id for it in items}
    total = len(batch.msgs)
    out: list[Hit] = []
    for idx, raw in enumerate(raw_hits):
        where = f"hits[{idx}]"
        if not isinstance(raw, dict):
            raise AuditError(f"{where} 必须是对象")
        item_id = _require_str(raw.get("item_id"), f"{where} 的 item_id")
        if item_id not in valid_ids:
            raise AuditError(f"{where} 的 item_id 不在关注项列表中: {item_id!r}")
        msg_ids = raw.get("msg_ids")
        if not isinstance(msg_ids, list) or not msg_ids:
            raise AuditError(f"{where} 的 msg_ids 必须是非空整数数组")
        norm: set[int] = set()
        for m in msg_ids:
            if isinstance(m, bool) or not isinstance(m, int):
                raise AuditError(f"{where} 的 msg_ids 必须全为整数，收到 {m!r}")
            if not 0 <= m < total:
                raise AuditError(
                    f"{where} 的消息下标越界: {m}（本批共 {total} 条，"
                    f"合法范围 0~{total - 1}）")
            norm.add(m)
        reason = _require_str(raw.get("reason"), f"{where} 的 reason")
        out.append(Hit(item_id, tuple(sorted(norm)), reason))
    return out


def _opt_str(value, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditError(f"{where} 必须是字符串或 null，收到 {value!r}")
    return value.strip() or None


def extract_intel(role: ModelRole, tpl: str, items: Sequence[WatchItem],
                  batch: Batch, hits: Sequence[Hit], context: Sequence[dict],
                  *, chat: Callable = chat, retries: int = 3) -> list[Intel]:
    """第二阶段：对每个命中项抽结构化情报；source_keys 由 Hit.msg_ids 映射批次记录。"""
    data = _call_stage("extract", role, tpl,
                       _extract_mapping(items, batch, hits, context),
                       chat, retries)
    if not isinstance(data, dict) or "items" not in data:
        raise AuditError(f"extract 输出缺少 'items' 字段: {type(data).__name__}")
    raw_items = data["items"]
    if not isinstance(raw_items, list):
        raise AuditError("extract 输出的 'items' 必须是数组")

    # 同一 item_id 的多条 Hit 合并 msg_ids（升序去重）。
    hit_ids: dict[str, list[int]] = {}
    for h in hits:
        merged = hit_ids.setdefault(h.item_id, [])
        for m in h.msg_ids:
            if m not in merged:
                merged.append(m)
    for ids in hit_ids.values():
        ids.sort()

    valid_ids = {it.id for it in items}
    # 可引用的「原文」池：本批消息 + 前文参考，逐字校验 excerpts 用。
    originals = [str(m.get("text", "")) for m in batch.msgs]
    originals += [str(m.get("text", "")) for m in context]

    out: list[Intel] = []
    for idx, raw in enumerate(raw_items):
        where = f"items[{idx}]"
        if not isinstance(raw, dict):
            raise AuditError(f"{where} 必须是对象")
        item_id = _require_str(raw.get("item_id"), f"{where} 的 item_id")
        if item_id not in valid_ids:
            raise AuditError(f"{where} 的 item_id 不在关注项列表中: {item_id!r}")
        # 相关性否决（accept-fix 2）：模型必须对每个命中项显式表态 relevant。
        # 键缺失即失败——绝不把"没写"当 true 放行（"测试"误伤硬凑空壳情报的
        # 实况教训）；非严格 bool 同拒；false → 整条丢弃（不产 Intel、不入库、
        # 不推送），同批其他 true 项照常处理。
        if "relevant" not in raw:
            raise AuditError(
                f"{where} 缺少 relevant 字段（每个命中项必须显式给出 "
                f"true/false——原文无实质可用信息就置 false，不许硬凑）")
        relevant = raw.get("relevant")
        if not isinstance(relevant, bool):
            raise AuditError(
                f"{where} 的 relevant 必须是布尔值 true/false，收到 "
                f"{relevant!r}")
        if not relevant:
            continue
        msg_ids = hit_ids.get(item_id)
        if msg_ids is None:
            raise AuditError(f"{where} 的 item_id 没有对应的 judge 命中: {item_id!r}")
        title = _require_str(raw.get("title"), f"{where} 的 title")
        detail = _require_str(raw.get("detail"), f"{where} 的 detail")
        deadline = _opt_str(raw.get("deadline"), f"{where} 的 deadline")
        action = _opt_str(raw.get("action"), f"{where} 的 action")
        excerpts: list[str] = []
        raw_ex = raw.get("excerpts", [])
        if not isinstance(raw_ex, list):
            raise AuditError(f"{where} 的 excerpts 必须是数组")
        for ex in raw_ex:
            excerpt = _require_str(ex, f"{where} 的 excerpts 每项")
            if not any(excerpt in text for text in originals):
                raise AuditError(
                    f"{where} 的 excerpt 未逐字摘自原文: {excerpt[:40]!r}")
            excerpts.append(excerpt)
        source_keys: list[str] = []
        for m in msg_ids:
            # hits 若不经 judge_batch 直接构造，下标可能越界——此处兜底校验。
            if not 0 <= m < len(batch.msgs):
                raise AuditError(
                    f"{where}: 命中下标越界 {m}（本批共 {len(batch.msgs)} 条）")
            key = batch.msgs[m].get("source_key")
            if not isinstance(key, str) or not key:
                raise AuditError(
                    f"{where}: 批次记录 [{m}] 缺少 source_key（Task 3 契约破坏）")
            source_keys.append(key)
        out.append(Intel(item_id, title, detail, deadline, action,
                         tuple(excerpts), tuple(source_keys)))
    return out


def retryable_audit(fn: Callable[[], Any], *, retries: int,
                    sleep: Callable[[float], None] = time.sleep) -> Any:
    """对 ModelError/AuditError/ValueError 重试；约定与 chat 一致：共调 retries+1 次。

    耗尽抛 AuditFailed（watchd 捕获记 audit_failed）；其余异常原样穿透。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except (ModelError, AuditError, ValueError) as e:
            last = e
            if attempt < retries:
                sleep(1.0)
    raise AuditFailed(
        f"审计重试 {retries} 次后仍失败（最后错误: {last}）") from last
