"""Memory-scan passphrase extraction: 64-hex runs -> page-1 HMAC oracle.

Pure logic lives here so it is unit-testable on synthetic pages; the
/proc reader and CLI wrapper live in `capture_linux.py`.
"""

from __future__ import annotations

import re

from crypto import page1_ok

_HEX_RUN = re.compile(rb"[0-9a-fA-F]+")


def extract_hex64(buf: bytes) -> list[str]:
    """Return deduplicated, lowercase hex strings of exactly 64 chars.

    Only maximal runs bounded by non-hex bytes count; longer/shorter runs
    are noise and rejected.  First-appearance order is preserved.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _HEX_RUN.finditer(buf):
        if m.end() - m.start() != 64:
            continue
        s = m.group().decode("ascii").lower()
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def candidate_passphrases(hex64: str) -> list[tuple[str, bytes]]:
    """32-byte passphrase interpretations of a 64-char hex run."""
    return [
        ("raw32", bytes.fromhex(hex64)),
        ("ascii32", hex64[:32].encode("ascii")),
    ]


# safety cap: windows taken from a single over-64 hex run
_MAX_WINDOWS_PER_RUN = 4096


def scan_candidates(buf: bytes) -> list[str]:
    """Unique 64-char lowercase hex candidates, best guesses first.

    Exact bounded runs (maximal hex string is exactly 64 chars) are the
    likely passphrase shape and are yielded before 64-char windows taken
    from inside longer hex runs.
    """
    bounded: list[str] = []
    embedded: list[str] = []
    seen: set[str] = set()
    for m in _HEX_RUN.finditer(buf):
        run = m.group()
        if len(run) == 64:
            s = run.decode("ascii").lower()
            if s not in seen:
                seen.add(s)
                bounded.append(s)
        elif len(run) > 64:
            for j in range(min(len(run) - 63, _MAX_WINDOWS_PER_RUN)):
                s = run[j : j + 64].decode("ascii").lower()
                if s not in seen:
                    seen.add(s)
                    embedded.append(s)
    return bounded + embedded


def find_passphrase(buf: bytes, page: bytes) -> tuple[str, bytes] | None:
    """First (form, passphrase) from buf whose PBKDF2 key passes page-1 HMAC."""
    for hex64 in scan_candidates(buf):
        for form, passphrase in candidate_passphrases(hex64):
            if page1_ok(page, passphrase, mode="pass"):
                return form, passphrase
    return None
