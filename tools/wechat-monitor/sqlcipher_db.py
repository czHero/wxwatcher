"""Discovery and read-only SQLCipher CLI access.

The raw per-DB key is passed to the sqlcipher shell on stdin, never in argv
or the environment.  All monitor queries are SELECT-only; the module itself
does not execute DDL/DML.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from crypto import PAGE_SIZE, derive_raw_key, page1_ok, read_page1
from paths import default_wechat_files

WECHAT_FILES = default_wechat_files()
MESSAGE_DB_RE = re.compile(r"^message_[0-9]+\.db$")


class SQLCipherError(RuntimeError):
    pass


def account_fingerprint(db_storage: Path) -> str:
    """Stable short fingerprint of the account directory name."""
    account = db_storage.parent.name
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:8]


def find_db_storages(base: Path | None = None) -> list[Path]:
    base = base or WECHAT_FILES
    if not base.is_dir():
        return []
    candidates = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        db_storage = child / "db_storage"
        if (db_storage / "message" / "message_0.db").is_file():
            candidates.append(db_storage)
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def discover_message_dbs(db_storage: Path) -> list[Path]:
    msg_dir = db_storage / "message"
    if not msg_dir.is_dir():
        raise FileNotFoundError(f"message directory not found under {db_storage.name}")
    dbs = [
        p
        for p in msg_dir.iterdir()
        if MESSAGE_DB_RE.match(p.name) and not p.name.endswith("-wal") and not p.name.endswith("-shm")
    ]
    if not (msg_dir / "message_0.db").is_file():
        raise FileNotFoundError("message_0.db not found")
    return sorted(dbs, key=lambda p: int(p.stem.split("_")[1]))


def discover_aux_dbs(db_storage: Path) -> list[Path]:
    """DBs used by schema probing, not by the poller."""
    paths = []
    for rel in (
        "message/message_fts.db",
        "message/message_resource.db",
        "session/session.db",
        "contact/contact.db",
    ):
        p = db_storage / rel
        if p.is_file():
            paths.append(p)
    return paths


def derive_keys_for_dbs(passphrase: bytes, dbs: Sequence[Path]) -> dict[str, bytes]:
    keys = {}
    for db in dbs:
        page = read_page1(str(db))
        salt = page[:16]
        keys[str(db)] = derive_raw_key(passphrase, salt)
    return keys


def verify_page1(db_path: Path, raw_key: bytes) -> list[str]:
    return page1_ok(read_page1(str(db_path)), raw_key, mode="raw")


def load_passphrase(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) == 32:
        return data
    text = data.decode("ascii", errors="strict").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)
    raise ValueError(f"{path}: expected 32 raw bytes or 64 hex characters")


def load_db_keys(path: Path) -> dict[str, bytes]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, hex_key in obj.get("keys", {}).items():
        key = bytes.fromhex(hex_key)
        if len(key) != 32:
            raise ValueError(f"{name}: key is not 32 bytes")
        out[name] = key
    return out


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def write_private_text(path: Path, text: str) -> None:
    write_private(path, text.encode("utf-8"))


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _find_sqlcipher() -> str:
    exe = shutil.which("sqlcipher")
    if not exe:
        raise SQLCipherError("sqlcipher not found on PATH; install it (apt/dnf: sqlcipher, brew: sqlcipher)")
    return exe


def _parse_json_output(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        # Multiple statements can produce multiple arrays; use the last one.
        start = text.rfind("\n[")
        if start != -1:
            try:
                return json.loads(text[start + 1 :])
            except json.JSONDecodeError:
                pass
        raise SQLCipherError(f"cannot parse sqlcipher JSON output (bytes={len(text)})")


def run_sqlcipher(
    db_path: Path,
    key_hex: str,
    sql: str,
    *,
    readonly: bool = True,
    timeout: int = 30,
) -> list[dict]:
    """Run one SELECT (or read-only PRAGMA) and return rows as dicts.

    The key is written to stdin; it never appears in argv or the environment.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
        raise ValueError("key_hex must be 64 hex characters")
    if not sql.strip():
        raise ValueError("empty SQL")
    args = [_find_sqlcipher(), "-batch", "-json"]
    if readonly:
        args.append("-readonly")
    args.append(str(db_path))
    # Setup statements (key, query_only, timeout) also emit result rows in
    # -json mode; route them to /dev/null so stdout contains only the real
    # query result (an empty SELECT result then stays unambiguous).
    script = (
        ".output /dev/null\n"
        f'PRAGMA key = "x\'{key_hex}\'";\n'
        "PRAGMA query_only=ON;\n"
        "PRAGMA busy_timeout=2000;\n"
        ".output stdout\n"
        f"{sql.strip()}\n"
    )
    try:
        proc = subprocess.run(
            args,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SQLCipherError(f"sqlcipher timed out after {timeout}s") from exc
    err = proc.stderr.strip()
    if proc.returncode != 0:
        raise SQLCipherError(f"sqlcipher exited {proc.returncode}: {err[:500]}")
    if err:
        # The shell reports SQL errors on stderr but may still exit 0.
        low = err.lower()
        if "error" in low or "not a database" in low or "file is encrypted" in low:
            raise SQLCipherError(f"sqlcipher error: {err[:500]}")
    return _parse_json_output(proc.stdout)


def list_tables(db_path: Path, key_hex: str) -> list[str]:
    rows = run_sqlcipher(
        db_path,
        key_hex,
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    return [r["name"] for r in rows]


def table_info(db_path: Path, key_hex: str, table: str) -> list[dict]:
    return run_sqlcipher(db_path, key_hex, f"PRAGMA table_info({quote_ident(table)})")


def index_list(db_path: Path, key_hex: str, table: str) -> list[dict]:
    return run_sqlcipher(db_path, key_hex, f"PRAGMA index_list({quote_ident(table)})")


def scalar(db_path: Path, key_hex: str, sql: str):
    rows = run_sqlcipher(db_path, key_hex, sql)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def file_snapshot(paths: Iterable[Path]) -> dict[str, tuple[int, int]]:
    snap = {}
    for p in paths:
        try:
            st = p.stat()
        except FileNotFoundError:
            snap[str(p)] = (-1, -1)
        else:
            snap[str(p)] = (st.st_mtime_ns, st.st_size)
    return snap


def check_unchanged(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    changed = []
    for path, old in before.items():
        new = after.get(path)
        if new != old:
            changed.append(path)
    return changed
