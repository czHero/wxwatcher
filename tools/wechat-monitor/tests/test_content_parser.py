import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zstandard as zstd

from content_parser import is_text_type, parse_content


class ContentParserTest(unittest.TestCase):
    def test_prefix(self):
        parsed = parse_content(b"wxid_abc:\nhello world")
        self.assertEqual(parsed.sender_id, "wxid_abc")
        self.assertEqual(parsed.text, "hello world")
        self.assertFalse(parsed.decompressed)
        self.assertFalse(parsed.binary)

    def test_no_prefix(self):
        parsed = parse_content(b"hello world")
        self.assertIsNone(parsed.sender_id)
        self.assertEqual(parsed.text, "hello world")

    def test_zstd_payload(self):
        payload = zstd.ZstdCompressor().compress("压缩文本".encode("utf-8"))
        parsed = parse_content(b"wxid_abc:\n" + payload)
        self.assertEqual(parsed.sender_id, "wxid_abc")
        self.assertEqual(parsed.text, "压缩文本")
        self.assertTrue(parsed.decompressed)

    def test_invalid_zstd_is_binary(self):
        parsed = parse_content(b"wxid_abc:\n\x28\xb5\x2f\xfdnot-a-frame")
        self.assertIsNone(parsed.text)
        self.assertTrue(parsed.binary)

    def test_binary_payload(self):
        parsed = parse_content(b"wxid_abc:\n\x00\x01\x02")
        self.assertIsNone(parsed.text)
        self.assertTrue(parsed.binary)

    def test_text_type_mask(self):
        self.assertTrue(is_text_type(1))
        self.assertTrue(is_text_type(0x100000001))
        self.assertFalse(is_text_type(3))
        self.assertFalse(is_text_type(0x100000003))


if __name__ == "__main__":
    unittest.main()
