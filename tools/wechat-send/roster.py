"""roster.json 解析与目标解析。

Task 6c（6b 联机结论）：目标必须置顶，row=置顶会话行的 y 坐标（GUI 靠它
点击聊天行，输入框随之自动聚焦）；wxid=直连消息表名用（有则跳过备注查询）。
两者均为可选字段——旧条目缺失仍合法（=None）；row 的类型校验刻意简单
（非负 int、非 bool），屏幕高度越界属 wxsend 运行期拒发范畴。
"""
import json
from dataclasses import dataclass
from pathlib import Path

KINDS = ("user", "group")


@dataclass
class Target:
    key: str
    kind: str
    ident: str
    row: int | None = None
    wxid: str | None = None


def load_roster(path: Path) -> dict[str, Target]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise ValueError(f"roster 缺少 'targets' 对象: {path}")
    out: dict[str, Target] = {}
    for key, entry in data["targets"].items():
        if not isinstance(entry, dict) or "kind" not in entry or "ident" not in entry:
            raise ValueError(f"roster 条目 {key!r} 缺少 'kind'/'ident'")
        if entry["kind"] not in KINDS:
            raise ValueError(f"roster 条目 {key!r} kind 非法: {entry['kind']!r}")
        row = None
        if "row" in entry:
            row = entry["row"]
            if isinstance(row, bool) or not isinstance(row, int) or row < 0:
                raise ValueError(
                    f"roster 条目 {key!r} 的 'row' 必须是非负 int: {row!r}")
        wxid = None
        if "wxid" in entry:
            wxid = entry["wxid"]
            if not isinstance(wxid, str) or not wxid:
                raise ValueError(
                    f"roster 条目 {key!r} 的 'wxid' 必须是非空 str: {wxid!r}")
        out[key] = Target(key, entry["kind"], entry["ident"], row=row, wxid=wxid)
    return out


def resolve(roster: dict[str, Target], key: str) -> Target:
    return roster[key]
