import hashlib
import hmac
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from crypto import PAGE_SIZE, RESERVE, derive_hmac_key, derive_raw_key, page1_ok


def build_page(passphrase: bytes) -> tuple[bytes, bytes]:
    salt = os.urandom(16)
    iv = os.urandom(16)
    plaintext = os.urandom(PAGE_SIZE - RESERVE - 16)
    key = derive_raw_key(passphrase, salt)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    hkey = derive_hmac_key(key, salt)
    tag = hmac.new(
        hkey,
        ciphertext + iv + (1).to_bytes(4, "little"),
        hashlib.sha512,
    ).digest()
    page = salt + ciphertext + iv + tag
    assert len(page) == PAGE_SIZE
    return page, key


class Page1OracleTest(unittest.TestCase):
    def test_passphrase_and_raw_key_positive(self):
        passphrase = os.urandom(32)
        page, key = build_page(passphrase)
        self.assertIn("ct16+iv+le", page1_ok(page, passphrase, mode="pass"))
        self.assertIn("ct16+iv+le", page1_ok(page, key, mode="raw"))

    def test_wrong_key_rejected(self):
        passphrase = os.urandom(32)
        page, _ = build_page(passphrase)
        self.assertEqual(page1_ok(page, os.urandom(32), mode="pass"), [])
        self.assertEqual(page1_ok(page, os.urandom(32), mode="raw"), [])

    def test_short_input_rejected(self):
        with self.assertRaises(ValueError):
            page1_ok(b"\x00" * 100, b"\x00" * 32, mode="raw")


if __name__ == "__main__":
    unittest.main()
