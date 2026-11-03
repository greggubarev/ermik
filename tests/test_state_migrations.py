"""Upgrade and restore tests using the populated preview state format."""

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ermik import state


# Frozen v0.1.0-preview.1 ledger, deliberately independent of state.SCHEMA.
PREVIEW_SCHEMA = """
CREATE TABLE admissions (
  issue INTEGER PRIMARY KEY, body_sha TEXT NOT NULL,
  allowed_json TEXT NOT NULL, admitted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE jobs (
  issue INTEGER PRIMARY KEY, branch TEXT NOT NULL UNIQUE,
  base_sha TEXT NOT NULL, head_sha TEXT, pr INTEGER,
  status TEXT NOT NULL, allowed_json TEXT NOT NULL,
  last_request INTEGER NOT NULL DEFAULT 0,
  error TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_model TEXT, result_model TEXT, attempts_count INTEGER NOT NULL DEFAULT 0,
  review_next_check_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE retry_state (
  issue INTEGER PRIMARY KEY REFERENCES jobs(issue),
  attempts INTEGER NOT NULL CHECK(attempts >= 1),
  category TEXT NOT NULL,
  resume_status TEXT NOT NULL CHECK(resume_status IN ('running','correcting')),
  next_retry_at INTEGER,
  lease_token TEXT,
  lease_expires_at INTEGER,
  failure_id TEXT NOT NULL,
  CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue INTEGER NOT NULL,
  kind TEXT NOT NULL,
  model TEXT,
  head_sha TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX events_issue_id ON events(issue,id);
CREATE TABLE usage_events (
  attempt_id TEXT PRIMARY KEY,
  issue INTEGER NOT NULL,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  complete INTEGER NOT NULL CHECK(complete IN (0,1)),
  input_tokens INTEGER,
  output_tokens INTEGER,
  reasoning_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  total_tokens INTEGER,
  estimated_cost REAL,
  result_code INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX usage_events_issue ON usage_events(issue,created_at);
"""

REVIEW = {"pr": 104, "issue": 7, "head": "c" * 40,
          "base": "a" * 40, "decision": "reviewed", "image_id": "sha256:" + "d" * 64}
PUBLICATION = {"issue": 7, "branch": "work/issue-7-preview",
               "head": "c" * 40, "base": "a" * 40,
               "ci": {"result": "passed", "image_id": "sha256:" + "d" * 64}}


