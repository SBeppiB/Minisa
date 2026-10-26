import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.db import Database


class DatabaseConnectionTests(unittest.TestCase):
    def test_context_manager_commits_and_closes_connection(self):
        with tempfile.TemporaryDirectory(prefix="minisa-db-close-") as td:
            db = Database(Path(td) / "state.db")
            db.initialize()
            with db.connect() as conn:
                conn.execute("CREATE TABLE close_check(value TEXT)")
                conn.execute("INSERT INTO close_check VALUES ('committed')")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            with db.connect(readonly=True) as reader:
                self.assertEqual(reader.execute("SELECT value FROM close_check").fetchone()[0], "committed")
            with self.assertRaises(sqlite3.ProgrammingError):
                reader.execute("SELECT 1")

    def test_context_manager_rolls_back_and_closes_on_exception(self):
        with tempfile.TemporaryDirectory(prefix="minisa-db-close-rollback-") as td:
            db = Database(Path(td) / "state.db")
            db.initialize()
            with self.assertRaisesRegex(ValueError, "stop"):
                with db.connect() as conn:
                    conn.execute("BEGIN")
                    conn.execute("CREATE TABLE rollback_check(value TEXT)")
                    raise ValueError("stop")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            with db.connect(readonly=True) as reader:
                self.assertIsNone(reader.execute("SELECT name FROM sqlite_master WHERE name='rollback_check'").fetchone())


if __name__ == "__main__":
    unittest.main()
