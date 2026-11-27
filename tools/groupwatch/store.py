"""groupwatch SQLite 状态存储。

单文件 `groupwatch.db`，7 张表：cursor / messages_seen / inbox / intelligence /
dispatches / audit_log / digest_fired。`open_store` 幂等建表；查询统一返回
dict（`sqlite3.Row` 转换），每个写方法各自 commit（进程内单连接、跨重开可见）
——例外是 `accept_message`：seen+inbox 两句共用**一次** commit，原子性属
C2 崩溃契约，不可拆。`inbox_after` 供 audited 水位重启恢复（rowid 升序）。

`intelligence.source_hash` 为 UNIQUE 去重列；哈希口径与 audit.py 的
`source_hash()` 一致（sha1 of "\\n".join(sorted(keys))），由本模块从
row["source_keys"] 内部派生——哈希的公共 API 属 audit.py（Task 5），此处只是
填列，不对外导出哈希函数。`group`/`table` 是 SQL 关键字，一律双引号包裹。
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cursor (
    key TEXT PRIMARY KEY,
    offset INTEGER
);
CREATE TABLE IF NOT EXISTS messages_seen (
    db TEXT,
    "table" TEXT,
    local_id INTEGER,
    PRIMARY KEY (db, "table", local_id)
);
CREATE TABLE IF NOT EXISTS inbox (
    db TEXT,
    "table" TEXT,
    local_id INTEGER,
    ts INTEGER,
    "group" TEXT,
    sender_name TEXT,
    text TEXT,
    PRIMARY KEY (db, "table", local_id)
);
CREATE TABLE IF NOT EXISTS intelligence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    "group" TEXT,
    item_id TEXT,
    title TEXT,
    detail TEXT,
    deadline TEXT,
    action TEXT,
    source_keys TEXT,
    source_hash TEXT UNIQUE,
    raw_json TEXT,
    pushed TEXT DEFAULT 'pending',
    retries INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY,
    ts INTEGER,
    kind TEXT,
    status TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER,
    "group" TEXT,
    n_msgs INTEGER,
    hits INTEGER,
    status TEXT
);
CREATE TABLE IF NOT EXISTS digest_fired (
    day TEXT,
    hhmm TEXT,
    PRIMARY KEY (day, hhmm)
);
CREATE TABLE IF NOT EXISTS digest_pending (
    day TEXT,
    hhmm TEXT,
    text TEXT,
    created_ts INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (day, hhmm)
);
"""


