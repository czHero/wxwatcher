#!/usr/bin/env python3
"""Read-only WeChat group-message poller.

State:   ~/.wechat-monitor/state.json (0600)
Output:  ~/.wechat-monitor/messages.jsonl (0600)
Keys:    ~/.wechat-monitor/passphrase (32 raw bytes, 0600) or db_keys.json

No DDL/DML is ever executed.  The sqlcipher shell receives the per-DB raw key
on stdin; message text is written only to the local JSONL file.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from content_parser import is_text_type, parse_content
from crypto import derive_raw_key, fingerprint, page1_ok, read_page1
from sqlcipher_db import (
    SQLCipherError,
    account_fingerprint,
    discover_message_dbs,
    find_db_storages,
    list_tables,
    load_db_keys,
    load_passphrase,
    quote_ident,
    quote_literal,
    run_sqlcipher,
    table_info,
    write_private_text,
)

DEFAULT_HOME = Path.home() / ".wechat-monitor"
REQUIRED_COLUMNS = {"local_id", "local_type", "create_time", "message_content"}
GROUP_SUFFIX = "@chatroom"


@dataclass
class Table:
    name: str
    columns: set[str]


@dataclass
class Database:
    path: Path
    key: bytes
    tables: list[Table]
    has_name2id: bool


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def choose_db_storage(explicit: str | None, account_fp: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not (p / "message" / "message_0.db").is_file():
            raise SystemExit(f"not a db_storage directory: {p}")
        return p
    candidates = find_db_storages()
    if not candidates:
        raise SystemExit("no WeChat db_storage found")
    if account_fp:
        matches = [p for p in candidates if account_fingerprint(p) == account_fp]
        if len(matches) != 1:
            raise SystemExit(f"account_fp {account_fp}: {len(matches)} matches")
        return matches[0]
    if len(candidates) > 1:
        fps = ", ".join(account_fingerprint(p) for p in candidates)
        raise SystemExit(f"multiple accounts found ({fps}); pass --account-fp")
    return candidates[0]


def load_key_material(db_storage: Path, dbs: list[Path]) -> tuple[dict[str, bytes], str]:
    passphrase_path = DEFAULT_HOME / "passphrase"
    keys_path = DEFAULT_HOME / "db_keys.json"
    errors = []
    if passphrase_path.is_file():
        try:
            passphrase = load_passphrase(passphrase_path)
            keys = {}
            for db in dbs:
                page = read_page1(str(db))
                key = derive_raw_key(passphrase, page[:16])
                hits = page1_ok(page, key, mode="raw")
                if not hits:
                    errors.append(f"{db.name}: page1 NO HIT")
                    continue
                keys[str(db)] = key
            if len(keys) == len(dbs):
                return keys, f"passphrase:{fingerprint(passphrase)}"
            errors.append(f"passphrase validated {len(keys)}/{len(dbs)} shards")
        except Exception as exc:
            errors.append(f"passphrase load failed: {type(exc).__name__}")
    if keys_path.is_file():
        try:
            raw = load_db_keys(keys_path)
            keys = {}
            for db in dbs:
                key = raw.get(db.name)
                if key is None:
                    errors.append(f"{db.name}: missing in db_keys.json")
                    continue
                hits = page1_ok(read_page1(str(db)), key, mode="raw")
                if not hits:
                    errors.append(f"{db.name}: raw key NO HIT")
                    continue
                keys[str(db)] = key
            if len(keys) == len(dbs):
                return keys, "db_keys"
            errors.append(f"raw keys validated {len(keys)}/{len(dbs)} shards")
        except Exception as exc:
            errors.append(f"db_keys load failed: {type(exc).__name__}")
    raise SystemExit("KEY_INVALID: " + "; ".join(errors or ["no key material"]))


def load_aux_keys(aux_paths: list[Path]) -> dict[str, bytes]:
    """Derive/load keys for session.db/contact.db without requiring them."""
    passphrase_path = DEFAULT_HOME / "passphrase"
    keys_path = DEFAULT_HOME / "db_keys.json"
    out: dict[str, bytes] = {}
    if passphrase_path.is_file():
        try:
            passphrase = load_passphrase(passphrase_path)
        except Exception:
            return out
        for db in aux_paths:
            if not db.is_file():
                continue
            try:
                page = read_page1(str(db))
                key = derive_raw_key(passphrase, page[:16])
                if page1_ok(page, key, mode="raw"):
                    out[str(db)] = key
            except Exception:
                continue
        return out
    if keys_path.is_file():
        try:
            raw = load_db_keys(keys_path)
        except Exception:
            return out
        for db in aux_paths:
            key = raw.get(db.name)
            if key is None:
                continue
            try:
                if page1_ok(read_page1(str(db)), key, mode="raw"):
                    out[str(db)] = key
            except Exception:
                continue
    return out


def _split_top_level(body: str) -> list[str]:
    parts, cur, depth = [], "", 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _parse_columns(sql: str) -> set[str]:
    start = sql.find("(")
    end = sql.rfind(")")
    if start == -1 or end <= start:
        return set()
    cols = set()
    for part in _split_top_level(sql[start + 1 : end]):
        part = part.strip()
        if not part:
            continue
        head = part.split(None, 1)[0]
        if head.upper() in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
            continue
        cols.add(head.strip('"'))
    return cols


def refresh_tables(db, known: list[Table] | None = None):
    """Re-enumerate Msg_ tables; returns (all_tables, newly_added).

    WeChat creates a fresh Msg_<md5> table for each new conversation while
    the poller runs; without this the poller never sees new chats.
    """
    known = known if known is not None else list(getattr(db, "tables", []))
    tables, _has = discover_tables(db.path, db.key)
    have = {t.name for t in known}
    added = [t for t in tables if t.name not in have]
    return tables, added


def discover_tables(db: Path, key: bytes) -> tuple[list[Table], bool]:
    key_hex = key.hex()
    rows = run_sqlcipher(
        db, key_hex, "SELECT name, sql FROM sqlite_master WHERE type='table'", timeout=30
    )
    names = [r["name"] for r in rows]
    has_name2id = "Name2Id" in names
    tables = []
    problems = []
    for r in rows:
        name = r["name"]
        if not name.startswith("Msg_"):
            continue
        cols = _parse_columns(r.get("sql") or "")
        missing = REQUIRED_COLUMNS - cols
        if missing:
            problems.append((name, sorted(missing)))
            continue
        tables.append(Table(name=name, columns=cols))
    if problems[:3]:
        log(f"{db.name}: {len(problems)} Msg_ tables lack {sorted(REQUIRED_COLUMNS)}: {problems[:3]}")
    return tables, has_name2id


def _candidate_columns(cols: list[dict]) -> tuple[list[str], list[str]]:
    names = [c["name"] for c in cols]
    id_cols = [
        n
        for n in names
        if any(t in n.lower() for t in ("username", "user_name", "wxid", "session_id", "contact_id"))
    ]
    display_cols = [
        n
        for n in names
        if any(t in n.lower() for t in ("remark", "nick", "display", "alias"))
        and n not in id_cols
    ]
    return id_cols, display_cols


def probe_maps(db: Path, key: bytes) -> tuple[dict[str, dict], dict[str, str]]:
    """Return ({group_id: {name, table}}, {contact_id: display_name})."""
    key_hex = key.hex()
    groups: dict[str, dict] = {}
    contacts: dict[str, str] = {}
    try:
        tables = list_tables(db, key_hex)
    except Exception:
        return groups, contacts
    for table in tables:
        try:
            cols = table_info(db, key_hex, table)
        except Exception:
            continue
        id_cols, display_cols = _candidate_columns(cols)
        if not id_cols:
            continue
        for id_col in id_cols[:2]:
            select_cols = [id_col] + display_cols[:3]
            select_sql = ", ".join(quote_ident(c) for c in select_cols)
            try:
                rows = run_sqlcipher(
                    db,
                    key_hex,
                    f"SELECT {select_sql} FROM {quote_ident(table)} "
                    f"WHERE {quote_ident(id_col)} LIKE '%{GROUP_SUFFIX}' LIMIT 2000",
                )
            except Exception:
                rows = []
            for row in rows:
                gid = row.get(id_col)
                if not gid:
                    continue
                name = None
                for c in display_cols:
                    if row.get(c):
                        name = row[c]
                        break
                groups[str(gid)] = {"name": name, "table": table}
            try:
                rows2 = run_sqlcipher(
                    db,
                    key_hex,
                    f"SELECT {select_sql} FROM {quote_ident(table)} LIMIT 5000",
                )
            except Exception:
                rows2 = []
            for row in rows2:
                cid = row.get(id_col)
                if not cid or str(cid).endswith(GROUP_SUFFIX):
                    continue
                for c in display_cols:
                    if row.get(c):
                        contacts[str(cid)] = str(row[c])
                        break
    return groups, contacts


def load_maps(db_storage: Path, keys: dict[str, bytes]) -> tuple[dict[str, dict], dict[str, str], dict[str, str]]:
    groups: dict[str, dict] = {}
    contacts: dict[str, str] = {}
    sources = {}
    for rel in ("session/session.db", "contact/contact.db"):
        db = db_storage / rel
        key = keys.get(str(db))
        if key is None:
            continue
        g, c = probe_maps(db, key)
        for gid, info in g.items():
            if gid not in groups:
                groups[gid] = info
                sources[gid] = info["table"]
        for cid, name in c.items():
            contacts.setdefault(cid, name)
    if not groups:
        raise SystemExit("no @chatroom groups found in session/contact; run schema_probe.py")
    return groups, contacts, sources


def group_table_map(groups: dict[str, dict]) -> dict[str, dict]:
    """Map Msg_<md5(group_id)> -> group info."""
    out = {}
    for gid, info in groups.items():
        digest = hashlib.md5(gid.encode("utf-8")).hexdigest()
        out[f"Msg_{digest}"] = {"id": gid, **info}
    return out


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "watermarks": {}}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"cannot read state: {type(exc).__name__}")
    if obj.get("version") != 1 or not isinstance(obj.get("watermarks"), dict):
        raise SystemExit("unsupported state file")
    return obj


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write_private_text(tmp, json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def state_key(db: Database, table: str) -> str:
    return f"{db.path.name}|{table}"


def query_max(db: Database, table: str) -> int:
    rows = run_sqlcipher(
        db.path,
        db.key.hex(),
        f"SELECT COALESCE(MAX(local_id), 0) AS n FROM {quote_ident(table)}",
    )
    if rows and "n" in rows[0]:
        return int(rows[0]["n"])
    raise SQLCipherError(f"unexpected query_max output for {table}: {list(rows[0])[:4] if rows else 'empty'}")


def query_backfill_watermark(db: Database, table: str, count: int) -> int:
    if count <= 0:
        return 0
    rows = run_sqlcipher(
        db.path,
        db.key.hex(),
        f"SELECT local_id FROM {quote_ident(table)} ORDER BY local_id DESC LIMIT 1 OFFSET {int(count)}",
    )
    if not rows:
        return 0
    if "local_id" not in rows[0]:
        raise SQLCipherError(f"unexpected backfill output for {table}: {list(rows[0])[:4]}")
    return int(rows[0]["local_id"])


def initialize_watermarks(
    dbs: list[Database],
    state: dict,
    *,
    since_now: bool,
    backfill: int | None,
) -> None:
    watermarks = state["watermarks"]
    fresh = not bool(watermarks)
    for db in dbs:
        for table in db.tables:
            key = state_key(db, table.name)
            max_id = query_max(db, table.name)
            if key in watermarks:
                old = int(watermarks[key])
                if max_id < old:
                    log(f"watermark reset {db.path.name}/{table.name}: {old} -> 0")
                    watermarks[key] = 0
                if since_now and max_id > old:
                    watermarks[key] = max_id
                continue
            if backfill is not None:
                watermarks[key] = query_backfill_watermark(db, table.name, backfill)
            elif fresh or since_now:
                watermarks[key] = max_id
            else:
                watermarks[key] = max_id


def build_union_sql(db: Database, tables: list[Table], watermarks: dict[str, int], limit: int) -> str:
    parts = []
    for table in tables:
        wm = int(watermarks.get(state_key(db, table.name), 0))
        server = "m.server_id" if "server_id" in table.columns else "NULL"
        sender = "m.real_sender_id" if "real_sender_id" in table.columns else "NULL"
        join = ""
        sender_name = "NULL"
        if db.has_name2id and "real_sender_id" in table.columns:
            join = ' LEFT JOIN "Name2Id" n ON n.rowid = m.real_sender_id'
            sender_name = "n.user_name"
        parts.append(
            "SELECT * FROM ("
            f"SELECT {quote_literal(table.name)} AS t, "
            f"m.local_id AS local_id, {server} AS server_id, "
            f"m.local_type AS local_type, m.create_time AS create_time, "
            f"{sender} AS real_sender_id, {sender_name} AS sender_id, "
            "hex(m.message_content) AS content_hex "
            f"FROM {quote_ident(table.name)} m{join} "
            f"WHERE m.local_id > {wm} ORDER BY m.local_id LIMIT {int(limit)}"
            ")"
        )
    return " UNION ALL ".join(parts)


def read_db_rows(db: Database, watermarks: dict[str, int], limit: int) -> list[dict]:
    selected = db.tables
    if not selected:
        return []
    rows = []
    # Stay well below SQLite's compound-select limit.
    for i in range(0, len(selected), 100):
        chunk = selected[i : i + 100]
        sql = build_union_sql(db, chunk, watermarks, limit)
        last_error = None
        for attempt in range(3):
            try:
                rows.extend(run_sqlcipher(db.path, db.key.hex(), sql, timeout=40))
                last_error = None
                break
            except SQLCipherError as exc:
                last_error = exc
                if "busy" not in str(exc).lower() and "locked" not in str(exc).lower():
                    raise
                time.sleep(0.5 * (attempt + 1))
        if last_error is not None:
            raise last_error
    return rows


def iso_time(value) -> str:
    try:
        return datetime.fromtimestamp(int(value)).astimezone().isoformat(timespec="seconds")
    except Exception:
        return str(value)


def run_tick(
    dbs: list[Database],
    group_tables: dict[str, dict],
    single_tables: dict[str, str],
    contacts: dict[str, str],
    state: dict,
    out_fd: int | None,
    *,
    echo: bool,
    json_stdout: bool,
    group_filter: str | None,
    groups_only: bool,
    limit: int,
) -> int:
    watermarks = state["watermarks"]
    records = []
    skipped_type = 0
    skipped_text = 0
    for db in dbs:
        rows = read_db_rows(db, watermarks, limit)
        for row in rows:
            table = row.get("t")
            local_id = int(row.get("local_id") or 0)
            key = state_key(db, table)
            if local_id > int(watermarks.get(key, 0)):
                watermarks[key] = local_id
            ginfo = group_tables.get(table)
            cid = single_tables.get(table)
            if ginfo is not None:
                kind = "group"
            elif cid is not None:
                kind = "single"
            else:
                kind = "unknown"
            if groups_only and kind != "group":
                continue
            try:
                local_type = int(row.get("local_type") or 0)
            except (TypeError, ValueError):
                local_type = 0
            if not is_text_type(local_type):
                skipped_type += 1
                continue
            try:
                content = bytes.fromhex(row.get("content_hex") or "")
            except ValueError:
                skipped_text += 1
                continue
            parsed = parse_content(content)
            if parsed.text is None:
                skipped_text += 1
                continue
            sender_id = parsed.sender_id or row.get("sender_id")
            if ginfo is not None:
                conv_id = ginfo["id"]
                conv_name = ginfo.get("name") or conv_id
            elif cid is not None:
                conv_id = cid
                conv_name = contacts.get(str(cid)) or cid
            else:
                conv_id = table  # Msg_<md5>: identity without lookup
                conv_name = None
            if group_filter:
                needle = group_filter.lower()
                if needle not in str(conv_id).lower() and needle not in str(conv_name or "").lower():
                    continue
            record = {
                "ts": iso_time(row.get("create_time")),
                "db": db.path.name,
                "table": table,
                "local_id": local_id,
                "server_id": row.get("server_id"),
                "create_time": row.get("create_time"),
                "kind": kind,
                "conversation_id": conv_id,
                "conversation_name": conv_name,
                "sender_id": sender_id,
                "sender_name": contacts.get(str(sender_id)) if sender_id else None,
                "type": local_type,
                "text": parsed.text,
                "decompressed": parsed.decompressed,
            }
            records.append(record)

    records.sort(key=lambda r: (r["create_time"] or 0, r["db"], r["local_id"]))
    for record in records:
        if out_fd is not None:
            line = json.dumps(record, ensure_ascii=False) + "\n"
            os.write(out_fd, line.encode("utf-8"))
        if json_stdout:
            print(json.dumps(record, ensure_ascii=False), flush=True)
        elif echo:
            sender = record.get("sender_name") or record.get("sender_id") or "?"
            conv = record.get("conversation_name") or record.get("conversation_id") or "?"
            print(f"[{record['kind']}] {conv} | {sender}: {record['text']}", flush=True)
    if out_fd is not None and records:
        os.fsync(out_fd)
    log(
        f"new={len(records)} skipped_type={skipped_type} skipped_text={skipped_text} "
        f"watermarks={len(watermarks)}"
    )
    return len(records)


def open_output(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-storage")
    ap.add_argument("--account-fp")
    ap.add_argument("--state", default=str(DEFAULT_HOME / "state.json"))
    ap.add_argument("--out", default=str(DEFAULT_HOME / "messages.jsonl"))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--since-now", action="store_true", help="reset watermarks to current max")
    ap.add_argument("--backfill", type=int, default=None, help="emit the last N rows per table on first run")
    ap.add_argument("--limit", type=int, default=500, help="max rows per table per tick")
    ap.add_argument("--group", help="substring filter on conversation id or name")
    ap.add_argument("--groups-only", action="store_true", help="emit only @chatroom groups")
    ap.add_argument("--echo", action="store_true", help="print message text to stdout")
    ap.add_argument("--json", action="store_true", help="print JSON records to stdout")
    ap.add_argument("--no-file", action="store_true")
    args = ap.parse_args(argv)

    db_storage = choose_db_storage(args.db_storage, args.account_fp)
    db_paths = discover_message_dbs(db_storage)
    keys, key_source = load_key_material(db_storage, db_paths)
    log(f"account_fp={account_fingerprint(db_storage)} shards={len(db_paths)} key={key_source}")

    dbs = []
    for path in db_paths:
        tables, has_name2id = discover_tables(path, keys[str(path)])
        dbs.append(Database(path=path, key=keys[str(path)], tables=tables, has_name2id=has_name2id))
    aux_paths = [
        db_storage / "session/session.db",
        db_storage / "contact/contact.db",
    ]
    all_keys = dict(keys)
    all_keys.update(load_aux_keys(aux_paths))
    groups, contacts, _ = load_maps(db_storage, all_keys)
    table_map = group_table_map(groups)
    single_map = {}
    for cid in contacts:
        digest = hashlib.md5(cid.encode("utf-8")).hexdigest()
        single_map[f"Msg_{digest}"] = cid

    def rebuild_maps():
        nonlocal groups, contacts, table_map, single_map
        groups, contacts, _ = load_maps(db_storage, all_keys)
        table_map = group_table_map(groups)
        single_map = {}
        for cid in contacts:
            single_map[f"Msg_{hashlib.md5(cid.encode('utf-8')).hexdigest()}"] = cid

    all_tables = sum(len(db.tables) for db in dbs)
    log(
        f"tables={all_tables} groups={len(groups)} group_hits={sum(1 for db in dbs for t in db.tables if t.name in table_map)} "
        f"single_hits={sum(1 for db in dbs for t in db.tables if t.name in single_map)} contacts={len(contacts)}"
    )

    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    initialize_watermarks(
        dbs,
        state,
        since_now=args.since_now,
        backfill=args.backfill,
    )
    save_state(state_path, state)
    log(f"state={state_path} watermarks={len(state['watermarks'])}")

    lock_path = DEFAULT_HOME / "monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another monitor instance is running")

    out_fd = None if args.no_file else open_output(Path(args.out).expanduser())
    stop = {"flag": False}

    def _signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)
    try:
        while True:
            # WeChat creates a new Msg_<md5> table for every new chat;
            # refresh each tick so the poller never goes blind to them.
            for db in dbs:
                _all, added = refresh_tables(db, known=db.tables)
                if added:
                    db.tables.extend(added)
                    for t in added:
                        state["watermarks"].setdefault(state_key(db, t.name), 0)
                    log(f"new tables: {db.path.name}/{[t.name for t in added]}")
                    rebuild_maps()
            run_tick(
                dbs,
                table_map,
                single_map,
                contacts,
                state,
                out_fd,
                echo=args.echo,
                json_stdout=args.json,
                group_filter=args.group,
                groups_only=args.groups_only,
                limit=args.limit,
            )
            save_state(state_path, state)
            if args.once or stop["flag"]:
                break
            deadline = time.time() + args.interval
            while time.time() < deadline and not stop["flag"]:
                time.sleep(0.2)
            if stop["flag"]:
                break
    finally:
        if out_fd is not None:
            os.close(out_fd)
        save_state(state_path, state)
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
