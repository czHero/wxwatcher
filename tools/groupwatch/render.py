"""groupwatch 纯文本渲染（实时情报提醒 / 每日汇总），无外部依赖。

`render_intel`：三行式（deadline 非空则四行）——`【item · group】title` /
detail 正文原样（可多行）/ 可选 `截止：…` / `来源：{group_name} · HH:MM`，
ts 按本机时区转 `%H:%M`。row 来自 store.intelligence，可选键缺失一律容错。

`render_digest`：`{day} 汇总` 首行 + 当日条目按关注项分组（组标题
`【item_name】`，组序=该组最早条目在 ts 序上的首次出现；组内条目按 ts 升序，
每行 `  HH:MM 群名 · title`）；空 rows → None。名称映射由调用方传入：item
缺失回落 `未知项`，群缺失回落群 id 本身。`suppressed_count>0` 时追加尾行
"另有累计 N 条情报被抑制（重试耗尽）…"（§5.5，终审 I4；计数为全表累计口径）。
"""
import time


def _hhmm(ts) -> str:
    """epoch 秒 → 本机时区 HH:MM；ts 缺失按当前时刻。"""
    return time.strftime("%H:%M", time.localtime(ts))


def render_intel(row: dict, item_name: str, group_name: str) -> str:
    lines = [f"【{item_name} · {group_name}】{row.get('title', '')}",
             row.get("detail") or ""]
    deadline = row.get("deadline")
    if deadline:
        lines.append(f"截止：{deadline}")
    lines.append(f"来源：{group_name} · {_hhmm(row.get('ts'))}")
    return "\n".join(lines)


def render_digest(day: str, rows: list[dict], item_names: dict[str, str],
                  group_names: dict[str, str],
                  suppressed_count: int = 0) -> str | None:
    """渲染当日汇总；suppressed_count>0 时尾行点名累计被抑制条数（设计
    §5.5"有 N 条被抑制"该句，终审 I4）。计数措辞用"累计"——suppressed 是
    全表口径（重试耗尽的既有行不清零），"当日 N 条"会失真。0（默认）时
    输出与无参版逐字节一致。"""
    if not rows:
        return None
    groups: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: r.get("ts") or 0):
        groups.setdefault(row.get("item_id") or "", []).append(row)
    out = [f"{day} 汇总", ""]
    for item_id, group_rows in groups.items():
        out.append(f"【{item_names.get(item_id) or '未知项'}】")
        for row in group_rows:
            gid = row.get("group") or ""
            gname = group_names.get(gid) or gid
            out.append(f"  {_hhmm(row.get('ts'))} {gname} · "
                       f"{row.get('title', '')}")
    text = "\n".join(out)
    if suppressed_count > 0:
        # 契约原文含前导 \n：单换行接尾行，不隔空行。
        text += (f"\n另有累计 {suppressed_count} 条情报被抑制（重试耗尽），"
                 "跑 groupwatch status 查看。")
    return text
