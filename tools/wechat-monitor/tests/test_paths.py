import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paths import parse_xdg_documents, wechat_files_dir


class XdgParseTest(unittest.TestCase):
    def test_dollar_home_expanded(self):
        text = 'XDG_DOCUMENTS_DIR="$HOME/文档"\nXDG_DOWNLOAD_DIR="$HOME/下载"\n'
        self.assertEqual(
            parse_xdg_documents(text, Path("/home/u")), Path("/home/u/文档")
        )

    def test_absolute_value_used_verbatim(self):
        text = 'XDG_DOCUMENTS_DIR="/data/Documents"\n'
        self.assertEqual(
            parse_xdg_documents(text, Path("/home/u")), Path("/data/Documents")
        )

    def test_missing_key_returns_none(self):
        self.assertIsNone(parse_xdg_documents('XDG_DOWNLOAD_DIR="$HOME/x"\n', Path("/h")))

    def test_none_content_returns_none(self):
        self.assertIsNone(parse_xdg_documents(None, Path("/h")))


class WechatFilesDirTest(unittest.TestCase):
    def test_darwin_container_path(self):
        got = wechat_files_dir("darwin", Path("/Users/bob"), None)
        self.assertEqual(
            got,
            Path(
                "/Users/bob/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
            ),
        )

    def test_linux_uses_xdg_documents(self):
        text = 'XDG_DOCUMENTS_DIR="$HOME/文档"\n'
        got = wechat_files_dir("linux", Path("/home/dgkj"), text)
        self.assertEqual(got, Path("/home/dgkj/文档/xwechat_files"))

    def test_linux_fallback_without_xdg(self):
        got = wechat_files_dir("linux", Path("/home/dgkj"), None)
        self.assertEqual(got, Path("/home/dgkj/Documents/xwechat_files"))

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            wechat_files_dir("win32", Path("C:/x"), None)


if __name__ == "__main__":
    unittest.main()