def _migrate_digest_pending_state(conn) -> None:
    """F2-round2 迁移：round-1 建库的实况 DB（2026-11-26 事故机）里
    digest_pending 无 state 列。CREATE TABLE IF NOT EXISTS 不会碰已存在的
    表，故显式 ALTER 补列——SQLite 允许 NOT NULL + 常量 DEFAULT，旧行按
    DEFAULT 落 'pending'（歧义语义尚不存在过，重试合法）。幂等：列已在即
    跳过，重复 open_store 不报错。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(digest_pending)")}
    if "state" not in cols:
        conn.execute("ALTER TABLE digest_pending ADD COLUMN "
                     "state TEXT NOT NULL DEFAULT 'pending'")


def _source_hash(keys) -> str:
    """与 audit.py source_hash() 同口径：sha1("\\n".join(sorted(keys))) hex。"""
    return hashlib.sha1("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ---- cursor ----

    def cursor_get(self, key: str) -> int:
        row = self._conn.execute(
            "SELECT offset FROM cursor WHERE key=?", (key,)).fetchone()
        return 0 if row is None else row["offset"]

    def cursor_set(self, key: str, offset: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cursor(key, offset) VALUES(?, ?)",
            (key, offset))
        self._conn.commit()

    # ---- messages_seen / inbox ----

    def is_seen(self, db: str, table: str, local_id: int) -> bool:
        row = self._conn.execute(
            'SELECT 1 FROM messages_seen WHERE db=? AND "table"=? AND local_id=?',
            (db, table, local_id)).fetchone()
        return row is not None

    def mark_seen(self, db: str, table: str, local_id: int) -> None:
        self._conn.execute(
            'INSERT OR IGNORE INTO messages_seen(db, "table", local_id) '
            "VALUES(?, ?, ?)", (db, table, local_id))
        self._conn.commit()

    def insert_inbox(self, msg: dict) -> None:
        self._conn.execute(
            'INSERT OR IGNORE INTO inbox(db, "table", local_id, ts, "group", '
            "sender_name, text) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (msg["db"], msg["table"], msg["local_id"], msg["ts"],
             msg["group"], msg["sender_name"], msg["text"]))
        self._conn.commit()

    def accept_message(self, msg: dict) -> int | None:
        """seen 标记 + inbox 落库**共用一条事务、一次 commit**（终审 C2）：
        崩溃落在两句之间 = 两行都不在，绝不存在"seen 了但正文没仓"的静默丢
        消息。返回 inbox 行的 rowid（audited 水位坐标）；INSERT OR IGNORE 撞
        已有行时回原行 rowid（幂等）。失败显式 rollback，不把半截事务留给
        后续别的写方法。"""
        try:
            self._conn.execute(
                'INSERT OR IGNORE INTO messages_seen(db, "table", local_id) '
                "VALUES(?, ?, ?)", (msg["db"], msg["table"], msg["local_id"]))
            self._conn.execute(
                'INSERT OR IGNORE INTO inbox(db, "table", local_id, ts, '
                '"group", sender_name, text) VALUES(?, ?, ?, ?, ?, ?, ?)',
                (msg["db"], msg["table"], msg["local_id"], msg["ts"],
                 msg["group"], msg["sender_name"], msg["text"]))
            row = self._conn.execute(
                'SELECT rowid FROM inbox WHERE db=? AND "table"=? AND local_id=?',
                (msg["db"], msg["table"], msg["local_id"])).fetchone()
            rowid = row["rowid"] if row is not None else None
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return rowid

    def inbox_after(self, group: str, after_rowid: int,
                    limit: int) -> list[dict]:
        """audited 水位恢复读：group 内 rowid > after_rowid 的 inbox 行，按
        rowid 升序（= 摄取顺序），每行携带 "rowid" 键，至多 limit 条。"""
        rows = self._conn.execute(
            'SELECT rowid, * FROM inbox WHERE "group"=? AND rowid>? '
            "ORDER BY rowid ASC LIMIT ?", (group, after_rowid, limit)).fetchall()
        return [dict(r) for r in rows]

    def recent(self, group: str, before_ts: int, limit: int) -> list[dict]:
        rows = self._conn.execute(
            'SELECT * FROM inbox WHERE "group"=? AND ts<? '
            "ORDER BY ts DESC LIMIT ?", (group, before_ts, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]  # 升序返回

    # ---- intelligence ----

    def insert_intelligence(self, row: dict) -> int | None:
        try:
            cur = self._conn.execute(
                'INSERT INTO intelligence(ts, "group", item_id, title, detail, '
                "deadline, action, source_keys, source_hash, raw_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), row["group"], row["item_id"],
                 row["title"], row["detail"], row["deadline"], row["action"],
                 json.dumps(row["source_keys"], ensure_ascii=False),
                 _source_hash(row["source_keys"]), row["raw_json"]))
        except sqlite3.IntegrityError:
            return None  # source_hash UNIQUE 冲突：不写
        self._conn.commit()
        return cur.lastrowid

    def pending(self, limit: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM intelligence WHERE pushed='pending' "
            "ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_status(self, intel_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE intelligence SET pushed=? WHERE id=?", (status, intel_id))
        self._conn.commit()

    def bump_retry(self, intel_id: int) -> int:
        self._conn.execute(
            "UPDATE intelligence SET retries=retries+1 WHERE id=?",
            (intel_id,))
        self._conn.commit()
        return self._conn.execute(
            "SELECT retries FROM intelligence WHERE id=?",
            (intel_id,)).fetchone()["retries"]

    def day_intels(self, day: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM intelligence "
            "WHERE date(ts, 'unixepoch', 'localtime')=? ORDER BY id",
            (day,)).fetchall()
        return [dict(r) for r in rows]

    # ---- digest_fired ----

    def try_fire_digest(self, day: str, hhmm: str) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO digest_fired(day, hhmm) VALUES(?, ?)",
            (day, hhmm))
        self._conn.commit()
        return cur.rowcount == 1  # 真=首次

    def digest_unfire(self, day: str, hhmm: str) -> None:
        """【已弃用·F2】旧 in-tick 重试 hack（配合 try_fire_digest）。持久
        队列落地后 one_tick 不再走 fire/unfire；保留实现只为兼容与既有契约
        测试，新代码请用 enqueue_digest/resolve_digest。"""
        self._conn.execute(
            "DELETE FROM digest_fired WHERE day=? AND hhmm=?", (day, hhmm))
        self._conn.commit()

    # ---- digest_pending（F2：未送达汇总持久补发队列）----

    def enqueue_digest(self, day: str, hhmm: str, text: str,
                      ts: int) -> bool:
        """排队一条待补发汇总（state 走列默认 'pending'）。INSERT OR IGNORE：
        True=新入队；False=该槽位已在队列——含 held 行（PK 撞行，歧义待裁决
        的原文本绝不被新一轮渲染覆盖）。已定稿槽位的防重排由调用方用
        has_digest_record 把守。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO digest_pending(day, hhmm, text, created_ts) "
            "VALUES(?, ?, ?, ?)", (day, hhmm, text, ts))
        self._conn.commit()
        return cur.rowcount == 1

    def oldest_digest_pending(self) -> dict | None:
        """队首（最早入队、state='pending'）待补发项，键含 day/hhmm/text/
        created_ts/state；无可发项 None。**held 行被排除**——歧义结局
        （needs_check/gui_partial/crash）永不自动重发（F2-round2 核心协议），
        只有人工裁决改回 pending 才重新入 FIFO。created_ts 相同（同 tick 多
        槽位齐补）按 day, hhmm 升序破平——先过期的先发。"""
        row = self._conn.execute(
            "SELECT * FROM digest_pending WHERE state='pending' "
            "ORDER BY created_ts, day, hhmm LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def hold_digest(self, day: str, hhmm: str) -> None:
        """歧义结局（推送返回 needs_check / gui_partial / crash——发送可能
        已执行、状态未知）→ state='held'：留在 digest_pending 供 status
        可见与人工裁决，但被 oldest_digest_pending 排除，自动重发不可达。
        行不存在是无害 no-op。"""
        self._conn.execute(
            "UPDATE digest_pending SET state='held' WHERE day=? AND hhmm=?",
            (day, hhmm))
        self._conn.commit()

    def held_digest_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM digest_pending "
            "WHERE state='held'").fetchone()["n"]

    def held_digests(self) -> list[dict]:
        """全部待人工裁决行（status 展示用），created_ts/day/hhmm 升序——
        首元素即最旧待裁决槽位。"""
        rows = self._conn.execute(
            "SELECT * FROM digest_pending WHERE state='held' "
            "ORDER BY created_ts, day, hhmm").fetchall()
        return [dict(r) for r in rows]

    def resolve_digest(self, day: str, hhmm: str) -> None:
        """槽位定稿（仅在推送 "ok" 后调用）：从 digest_pending 删行（任意
        state，held 行亦然——即人工裁决"确认已送达"的等价操作）+ INSERT OR
        IGNORE 进 digest_fired。两步各自幂等，重复调用无害。"""
        self._conn.execute(
            "DELETE FROM digest_pending WHERE day=? AND hhmm=?", (day, hhmm))
        self._conn.execute(
            "INSERT OR IGNORE INTO digest_fired(day, hhmm) VALUES(?, ?)",
            (day, hhmm))
        self._conn.commit()

    def has_digest_record(self, day: str, hhmm: str) -> bool:
        """该槽位是否"已处理"：在队列（任意 state，pending/held 皆算）**或**
        已定稿（digest_fired）——enqueue 侧的幂等守卫，防止已排队（含歧义
        待裁决）、已发槽位被重新渲染再排一遍。"""
        row = self._conn.execute(
            "SELECT 1 FROM digest_pending WHERE day=? AND hhmm=? "
            "UNION ALL SELECT 1 FROM digest_fired WHERE day=? AND hhmm=?",
            (day, hhmm, day, hhmm)).fetchone()
        return row is not None

    def fired_digests(self, day: str) -> list[str]:
        """该日已定稿（digest_fired 有行）的槽位 hhmm 升序列表——F2-round4
        status 逐槽点名"已发送"用。排队未定稿不算；按 day 精确过滤，历史日
        /其它日的行绝不掺入（跨天补发槽位属其当日，今日点名不背）。"""
        rows = self._conn.execute(
            "SELECT hhmm FROM digest_fired WHERE day=? ORDER BY hhmm",
            (day,)).fetchall()
        return [r["hhmm"] for r in rows]

    def pending_digest_count(self) -> int:
        """待补发条数（state='pending'）。held 行不计——它们有专列计数
        held_digest_count()，status 两路分开显示。"""
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM digest_pending "
            "WHERE state='pending'").fetchone()["n"]

    # ---- dispatches / audit_log ----

    def log_dispatch(self, kind: str, status: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO dispatches(ts, kind, status, detail) VALUES(?, ?, ?, ?)",
            (int(time.time()), kind, status, detail))
        self._conn.commit()

    def log_audit(self, group: str, n_msgs: int, hits: int, status: str) -> None:
        self._conn.execute(
            'INSERT INTO audit_log(ts, "group", n_msgs, hits, status) '
            "VALUES(?, ?, ?, ?, ?)",
            (int(time.time()), group, n_msgs, hits, status))
        self._conn.commit()

    def recent_dispatches(self, n: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dispatches ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def last_dispatch(self, kind: str) -> dict | None:
        """该 kind 的最新一笔 dispatch（无则 None）。F1：pre_fail 去重的
        比较对象——跨 kind 的"全局最新一笔"会遮蔽被挡 digest 的首笔
        （2026-11-26 实况：20:30 digest 的 pre_fail 整晚零痕迹）。"""
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,)).fetchone()
        return dict(row) if row is not None else None

    # ---- 聚合 / 清理 ----

    def counts(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM inbox) AS inbox, "
            "(SELECT COUNT(*) FROM intelligence) AS intelligence, "
            "(SELECT COUNT(*) FROM intelligence WHERE pushed='pending') AS pending, "
            "(SELECT COUNT(*) FROM intelligence WHERE pushed='hold') AS hold, "
            "(SELECT COUNT(*) FROM intelligence WHERE pushed='suppressed') "
            "AS suppressed, "
            "(SELECT COUNT(*) FROM dispatches) AS dispatches").fetchone()
        return {k: row[k] for k in ("inbox", "intelligence", "pending",
                                    "hold", "suppressed", "dispatches")}

    def prune_inbox(self, now_ts: int, retention_days: int) -> None:
        cutoff = now_ts - retention_days * 86400
        # messages_seen 无 ts 列：借道 inbox 的 (db,table,local_id) 删同批旧行，
        # 必须在删 inbox 之前执行；intelligence 永不动。
        self._conn.execute(
            "DELETE FROM messages_seen WHERE EXISTS ("
            'SELECT 1 FROM inbox i WHERE i.db = messages_seen.db '
            'AND i."table" = messages_seen."table" '
            "AND i.local_id = messages_seen.local_id AND i.ts < ?)",
            (cutoff,))
        self._conn.execute("DELETE FROM inbox WHERE ts < ?", (cutoff,))
        self._conn.commit()


def open_store(path: Path) -> Store:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _migrate_digest_pending_state(conn)  # F2-round2：旧库补 state 列（幂等）
    conn.commit()
    return Store(conn)