def create_preview(root: Path, *, with_image: bool = False) -> Path:
    root.mkdir(parents=True)
    db = root / "state.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(PREVIEW_SCHEMA)
        if with_image:
            conn.execute("ALTER TABLE jobs ADD COLUMN image_id TEXT")
        conn.execute("INSERT INTO admissions VALUES(7,?,?,?)",
                     ("f" * 64, '["src/worker.py"]', "2026-01-02 03:04:05"))
        job_values = (7, "work/issue-7-preview", "a" * 40, "c" * 40, 104,
                      "retry_wait", '["src/worker.py"]', 2, "429 quota",
                      "2026-01-03 04:05:06", "provider/first", "provider/second",
                      3, 1735900000)
        if with_image:
            job_values += ("sha256:" + "d" * 64,)
        conn.execute("INSERT INTO jobs VALUES(" + ",".join("?" for _ in job_values) + ")",
                     job_values)
        conn.execute("INSERT INTO retry_state VALUES(?,?,?,?,?,?,?,?)",
                     (7, 2, "rate_limit", "correcting", 1735900100,
                      "lease-7", 1735900200, "failure-7"))
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                     (41, 7, "checks_passed", "provider/second", "c" * 40,
                      "2026-01-03 04:05:00"))
        conn.execute("INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("attempt-7-2", 7, "provider", "second", 1, 1234, 321,
                      20, 50, 10, 1555, 0.042, 0, "2026-01-03 04:04:00"))
        conn.commit()
    for relative, value in (("reviews/pr-104.json", REVIEW),
                            ("evidence/7/" + "c" * 40 + ".json", PUBLICATION)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    return db


def ledger(root: Path) -> dict:
    with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
        return {name: conn.execute(
                    "SELECT " + ("issue,branch,base_sha,head_sha,pr,status,allowed_json,"
                                 "last_request,error,updated_at,last_model,result_model,"
                                 "attempts_count,review_next_check_at" if name == "jobs" else "*")
                    + f" FROM {name} ORDER BY 1").fetchall()
                for name in ("admissions", "jobs", "retry_state", "events",
                             "usage_events")}


def evidence(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes()
            for directory in (root / "reviews", root / "evidence")
            if directory.exists() for path in directory.rglob("*.json")}


class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "state"
        create_preview(self.root)
        self.before = ledger(self.root)
        self.before_evidence = evidence(self.root)

    def version(self, root=None):
        with closing(sqlite3.connect((root or self.root) / "state.sqlite3")) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def test_populated_preview_upgrade_preserves_ledger_and_evidence(self):
        self.assertEqual(self.version(), 0)
        state.initialize(self.root)
        self.assertEqual(self.version(), state.SCHEMA_VERSION)
        self.assertGreater(self.version(), 0)
        self.assertEqual(ledger(self.root), self.before)
        self.assertEqual(evidence(self.root), self.before_evidence)
        with state.connect(self.root) as conn:
            self.assertEqual(state.admission(conn, 7)["body_sha"], "f" * 64)
            self.assertEqual(state.job(conn, 7)["pr"], 104)
            self.assertIsNone(state.job(conn, 7)["image_id"])

    def test_unversioned_post_preview_image_column_is_preserved(self):
        root = self.base / "post-preview"
        create_preview(root, with_image=True)
        image_id = "sha256:" + "d" * 64
        before = ledger(root)
        state.initialize(root)
        self.assertEqual(ledger(root), before)
        with state.connect(root) as conn:
            self.assertEqual(state.job(conn, 7)["image_id"], image_id)

    def test_interrupted_migration_rolls_back_schema_rows_and_version(self):
        original = state._migrate_preview

        def interrupted(conn):
            original(conn)
            raise OSError("simulated interruption after schema change")

        with patch.object(state, "_migrate_preview", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                state.initialize(self.root)
        self.assertEqual(self.version(), 0)
        self.assertEqual(ledger(self.root), self.before)
        self.assertEqual(evidence(self.root), self.before_evidence)
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("image_id", columns)
        state.initialize(self.root)
        self.assertEqual(self.version(), state.SCHEMA_VERSION)
        self.assertEqual(ledger(self.root), self.before)

    def test_repeated_initialization_is_idempotent(self):
        state.initialize(self.root)
        after_first = ledger(self.root)
        state.initialize(self.root)
        with state.connect(self.root):
            pass
        self.assertEqual(self.version(), state.SCHEMA_VERSION)
        self.assertEqual(ledger(self.root), after_first)
        self.assertEqual(ledger(self.root), self.before)
        self.assertEqual(evidence(self.root), self.before_evidence)

    def test_newer_unknown_version_is_rejected_without_mutation(self):
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as conn:
            conn.execute(f"PRAGMA user_version = {state.SCHEMA_VERSION + 1}")
            conn.commit()
        with self.assertRaises((ValueError, RuntimeError, sqlite3.DatabaseError)):
            state.initialize(self.root)
        self.assertEqual(self.version(), state.SCHEMA_VERSION + 1)
        self.assertEqual(ledger(self.root), self.before)
        self.assertEqual(evidence(self.root), self.before_evidence)

    def test_backup_restores_preview_state_after_upgrade(self):
        backup = self.base / "backup"
        backup.mkdir()
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as source, \
             closing(sqlite3.connect(backup / "state.sqlite3")) as target:
            source.backup(target)
        for directory in ("reviews", "evidence"):
            shutil.copytree(self.root / directory, backup / directory)
        state.initialize(self.root)
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as conn:
            conn.execute("UPDATE jobs SET status='blocked' WHERE issue=7")
            conn.commit()
        shutil.copy2(backup / "state.sqlite3", self.root / "state.sqlite3")
        for directory in ("reviews", "evidence"):
            shutil.rmtree(self.root / directory)
            shutil.copytree(backup / directory, self.root / directory)
        self.assertEqual(self.version(), 0)
        self.assertEqual(ledger(self.root), self.before)
        self.assertEqual(evidence(self.root), self.before_evidence)
        state.initialize(self.root)
        self.assertEqual(self.version(), state.SCHEMA_VERSION)
        self.assertEqual(ledger(self.root), self.before)


if __name__ == "__main__":
    unittest.main()
