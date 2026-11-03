"""Durable image identity for a controller job."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from ermik import state


IMAGE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


class ImageStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _insert_job(self):
        with state.connect(self.root) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(1,'work/issue-1-test',?,'running','[]')", ("a" * 40,))

    def test_fresh_job_binds_once_and_survives_reconnect(self):
        self._insert_job()
        with state.connect(self.root) as conn:
            self.assertIsNone(state.job(conn, 1)["image_id"])
            self.assertEqual(state.bind_image(conn, 1, IMAGE), IMAGE)
            self.assertEqual(state.bind_image(conn, 1, IMAGE), IMAGE)
        with state.connect(self.root) as conn:
            self.assertEqual(state.job(conn, 1)["image_id"], IMAGE)
            with self.assertRaisesRegex(ValueError, "changed"):
                state.bind_image(conn, 1, OTHER)
            self.assertEqual(state.job(conn, 1)["image_id"], IMAGE)

    def test_rejects_invalid_identity_and_missing_job(self):
        self._insert_job()
        for invalid in ("demo:latest", "sha256:" + "A" * 64,
                        "sha256:" + "a" * 63, None):
            with self.subTest(invalid=invalid), state.connect(self.root) as conn:
                with self.assertRaisesRegex(ValueError, "invalid"):
                    state.bind_image(conn, 1, invalid)
                self.assertIsNone(state.job(conn, 1)["image_id"])
        with state.connect(self.root) as conn:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                state.bind_image(conn, 2, IMAGE)

    def test_legacy_database_gains_nullable_image_column(self):
        db = self.root / "state.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE jobs (issue INTEGER PRIMARY KEY, branch TEXT, "
                         "base_sha TEXT, status TEXT, allowed_json TEXT)")
            conn.execute("INSERT INTO jobs VALUES(1,'work/issue-1-test',?,'running','[]')",
                         ("a" * 40,))
            conn.commit()
        state.initialize(self.root)
        with state.connect(self.root) as conn:
            self.assertIsNone(state.job(conn, 1)["image_id"])
            state.bind_image(conn, 1, IMAGE)
        state.initialize(self.root)
        with state.connect(self.root) as conn:
            self.assertEqual(state.job(conn, 1)["image_id"], IMAGE)


if __name__ == "__main__":
    unittest.main()
