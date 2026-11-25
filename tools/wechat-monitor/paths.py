"""Platform-aware discovery of the WeChat xwechat_files directory.

macOS: sandboxed container under ~/Library/Containers.
Linux: <Documents>/xwechat_files, where Documents comes from
~/.config/user-dirs.dirs (XDG_DOCUMENTS_DIR), falling back to ~/Documents.
The Linux native client passes this path to WeChatAppEx as
`--wechat-files-path`, and it lives under the localized Documents dir.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_MAC_SUBPATH = Path(
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
)


def parse_xdg_documents(userdirs_text: str | None, home: Path) -> Path | None:
    """Resolve XDG_DOCUMENTS_DIR from user-dirs.dirs content."""
    if not userdirs_text:
        return None
    m = re.search(r'^XDG_DOCUMENTS_DIR="?([^"\n]+)"?', userdirs_text, re.M)
    if not m:
        return None
    value = m.group(1).replace("$HOME", str(home))
    return Path(value)


def wechat_files_dir(
    platform: str, home: Path, userdirs_text: str | None
) -> Path:
    if platform == "darwin":
        return home / _MAC_SUBPATH
    if platform.startswith("linux"):
        documents = parse_xdg_documents(userdirs_text, home) or (home / "Documents")
        return documents / "xwechat_files"
    raise ValueError(f"unsupported platform: {platform}")


def default_wechat_files() -> Path:
    text = None
    ud = Path.home() / ".config" / "user-dirs.dirs"
    if ud.is_file():
        try:
            text = ud.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None
    return wechat_files_dir(sys.platform, Path.home(), text)
