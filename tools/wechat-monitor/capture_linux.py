#!/usr/bin/env python3
"""Linux passphrase capture via memory scan (port of the macOS lldb path).

WeChat for Linux 4.x keeps the login 32-byte passphrase in the main
process's anonymous memory as a 64-char hex string.  We read the process's
writable regions, extract hex-64 candidates, and validate each against the
page-1 HMAC oracle of a real message DB (crypto.page1_ok).  Nothing is
printed but short fingerprints; the verified passphrase goes to
~/.wechat-monitor/passphrase (0600).

Requires root (ptrace_scope>0), run:  sudo python3 capture_linux.py
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto import fingerprint, page1_ok, read_page1
from paths import wechat_files_dir
from scanner import candidate_passphrases, scan_candidates
from sqlcipher_db import (
    find_db_storages,
    discover_message_dbs,
    write_private,
)


def target_home() -> Path:
    """Real user's home even when run through sudo (root's home is useless)."""
    su = os.environ.get("SUDO_USER")
    if su and su != "root":
        try:
            return Path(pwd.getpwnam(su).pw_dir)
        except KeyError:
            pass
    return Path.home()


HOME = target_home()
STATE_DIR = HOME / ".wechat-monitor"
PASSPHRASE_FILE = STATE_DIR / "passphrase"
CHUNK = 8 << 20
OVERLAP = 256
MAX_SCAN_BYTES = 4 << 30

_MAPS_RE = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+) (rw-[ps]) 0*[0-9a-f]+ \d+:\d+ \d+(?:\s+(.*))?$"
)


def parse_writable_maps(text: str, max_size: int = 512 << 20) -> list[tuple[int, int, str]]:
    """Private/shared writable mappings, each capped at max_size bytes."""
    out = []
    for line in text.splitlines():
        m = _MAPS_RE.match(line)
        if not m:
            continue
        start, end = int(m.group(1), 16), int(m.group(2), 16)
        if end - start > max_size:
            continue
        out.append((start, end, m.group(4) or "[anon]"))
    return out


def find_wechat_pids() -> list[int]:
    """PIDs of the main wechat binary (not the AppEx render helpers)."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            exe = Path(f"/proc/{pid}/exe").resolve(strict=True)
        except OSError:
            continue
        if exe == Path("/opt/wechat/wechat"):
            pids.append(pid)
    return sorted(pids)


def writable_regions(pid: int) -> list[tuple[int, int, str]]:
    """(start, end, name) writable mappings of a pid (guard/RO/exec excluded)."""
    with open(f"/proc/{pid}/maps", encoding="ascii", errors="replace") as f:
        return parse_writable_maps(f.read())


def scan_pid(pid: int, oracle_pages: list[bytes]) -> tuple[str, bytes, int] | None:
    """Return (form, passphrase, total_bytes_scanned) on the first oracle hit."""
    total = 0
    n_cand = 0
    tested: set[bytes] = set()
    with open(f"/proc/{pid}/mem", "rb", buffering=0) as mem:
        for start, end, _name in writable_regions(pid):
            pos = start
            carry = b""
            while pos < end and total < MAX_SCAN_BYTES:
                chunk_start = pos
                try:
                    mem.seek(chunk_start)
                    data = mem.read(min(CHUNK, end - chunk_start))
                except OSError:
                    break
                if not data:
                    break
                total += len(data)
                buf = carry + data
                for hex64 in scan_candidates(buf):
                    for form, passphrase in candidate_passphrases(hex64):
                        if passphrase in tested:
                            continue
                        tested.add(passphrase)
                        n_cand += 1
                        if n_cand % 200 == 0:
                            print(f"  ...{n_cand} candidates, {total>>20}MB", flush=True)
                        for page in oracle_pages:
                            if page1_ok(page, passphrase, mode="pass"):
                                return form, passphrase, total
                # keep tail so a candidate spanning the next chunk is caught
                carry = data[-OVERLAP:] if len(data) >= OVERLAP else data
                pos = chunk_start + len(data) - len(carry)
    return None


def wechat_files_from_home() -> "Path":
    ud = HOME / ".config" / "user-dirs.dirs"
    text = ud.read_text(encoding="utf-8", errors="replace") if ud.is_file() else None
    return wechat_files_dir(sys.platform, HOME, text)


def oracle_pages_from_disk() -> list[bytes]:
    pages = []
    for db_storage in find_db_storages(wechat_files_from_home()):
        for db in discover_message_dbs(db_storage)[:2]:
            pages.append(read_page1(str(db)))
        if pages:
            break
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pid", type=int, help="wechat main pid (default: auto)")
    ap.add_argument("--force", action="store_true", help="overwrite existing passphrase")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("run as root: sudo python3 capture_linux.py", file=sys.stderr)
        return 2

    if PASSPHRASE_FILE.exists() and not args.force:
        print(f"already captured: {PASSPHRASE_FILE} (fingerprint {fingerprint(PASSPHRASE_FILE.read_bytes())}); use --force")
        return 0

    pages = oracle_pages_from_disk()
    if not pages:
        print("no message_0.db found under", wechat_files_from_home(), "— log into WeChat first", file=sys.stderr)
        return 3

    pids = [args.pid] if args.pid else find_wechat_pids()
    if not pids:
        print("wechat process not running", file=sys.stderr)
        return 3

    for pid in pids:
        print(f"scanning pid {pid} ({len(writable_regions(pid))} regions)...", flush=True)
        hit = scan_pid(pid, pages)
        if hit:
            form, passphrase, scanned = hit
            write_private(PASSPHRASE_FILE, passphrase)
            os.chmod(PASSPHRASE_FILE, 0o600)
            # when run via sudo, hand ownership back to the desktop user so
            # the (non-root) monitor process can read it
            su = os.environ.get("SUDO_USER")
            if su and su != "root":
                try:
                    pw = pwd.getpwnam(su)
                    os.chown(STATE_DIR, pw.pw_uid, pw.pw_gid)
                    os.chown(PASSPHRASE_FILE, pw.pw_uid, pw.pw_gid)
                except KeyError:
                    pass
            print(
                f"HIT pid={pid} form={form} fingerprint={fingerprint(passphrase)} "
                f"scanned={scanned >> 20}MB -> {PASSPHRASE_FILE}"
            )
            return 0
        print(f"pid {pid}: no candidate passed the page-1 oracle", flush=True)
    print("capture failed: is the account freshly logged in on this process?", file=sys.stderr)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
