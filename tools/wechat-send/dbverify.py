"""dbverify: 数据库侧发送前/发送后校验。

- expected_table: 目标 ident -> Msg_<md5(ident)> 表名（实测 message_0.db 命名规则）。
- find_remark:    contact.db 按备注查 username（contact 表实测列为 `remark` /
                  `username`，见 task-3 Step 0 记录）。返回 0/1/多 个 id，
                  由调用方裁决唯一性；SQLCipherError（密钥失效等）原样上抛。
- latest_local_id: 发送前只读探测目标表 max(local_id) 基线；表不存在/空表 →
                  None；SQLCipherError（密钥失效等）原样上抛，供调用方在
                  任何 GUI 命令之前拒发（exit 2）。
- postcheck:      轮询消息表最新行（local_id 最大），parse_content 解压后与
                  sent_text 前 80 字符做 startswith 比对；clock/sleep 可注入。
                  after_id 给定时额外要求 local_id > after_id（spec 的
                  「local_id 增长」条款：同前缀旧行不得冒充回执）。

复用 tools/wechat-monitor 的 sqlcipher_db / content_parser（sys.path 引导，
与现有测试一致）。
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

_MONITOR_DIR = Path(__file__).resolve().parents[1] / "wechat-monitor"
if str(_MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(_MONITOR_DIR))

from sqlcipher_db import (  # noqa: E402
    SQLCipherError,
    quote_ident,
    quote_literal,
    run_sqlcipher,
)
from content_parser import parse_content  # noqa: E402

# 轮询期间可安全重试的 sqlcipher 错误片段（表尚未创建 / 库被短暂锁住）。
_RETRYABLE_ERRORS = ("no such table", "busy", "locked")
_MATCH_WINDOW = 80


def expected_table(ident: str) -> str:
    """WeChat 存储单个会话消息的表名：Msg_<md5(会话id)>。"""
    return "Msg_" + hashlib.md5(ident.encode()).hexdigest()


def find_remark(contact_db: Path, key_hex: str, remark: str) -> list[str]:
    """按备注精确查找联系人 username（wxid/chatroom id），返回 0/1/多 项列表。

    只查主 contact 表（实测真实 contact.db：`remark`、`username` 列）。
    不过滤 delete_flag：已删好友若撞备注，宁可让调用方看到“多义”而拒发，
    也不静默选中在线那一个。空/纯空白备注一律返回 []（不可能有效定位）。
    """
    if not remark or not remark.strip():
        return []
    sql = (
        "SELECT username FROM contact "
        "WHERE remark IS NOT NULL AND remark <> '' AND remark = "
        + quote_literal(remark)
    )
    rows = run_sqlcipher(Path(contact_db), key_hex, sql)
    return [row["username"] for row in rows]


def latest_local_id(message_db: Path, key_hex: str, table: str) -> int | None:
    """发送前基线：目标表当前 max(local_id)（只读探测，绝不写库）。

    表尚未建出（新会话，"no such table"）或表内无行 → None（此时 postcheck
    以 after_id=None 保持旧行为）；其它 SQLCipherError（密钥失效等）原样
    上抛——调用方必须在任何 GUI 命令之前据此拒发。
    """
    sql = f"SELECT max(local_id) AS max_id FROM {quote_ident(table)}"
    try:
        rows = run_sqlcipher(Path(message_db), key_hex, sql)
    except SQLCipherError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    value = rows[0].get("max_id") if rows else None
    return None if value is None else int(value)


def postcheck(
    message_db: Path,
    key_hex: str,
    table: str,
    sent_text: str,
    timeout_s: float = 15.0,
    poll_s: float = 1.5,
    clock=time.monotonic,
    sleep=time.sleep,
    after_id: int | None = None,
) -> bool:
    """轮询 table 内最新行，确认 sent_text 已落库；超时返回 False。

    比对规则：parse_content 解压得到 text 后，
    text.strip().startswith(sent_text.strip()[:80]) 即真。
    after_id 给定（发送前基线）时，匹配行还必须满足 local_id > after_id——
    同前缀的旧最新行（模板开头/上一次已送达的重试）不得冒充本次回执。
    “no such table”（新会话表尚未建出）与 busy/locked 视为可重试，继续轮询；
    其它 SQLCipherError（如密钥错误 "file is not a database"）直接上抛，
    绝不把解密失败伪装成发送超时。
    """
    if not sent_text or not sent_text.strip():
        raise ValueError("sent_text 为空，无内容可校验")
    want = sent_text.strip()[:_MATCH_WINDOW]
    growth = "" if after_id is None else f" WHERE local_id > {int(after_id)}"
    sql = (
        f"SELECT hex(message_content) AS content_hex FROM {quote_ident(table)}"
        + growth + " ORDER BY local_id DESC LIMIT 1"
    )
    deadline = clock() + timeout_s
    while True:
        try:
            rows = run_sqlcipher(Path(message_db), key_hex, sql)
        except SQLCipherError as exc:
            if not any(token in str(exc).lower() for token in _RETRYABLE_ERRORS):
                raise
            rows = []
        content_hex = rows[0].get("content_hex") if rows else None
        if content_hex:
            try:
                content = bytes.fromhex(content_hex)
            except ValueError:  # 异常编码的 hex：视为不匹配，继续轮询
                content = b""
            text = parse_content(content).text
            if text is not None and text.strip().startswith(want):
                return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll_s, remaining))
