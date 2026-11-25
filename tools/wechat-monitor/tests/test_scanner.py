"""Tests for the memory-scan passphrase extractor (Linux capture path).

Real crypto, synthetic pages: build a page-1 for a known passphrase, plant
its hex form in a fake memory buffer, and require the scanner to recover it.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from crypto import PAGE_SIZE, RESERVE, derive_hmac_key, derive_raw_key
from scanner import candidate_passphrases, extract_hex64, find_passphrase


def build_page(passphrase: bytes) -> bytes:
    salt = os.urandom(16)
    iv = os.urandom(16)
    plaintext = os.urandom(PAGE_SIZE - RESERVE - 16)
    key = derive_raw_key(passphrase, salt)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    hkey = derive_hmac_key(key, salt)
    import hmac as _hmac
    import hashlib as _hashlib

    tag = _hmac.new(
        hkey, ciphertext + iv + (1).to_bytes(4, "little"), _hashlib.sha512
    ).digest()
    return (salt + ciphertext + iv + tag)[:PAGE_SIZE]


class ExtractHex64Test(unittest.TestCase):
    def test_finds_planted_bounded_string(self):
        secret = os.urandom(32).hex()
        # '|' delimiters keep the planted run exactly 64 long regardless
        # of which noise bytes abut it
        buf = b"|" + os.urandom(200) + b"|" + secret.encode() + b"|" + os.urandom(200) + b"|"
        self.assertEqual(extract_hex64(buf), [secret])

    def test_dedups_repeats_preserves_order(self):
        a = "ab" * 32
        b = "cd" * 32
        buf = b"|" + b.encode() + b"|" + a.encode() + b"|sep|" + a.encode() + b"|"
        self.assertEqual(extract_hex64(buf), [b, a])

    def test_rejects_wrong_length_runs(self):
        buf = b"x" + ("0" * 63).encode() + b"y" + ("0" * 65).encode() + b"z"
        self.assertEqual(extract_hex64(buf), [])

    def test_uppercase_normalized(self):
        secret = os.urandom(32).hex().upper()
        buf = b" " + secret.encode() + b" "
        self.assertEqual(extract_hex64(buf), [secret.lower()])


class CandidatePassphrasesTest(unittest.TestCase):
    def test_two_thirtytwo_byte_forms(self):
        h = "0123456789abcdef" * 4
        got = dict(candidate_passphrases(h))
        self.assertEqual(got["raw32"], bytes.fromhex(h))
        self.assertEqual(got["ascii32"], h[:32].encode())
        self.assertTrue(all(len(v) == 32 for v in got.values()))


class FindPassphraseTest(unittest.TestCase):
    def test_recovers_hex_encoded_passphrase(self):
        passphrase = os.urandom(32)
        page = build_page(passphrase)
        noise = os.urandom(4096)
        buf = b"|" + passphrase.hex().encode() + b"|" + noise
        found = find_passphrase(buf, page)
        self.assertIsNotNone(found)
        form, recovered = found
        self.assertEqual((form, recovered), ("raw32", passphrase))

    def test_recovers_ascii32_passphrase(self):
        # true passphrase is 32 printable hex chars; in memory it sits as an
        # exactly-64-hex run whose first half is the passphrase
        passphrase = "0123456789abcdef" * 2
        suffix = os.urandom(16).hex()
        page = build_page(passphrase.encode())
        buf = b"head " + (passphrase + suffix).encode() + b" tail"
        found = find_passphrase(buf, page)
        self.assertIsNotNone(found)
        form, recovered = found
        self.assertEqual((form, recovered), ("ascii32", passphrase.encode()))

    def test_returns_none_when_buffer_has_no_key(self):
        passphrase = os.urandom(32)
        page = build_page(passphrase)
        buf = os.urandom(8192)
        self.assertIsNone(find_passphrase(buf, page))

    def test_finds_passphrase_embedded_in_longer_hexrun(self):
        # a 64-char passphrase sitting in the middle of a 100-char hex run
        passphrase = os.urandom(32)
        page = build_page(passphrase)
        hex64 = passphrase.hex().encode()
        run = os.urandom(18).hex().encode() + hex64 + os.urandom(18).hex().encode()
        self.assertGreater(len(run), 64)
        buf = b"|" + run + b"|"
        found = find_passphrase(buf, page)
        self.assertIsNotNone(found)
        self.assertEqual((found[0], found[1]), ("raw32", passphrase))

    def test_prefers_bounded_runs_over_embedded_windows(self):
        # scan_candidates yields exact-bounded hex before embedded windows
        from scanner import scan_candidates
        embedded = "a" * 80  # one long run
        bounded = "b" * 64
        buf = embedded.encode() + b"|" + bounded.encode()
        got = scan_candidates(buf)
        self.assertEqual(got[0], "b" * 64)  # bounded first
        self.assertIn("a" * 64, got)  # embedded window present later


if __name__ == "__main__":
    unittest.main()
