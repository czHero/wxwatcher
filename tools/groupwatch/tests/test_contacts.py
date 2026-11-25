"""contacts.py 群名解析器单测：假 monitor 注入，绝不碰真 DB/子进程/网络。

契约（accept-fix-names.md §4）：
- run_sqlcipher 返回的行按 {username: nick_name} 收集，nick_name 空白剔除；
- key 为 bytes 与 hex-str 都接受，bytes 走 .hex() 归一；
- 传给 run_sqlcipher 的三元组 = (db_storage/contact/contact.db, key_hex,
  含 '@chatroom' 与 'nick_name' 的 SELECT)；
- 任何异常 → {}（永不冒泡）；无 key → {} 且不调 run_sqlcipher；
- monitor 有 quote_ident 则标识符加引号，没有也能跑。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contacts

_KEY_BYTES = bytes(range(32))
_KEY_HEX = _KEY_BYTES.hex()


def _fake_monitor(rows=None, raises=None):
    """记录 (db, key, sql) 三元组的假 monitor；raises 则 run_sqlcipher 抛出。"""
    calls = []

    def run_sqlcipher(db, key_hex, sql, **kw):
        calls.append((db, key_hex, sql))
        if raises is not None:
            raise raises
        return rows if rows is not None else []

    def quote_ident(name):
        return '"' + name.replace('"', '""') + '"'

    return SimpleNamespace(run_sqlcipher=run_sqlcipher,
                           quote_ident=quote_ident, calls=calls)


class ChatroomNamesTest(unittest.TestCase):
    def setUp(self):
        self.db_storage = Path("/tmp/fake-db-storage")
        self.contact_db = self.db_storage / "contact" / "contact.db"

    def _keys(self, key):
        return {str(self.contact_db): key}

    def test_rows_collected_blanks_excluded(self):
        rows = [
            {"username": "19927187460@chatroom", "nick_name": "family"},
            {"username": "g2@chatroom", "nick_name": "   "},
            {"username": "g3@chatroom", "nick_name": ""},
            {"username": "g4@chatroom", "nick_name": None},
            {"username": "g5@chatroom", "nick_name": " 作业群 "},
        ]
        mon = _fake_monitor(rows=rows)
        out = contacts.chatroom_names(mon, self.db_storage,
                                       self._keys(_KEY_BYTES))
        self.assertEqual(out, {"19927187460@chatroom": "family",
                               "g5@chatroom": "作业群"})

    def test_call_triple_db_key_sql(self):
        mon = _fake_monitor(rows=[])
        contacts.chatroom_names(mon, self.db_storage, self._keys(_KEY_BYTES))
        self.assertEqual(len(mon.calls), 1)
        db, key_hex, sql = mon.calls[0]
        self.assertEqual(Path(db), self.contact_db)
        self.assertEqual(key_hex, _KEY_HEX)  # bytes → .hex() 归一
        self.assertIn("@chatroom", sql)
        self.assertIn("nick_name", sql)
        self.assertIn("username", sql)
        self.assertIn("contact", sql)

    def test_hex_str_key_accepted_as_is(self):
        mon = _fake_monitor(rows=[{"username": "x@chatroom",
                                   "nick_name": "X 群"}])
        out = contacts.chatroom_names(mon, self.db_storage,
                                      self._keys(_KEY_HEX))
        self.assertEqual(out, {"x@chatroom": "X 群"})
        self.assertEqual(mon.calls[0][1], _KEY_HEX)

    def test_run_sqlcipher_raises_returns_empty(self):
        mon = _fake_monitor(raises=RuntimeError("decrypt boom"))
        out = contacts.chatroom_names(mon, self.db_storage,
                                      self._keys(_KEY_BYTES))
        self.assertEqual(out, {})  # 绝不冒泡

    def test_no_key_returns_empty_without_query(self):
        mon = _fake_monitor(rows=[{"username": "x@chatroom",
                                   "nick_name": "X 群"}])
        out = contacts.chatroom_names(mon, self.db_storage, {})
        self.assertEqual(out, {})
        self.assertEqual(mon.calls, [])

    def test_monitor_without_quote_ident_still_works(self):
        rows = [{"username": "x@chatroom", "nick_name": "X 群"}]
        calls = []

        def run_sqlcipher(db, key_hex, sql, **kw):
            calls.append(sql)
            return rows

        mon = SimpleNamespace(run_sqlcipher=run_sqlcipher)
        out = contacts.chatroom_names(mon, self.db_storage,
                                      self._keys(_KEY_BYTES))
        self.assertEqual(out, {"x@chatroom": "X 群"})
        self.assertIn("nick_name", calls[0])

    def test_quote_ident_used_when_available(self):
        mon = _fake_monitor(rows=[])
        contacts.chatroom_names(mon, self.db_storage, self._keys(_KEY_BYTES))
        self.assertIn('"nick_name"', mon.calls[0][2])
        self.assertIn('"contact"', mon.calls[0][2])


if __name__ == "__main__":
    unittest.main()
