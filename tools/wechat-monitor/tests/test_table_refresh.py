"""Monitor must notice Msg_ tables created AFTER startup (new chats).

Uses the real sqlcipher CLI on a real (unencrypted) sqlite db for the
discovery layer; asserts the pure diff logic of refresh_tables.
"""
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import Table, REQUIRED_COLUMNS, refresh_tables


HAS_SQLCIPHER = shutil.which("sqlcipher") is not None


KEY_HEX = "11" * 32


def wsql(path: Path, script: str) -> str:
    inp = f'.output /dev/null\nPRAGMA key="x\'{KEY_HEX}\'";\n.output stdout\n{script}\n'
    r = subprocess.run(["sqlcipher", "-batch", str(path)], input=inp,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return r.stdout


class RefreshTablesTest(unittest.TestCase):
    @unittest.skipUnless(HAS_SQLCIPHER, "needs sqlcipher CLI")
    def test_new_table_detected_and_known_stable(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "message_0.db"
            wsql(p, "CREATE TABLE Msg_aaa (local_id INTEGER PRIMARY KEY, server_id TEXT, "
                    "local_type INTEGER, create_time INTEGER, message_content BLOB);"
                    "INSERT INTO Msg_aaa VALUES (5,'s',1,1700000000,x'08');")

            class FakeDB:
                pass
            db = FakeDB()
            db.path, db.key = p, bytes.fromhex(KEY_HEX)
            tables, _ = refresh_tables(db)
            self.assertEqual([t.name for t in tables], ["Msg_aaa"])

            # simulate WeChat creating a new chat table
            wsql(p, "CREATE TABLE Msg_bbb (local_id INTEGER PRIMARY KEY, server_id TEXT, "
                    "local_type INTEGER, create_time INTEGER, message_content BLOB);"
                    "INSERT INTO Msg_bbb VALUES (1,'s',1,1700000001,x'08');")

            tables2, added = refresh_tables(db, known=tables)
            names = sorted(t.name for t in tables2)
            self.assertEqual(names, ["Msg_aaa", "Msg_bbb"])
            self.assertEqual([t.name for t in added], ["Msg_bbb"])

    @unittest.skipUnless(HAS_SQLCIPHER, "needs sqlcipher CLI")
    def test_bad_schema_table_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "message_0.db"
            wsql(p, "CREATE TABLE Msg_aaa (local_id INTEGER PRIMARY KEY, server_id TEXT, "
                    "local_type INTEGER, create_time INTEGER, message_content BLOB);")
            wsql(p, "CREATE TABLE Msg_ccc (local_id INTEGER, nonsense TEXT);")

            class FakeDB:
                pass
            db = FakeDB()
            db.path, db.key = p, bytes.fromhex(KEY_HEX)
            tables, added = refresh_tables(db, known=[])
            self.assertEqual([t.name for t in tables], ["Msg_aaa"])
            self.assertNotIn("Msg_ccc", [t.name for t in added])


if __name__ == "__main__":
    unittest.main()
