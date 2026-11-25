"""dbverify tests on synthetic sqlcipher fixtures (no real WeChat dbs).

Column names come from the Step 0 measurement of the REAL contact.db /
message_0.db (see task-3 report): contact table exposes `username` and
`remark` directly; Msg_ tables key rows by `local_id` with the payload in
`message_content` read via hex().  The fixture DDL below reuses those exact
names so implementation SQL and tests are anchored to one schema.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dbverify
from dbverify import expected_table, find_remark, postcheck

HAS_SQLCIPHER = shutil.which("sqlcipher") is not None
KEY_HEX = "11" * 32

# Exact measured DDL of the real contact.db `contact` table.
CONTACT_DDL = (
    "CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, "
    "alias TEXT, encrypt_username TEXT, flag INTEGER, delete_flag INTEGER, "
    "verify_flag INTEGER, remark TEXT, remark_quan_pin TEXT, remark_pin_yin_initial TEXT, "
    "nick_name TEXT, pin_yin_initial TEXT, quan_pin TEXT, big_head_url TEXT, "
    "small_head_url TEXT, head_img_md5 TEXT, chat_room_notify INTEGER, "
    "is_in_chat_room INTEGER, description TEXT, extra_buffer BLOB, chat_room_type INTEGER);"
)

# Real Msg_ DDL trimmed to the columns postcheck needs (names verbatim).
MSG_DDL_TMPL = (
    'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY, local_type INTEGER, '
    "create_time INTEGER, message_content BLOB);"
)


def wsql(path: Path, script: str) -> str:
    inp = f'.output /dev/null\nPRAGMA key="x\'{KEY_HEX}\'";\n.output stdout\n{script}\n'
    r = subprocess.run(["sqlcipher", "-batch", str(path)], input=inp,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return r.stdout


def msg_hex(text: str, sender: str = "wxid_sender001") -> str:
    """Real on-disk payload shape: b'<sender>:\\n' + zstd(text), as SQL hex literal bytes."""
    import zstandard as zstd

    payload = zstd.ZstdCompressor().compress(text.encode("utf-8"))
    return (sender.encode("ascii") + b":\n" + payload).hex()


def table_for(ident: str) -> str:
    # Independent recomputation of the md5 rule (mirrors, but does not import,
    # expected_table, so fixtures stay honest when the rule is under test).
    return "Msg_" + hashlib.md5(ident.encode()).hexdigest()


class Scripted:
    """Fake clock/sleep so timeout tests run instantly and poll timing is visible."""

    def __init__(self, times):
        self.times = list(times)
        self.sleeps = []

    def clock(self):
        return self.times.pop(0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class ExpectedTableTest(unittest.TestCase):
    def test_expected_table_md5_rule(self):
        self.assertEqual(
            expected_table("wxid_abc123"),
            "Msg_" + hashlib.md5(b"wxid_abc123").hexdigest(),
        )
        self.assertEqual(len(expected_table("x")), len("Msg_") + 32)


class FindRemarkTest(unittest.TestCase):
    @unittest.skipUnless(HAS_SQLCIPHER, "needs sqlcipher CLI")
    def test_find_remark_unique_ambiguous_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "contact.db"
            wsql(
                p,
                CONTACT_DDL
                + "INSERT INTO contact (username, remark) VALUES "
                "('wxid_u1', '独一备注'),"
                "('wxid_a1', '撞名联系人'),"
                "('wxid_a2', '撞名联系人'),"
                "('wxid_n1', 'O''Brien 备注'),"
                "('wxid_e1', ''),"
                "('wxid_e2', NULL);",
            )
            self.assertEqual(find_remark(p, KEY_HEX, "独一备注"), ["wxid_u1"])
            self.assertEqual(
                sorted(find_remark(p, KEY_HEX, "撞名联系人")), ["wxid_a1", "wxid_a2"]
            )
            self.assertEqual(find_remark(p, KEY_HEX, "查无此人"), [])
            # Remark with an embedded apostrophe must survive SQL quoting.
            self.assertEqual(find_remark(p, KEY_HEX, "O'Brien 备注"), ["wxid_n1"])
            # Empty/whitespace remarks are never a legitimate identity lookup.
            self.assertEqual(find_remark(p, KEY_HEX, ""), [])
            self.assertEqual(find_remark(p, KEY_HEX, "   "), [])


class PostcheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAS_SQLCIPHER:
            raise unittest.SkipTest("needs sqlcipher CLI")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.db = Path(cls.tmp.name) / "message_0.db"
        t_match, t_stale, t_empty = (
            table_for("wxid_match"),
            table_for("wxid_stale"),
            table_for("wxid_empty"),
        )
        script = (
            MSG_DDL_TMPL.format(table=t_match)
            + MSG_DDL_TMPL.format(table=t_stale)
            + MSG_DDL_TMPL.format(table=t_empty)
            + f"INSERT INTO \"{t_match}\" (local_id, local_type, create_time, message_content) VALUES "
            f"(1, 1, 1700000000, x'{msg_hex('旧消息 无关内容')}'),"
            f"(2, 1, 1700000001, x'{msg_hex('测试回执文本，后面是更长的尾巴。')}');"
            + f"INSERT INTO \"{t_stale}\" (local_id, local_type, create_time, message_content) VALUES "
            f"(1, 1, 1700000000, x'{msg_hex('测试回执文本，后面是更长的尾巴。')}'),"
            f"(2, 1, 1700000001, x'{msg_hex('对方刚回了别的话')}');"
        )
        wsql(cls.db, script)
        cls.t_match, cls.t_stale, cls.t_empty = t_match, t_stale, t_empty

    def test_postcheck_true_on_matching_new_row(self):
        s = Scripted([0.0])
        ok = postcheck(
            self.db, KEY_HEX, self.t_match, "测试回执文本",
            timeout_s=4.5, poll_s=1.5, clock=s.clock, sleep=s.sleep,
        )
        self.assertTrue(ok)
        self.assertEqual(s.sleeps, [])  # matched on first poll, never waited

    def test_postcheck_uses_newest_row_only(self):
        # newest row (local_id 2) is NOT the sent text -> older match must not count
        ok = postcheck(
            self.db, KEY_HEX, self.t_stale, "测试回执文本",
            timeout_s=0.0, poll_s=1.5, clock=lambda: 0.0, sleep=lambda _s: None,
        )
        self.assertFalse(ok)

    def test_postcheck_false_on_timeout(self):
        s = Scripted([0.0, 1.5, 3.0, 4.5])
        ok = postcheck(
            self.db, KEY_HEX, self.t_empty, "永远不会出现的文本",
            timeout_s=4.5, poll_s=1.5, clock=s.clock, sleep=s.sleep,
        )
        self.assertFalse(ok)
        self.assertEqual(s.sleeps, [1.5, 1.5])  # polled on schedule, 3 tries
        self.assertEqual(s.times, [])           # clock consumed exactly

    def test_postcheck_polls_through_missing_table(self):
        # A chat table may not exist yet; that is retryable, not a crash.
        s = Scripted([0.0, 1.5, 3.0, 4.5])
        ok = postcheck(
            self.db, KEY_HEX, table_for("wxid_not_created"), "文本",
            timeout_s=4.5, poll_s=1.5, clock=s.clock, sleep=s.sleep,
        )
        self.assertFalse(ok)
        self.assertEqual(s.sleeps, [1.5, 1.5])

    def test_postcheck_raises_on_bad_key(self):
        # Wrong key is NOT retryable (precheck story is find_remark's job;
        # postcheck must not mask a decryption failure as a timeout).
        from sqlcipher_db import SQLCipherError

        with self.assertRaises(SQLCipherError):
            postcheck(
                self.db, "33" * 32, self.t_match, "测试回执文本",
                timeout_s=4.5, poll_s=1.5, clock=lambda: 0.0, sleep=lambda _s: None,
            )

    def test_postcheck_rejects_empty_sent_text(self):
        with self.assertRaises(ValueError):
            postcheck(self.db, KEY_HEX, self.t_match, "   ",
                      clock=lambda: 0.0, sleep=lambda _s: None)

    def test_postcheck_after_id_requires_growth(self):
        # Final-review Important #1: same-prefix stale newest row (boilerplate
        # head / exit-3 retry where attempt 1 delivered). Text matches but
        # local_id 2 <= baseline 2 -> must NOT count as a receipt.
        ok = postcheck(
            self.db, KEY_HEX, self.t_match, "测试回执文本",
            after_id=2, timeout_s=0.0, poll_s=1.5,
            clock=lambda: 0.0, sleep=lambda _s: None,
        )
        self.assertFalse(ok)

    def test_postcheck_after_id_matches_newer_row(self):
        s = Scripted([0.0])
        ok = postcheck(
            self.db, KEY_HEX, self.t_match, "测试回执文本",
            after_id=1, timeout_s=4.5, poll_s=1.5, clock=s.clock, sleep=s.sleep,
        )
        self.assertTrue(ok)
        self.assertEqual(s.sleeps, [])  # row 2 > baseline 1 matched first poll

    def test_postcheck_after_id_none_keeps_old_behavior(self):
        ok = postcheck(
            self.db, KEY_HEX, self.t_match, "测试回执文本",
            after_id=None, timeout_s=0.0, poll_s=1.5,
            clock=lambda: 0.0, sleep=lambda _s: None,
        )
        self.assertTrue(ok)

    def test_postcheck_after_id_waits_for_new_row(self):
        # Baseline 1 with a same-prefix stale row: receipt only once a NEW row
        # (local_id 2 > 1) lands during polling.
        probe = self.db.with_name("growth.db")
        wsql(
            probe,
            MSG_DDL_TMPL.format(table="Msg_g")
            + f"INSERT INTO \"Msg_g\" (local_id, local_type, create_time, message_content) VALUES "
            f"(1, 1, 1, x'{msg_hex('测试回执文本，后面是更长的尾巴。')}');",
        )
        s = Scripted([0.0, 1.5])

        def sleep_then_insert(seconds):
            s.sleep(seconds)
            wsql(
                probe,
                f"INSERT INTO \"Msg_g\" (local_id, local_type, create_time, message_content) VALUES "
                f"(2, 1, 2, x'{msg_hex('测试回执文本 新落库')}');",
            )

        ok = postcheck(
            probe, KEY_HEX, "Msg_g", "测试回执文本",
            after_id=1, timeout_s=4.5, poll_s=1.5,
            clock=s.clock, sleep=sleep_then_insert,
        )
        self.assertTrue(ok)
        self.assertEqual(s.sleeps, [1.5])
        self.assertEqual(s.times, [])

    def test_postcheck_80_char_prefix_window(self):
        long_text = "标" * 100
        s = Scripted([0.0])
        # sent_text longer than 80 chars must still match against the row whose
        # stored text starts with the first 80 chars.
        script = (
            MSG_DDL_TMPL.format(table="Msg_probe")
            + f"INSERT INTO \"Msg_probe\" (local_id, local_type, create_time, message_content) VALUES "
            f"(1, 1, 1, x'{msg_hex(long_text)}');"
        )
        probe = self.db.with_name("probe.db")
        wsql(probe, script)
        ok = postcheck(
            probe, KEY_HEX, "Msg_probe", long_text,
            timeout_s=4.5, poll_s=1.5, clock=s.clock, sleep=s.sleep,
        )
        self.assertTrue(ok)


class LatestLocalIdTest(unittest.TestCase):
    """Pre-GUI baseline probe (final-review Important #1/#2): read-only
    max(local_id) of the target table; SQLCipherError propagates so wxsend
    can REFUSE (exit 2) on a stale message key for EVERY target kind."""

    @classmethod
    def setUpClass(cls):
        if not HAS_SQLCIPHER:
            raise unittest.SkipTest("needs sqlcipher CLI")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.db = Path(cls.tmp.name) / "message_0.db"
        cls.t = table_for("wxid_base")
        wsql(
            cls.db,
            MSG_DDL_TMPL.format(table=cls.t)
            + MSG_DDL_TMPL.format(table="Msg_empty")
            + f"INSERT INTO \"{cls.t}\" (local_id, local_type, create_time, message_content) VALUES "
            f"(1, 1, 1, x'{msg_hex('甲')}'),"
            f"(5, 1, 2, x'{msg_hex('乙')}');",
        )

    def test_returns_max_local_id(self):
        self.assertEqual(dbverify.latest_local_id(self.db, KEY_HEX, self.t), 5)

    def test_empty_table_returns_none(self):
        self.assertIsNone(dbverify.latest_local_id(self.db, KEY_HEX, "Msg_empty"))

    def test_missing_table_returns_none(self):
        # A brand-new conversation has no Msg_ table yet: baseline None,
        # postcheck then keeps its old (no-growth-requirement) behavior.
        self.assertIsNone(
            dbverify.latest_local_id(self.db, KEY_HEX, table_for("wxid_never")))

    def test_bad_key_raises_sqlciphererror(self):
        from sqlcipher_db import SQLCipherError

        with self.assertRaises(SQLCipherError):
            dbverify.latest_local_id(self.db, "33" * 32, self.t)


if __name__ == "__main__":
    unittest.main()
