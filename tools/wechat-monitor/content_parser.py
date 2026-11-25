"""Parse WeChat message_content payloads.

Observed/lead format: b"<sender_wxid>:\\n<payload>".  A payload may be a
zstd frame (magic 28 b5 2f fd).  Only plain text (local_type & 0xffffffff == 1)
is emitted by the monitor.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - runtime dependency
    zstd = None

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_DECOMPRESSED = 1 << 20  # 1 MiB


@dataclass
class ParsedContent:
    sender_id: str | None
    text: str | None
    decompressed: bool
    binary: bool


def is_text_type(local_type: int) -> bool:
    return (int(local_type) & 0xFFFFFFFF) == 1


def maybe_decompress(data: bytes) -> tuple[bytes, bool]:
    if not data.startswith(ZSTD_MAGIC):
        return data, False
    if zstd is None:
        raise RuntimeError("zstandard is required to decompress this message")
    dctx = zstd.ZstdDecompressor()
    with dctx.stream_reader(io.BytesIO(data)) as reader:
        out = reader.read(MAX_DECOMPRESSED + 1)
    if len(out) > MAX_DECOMPRESSED:
        raise ValueError("decompressed message exceeds 1 MiB")
    return out, True


def _decode_text(data: bytes) -> tuple[str | None, bool]:
    if b"\x00" in data:
        return None, True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, True
    return text, False


def parse_content(content: bytes) -> ParsedContent:
    """Split the optional sender prefix and decompress a zstd payload."""
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("content must be bytes")
    raw = bytes(content)
    sender = None
    payload = raw
    sep = raw.find(b":\n")
    if 0 < sep <= 128:
        prefix = raw[:sep]
        # wxid / group ids are ASCII and never contain whitespace or NUL.
        if prefix and all(32 <= b < 127 for b in prefix) and b" " not in prefix:
            sender = prefix.decode("ascii", errors="replace")
            payload = raw[sep + 2 :]

    decompressed = False
    try:
        payload, decompressed = maybe_decompress(payload)
    except Exception:
        # A zstd frame that fails to decode is not text we can trust.
        return ParsedContent(sender_id=sender, text=None, decompressed=False, binary=True)

    text, binary = _decode_text(payload)
    if text is not None:
        text = text.rstrip("\x00")
    return ParsedContent(
        sender_id=sender,
        text=text,
        decompressed=decompressed,
        binary=binary,
    )
