"""SQLCipher 4 key derivation and page-1 verification.

Parameters used by WeChat 4.1.x:
  key      = PBKDF2-HMAC-SHA512(passphrase, db_salt[0:16], 256000, 32)
  hmac_key = PBKDF2-HMAC-SHA512(key, db_salt[0:16] XOR 0x3a, 2, 32)
  page     = salt16 | AES-256-CBC(ciphertext) | iv16 | HMAC-SHA512(ct | iv | pgno_le32)

Only hashlib/hmac are used here so the runtime has no crypto dependency.
"""

from __future__ import annotations

import hashlib
import hmac

PAGE_SIZE = 4096
RESERVE = 80
SALT_SIZE = 16
KEY_SIZE = 32
KDF_ITER = 256000
HMAC_KDF_ITER = 2
HMAC_ITER_XOR = 0x3A
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def fingerprint(data: bytes, n: int = 8) -> str:
    """Short SHA-256 prefix safe to print."""
    return hashlib.sha256(data).hexdigest()[:n]


def derive_raw_key(passphrase: bytes, salt: bytes, iterations: int = KDF_ITER) -> bytes:
    if len(passphrase) != KEY_SIZE:
        raise ValueError("passphrase must be 32 bytes")
    if len(salt) != SALT_SIZE:
        raise ValueError("salt must be 16 bytes")
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, iterations, KEY_SIZE)


def derive_hmac_key(raw_key: bytes, salt: bytes) -> bytes:
    if len(raw_key) != KEY_SIZE:
        raise ValueError("raw key must be 32 bytes")
    if len(salt) != SALT_SIZE:
        raise ValueError("salt must be 16 bytes")
    masked = bytes(b ^ HMAC_ITER_XOR for b in salt)
    return hashlib.pbkdf2_hmac("sha512", raw_key, masked, HMAC_KDF_ITER, KEY_SIZE)


def page1_hmac_inputs(page: bytes) -> dict[str, bytes]:
    """Return the candidate HMAC message layouts for page 1.

    SQLCipher's canonical layout is ct16|iv|pgno_le where ct16 starts at
    offset 16 (the 16-byte salt is not part of the HMAC input).
    """
    if len(page) != PAGE_SIZE:
        raise ValueError(f"expected {PAGE_SIZE} byte page, got {len(page)}")
    iv = page[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + 16]
    c16 = page[SALT_SIZE:PAGE_SIZE - RESERVE]
    c0 = page[0:PAGE_SIZE - RESERVE]
    pgno = (1).to_bytes(4, "little")
    pgno_be = (1).to_bytes(4, "big")
    return {
        "ct16+iv+le": c16 + iv + pgno,
        "ct16+iv+be": c16 + iv + pgno_be,
        "ct0+iv+le": c0 + iv + pgno,
    }


def page1_ok(page: bytes, passphrase_or_key: bytes, mode: str = "pass") -> list[str]:
    """Return the HMAC layout names that validate for this page.

    mode='pass': passphrase is the 32-byte SQLCipher passphrase.
    mode='raw':  the argument is already the per-DB 32-byte raw key.
    An empty list means the key/passphrase did not validate.
    """
    if len(page) != PAGE_SIZE:
        raise ValueError(f"expected {PAGE_SIZE} byte page, got {len(page)}")
    salt = page[:SALT_SIZE]
    stored = page[PAGE_SIZE - 64:PAGE_SIZE]
    if mode == "pass":
        raw_key = derive_raw_key(passphrase_or_key, salt)
    elif mode == "raw":
        if len(passphrase_or_key) != KEY_SIZE:
            raise ValueError("raw key must be 32 bytes")
        raw_key = passphrase_or_key
    else:
        raise ValueError("mode must be 'pass' or 'raw'")
    hkey = derive_hmac_key(raw_key, salt)
    hits = []
    for name, message in page1_hmac_inputs(page).items():
        tag = hmac.new(hkey, message, hashlib.sha512).digest()
        if hmac.compare_digest(tag, stored):
            hits.append(name)
    return hits


def read_page1(db_path: str) -> bytes:
    with open(db_path, "rb") as f:
        page = f.read(PAGE_SIZE)
    if len(page) != PAGE_SIZE:
        raise ValueError(f"{db_path}: short read ({len(page)} bytes)")
    return page
