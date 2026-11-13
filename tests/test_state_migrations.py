"""Upgrade and restore tests using the populated preview state format."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ermik import state
from ermik.config import Config
from ermik.evidence import make_manifest
from ermik.evidence_export import _scope_sha256, verify_manifest
from ermik.evidence_store import read_evidence, write_evidence
from ermik.review import _checks_sha256


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


def create_preview(root: Path, *, with_image: bool = False,
                   schema: str = PREVIEW_SCHEMA) -> Path:
    root.mkdir(parents=True)
    db = root / "state.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(schema)
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
                                 "attempts_count,review_next_check_at" if name == "jobs" else
                                 "attempt_id,issue,provider_id,model_id,complete,input_tokens,"
                                 "output_tokens,reasoning_tokens,cache_read_tokens,cache_write_tokens,"
                                 "total_tokens,estimated_cost,result_code,created_at"
                                 if name == "usage_events" else
                                 "id,issue,kind,model,head_sha,created_at"
                                 if name == "events" else "*")
                    + f" FROM {name} ORDER BY 1").fetchall()
                for name in ("admissions", "jobs", "retry_state", "events",
                             "usage_events")}


def evidence(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes()
            for directory in (root / "reviews", root / "evidence")
            if directory.exists() for path in directory.rglob("*.json")}


class StateMigrationTests(unittest.TestCase):
    def test_populated_v6_adds_nullable_task_set_and_rolls_back_on_failure(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root))
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(state.SCHEMA.replace(",\n  task_set TEXT", ""))
            conn.execute("PRAGMA user_version = 6")
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,"
                         "complexity,profile_sha) VALUES(7,'work-7',?,'running','[]',"
                         "'easy',?)", ("a" * 40, "b" * 64))
            conn.commit()
        original = state._migrate_v6

        def interrupted(conn):
            original(conn)
            raise OSError("interrupted task-set migration")

        with patch.object(state, "_migrate_v6", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted task-set migration"):
                state.initialize(root)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertNotIn("task_set", {column[1] for column in
                             conn.execute("PRAGMA table_info(jobs)")})
        state.initialize(root)
        with state.connect(root) as conn:
            row = state.job(conn, 7)
            self.assertEqual((row["complexity"], row["profile_sha"], row["task_set"]),
                             ("easy", "b" * 64, None))
        state.initialize(root)

    def test_v5_upgrade_adds_complexity_fields_without_changing_prior_jobs(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root))
        old = (state.SCHEMA.replace(",\n  complexity TEXT", "")
               .replace(",\n  profile_sha TEXT", "")
               .replace(",\n  result_effort TEXT", "")
               .replace(",\n  task_set TEXT", "")
               .replace(",\n  effort TEXT", ""))
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(old)
            conn.execute("PRAGMA user_version = 5")
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(7,'work-7',?,'running','[]')", ("a" * 40,))
            conn.execute("INSERT INTO events(issue,kind,model,policy_version,eligible_json,"
                         "decision_reason) VALUES(7,'route_decision','provider/model',1,?,"
                         "'priority')", ('["provider/model"]',))
            conn.commit()
        original = state._migrate_v5

        def interrupted(conn):
            original(conn)
            raise OSError("interrupted complexity migration")

        with patch.object(state, "_migrate_v5", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted complexity migration"):
                state.initialize(root)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertNotIn("complexity", {column[1] for column in
                             conn.execute("PRAGMA table_info(jobs)")})
        state.initialize(root)
        with state.connect(root) as conn:
            row = conn.execute("SELECT complexity,profile_sha,result_effort "
                               "FROM jobs WHERE issue=7").fetchone()
            self.assertEqual(tuple(row), (None, None, None))
            self.assertIn("complexity", {column[1] for column in conn.execute(
                "PRAGMA table_info(events)")})
            event = conn.execute("SELECT policy_version,eligible_json,decision_reason,"
                                 "effort,complexity FROM events WHERE issue=7").fetchone()
            self.assertEqual(tuple(event), (1, '["provider/model"]', "priority", None, None))

    @staticmethod
    def v3_schema() -> str:
        return (state.SCHEMA.replace(",\n  complexity TEXT", "")
                .replace(",\n  profile_sha TEXT", "")
                .replace(",\n  result_effort TEXT", "")
                .replace(",\n  task_set TEXT", "")
                .replace(",\n  effort TEXT", "")
                .replace("\n  ,complexity TEXT", "")
                .replace(",\n  route_key TEXT", "")
                .replace(",\n  policy_version INTEGER,\n  eligible_json TEXT,\n  decision_reason TEXT", "")
                .replace(",\n  phase TEXT CHECK(phase IN ('initial','correction')),\n"
                         "  finished_at TEXT,\n"
                         "  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0)", ""))

    @staticmethod
    def v4_schema() -> str:
        return (state.SCHEMA.replace(",\n  complexity TEXT", "")
                .replace(",\n  profile_sha TEXT", "")
                .replace(",\n  result_effort TEXT", "")
                .replace(",\n  task_set TEXT", "")
                .replace(",\n  effort TEXT", "")
                .replace("\n  ,complexity TEXT", "")
                .replace(",\n  policy_version INTEGER,\n  eligible_json TEXT,\n  decision_reason TEXT", ""))

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

    def test_v3_upgrade_preserves_usage_and_rejects_unexpected_columns(self):
        root = self.base / "v3"
        root.mkdir()
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(self.v3_schema())
            conn.execute("PRAGMA user_version = 3")
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(7,'work-7',?,'running','[]')", ("a" * 40,))
            conn.execute("INSERT INTO usage_events(attempt_id,issue,provider_id,model_id,"
                         "complete,result_code,created_at) VALUES(?,?,?,?,?,?,?)",
                         ("old-attempt", 7, "provider", "model", 0, 75,
                          "2026-01-01 00:00:00"))
            conn.commit()
        state.initialize(root)
        with state.connect(root) as conn:
            row = conn.execute("SELECT * FROM usage_events").fetchone()
            self.assertEqual((row["result_code"], row["finished_at"], row["phase"],
                              row["duration_ms"]),
                             (75, "2026-01-01 00:00:00", None, None))
            self.assertIsNone(state.job(conn, 7)["route_key"])
        bad = self.base / "v3-extra"
        bad.mkdir()
        with closing(sqlite3.connect(bad / "state.sqlite3")) as conn:
            conn.executescript(self.v3_schema())
            conn.execute("ALTER TABLE jobs ADD COLUMN unexpected TEXT")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "schema"):
            state.initialize(bad)
        self.assertEqual(self.version(bad), 3)

    def test_v4_upgrade_preserves_events_and_provider_leases(self):
        root = self.base / "v4"
        root.mkdir()
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(self.v4_schema())
            conn.execute("CREATE TABLE provider_leases ("
                         "lease_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, "
                         "model_id TEXT NOT NULL, expires_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO events(issue,kind,model) VALUES(7,'model_selected','p/m')")
            conn.execute("INSERT INTO provider_leases VALUES('lease-1','p','m',42)")
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        state.initialize(root)
        with state.connect(root) as conn:
            row = conn.execute("SELECT * FROM events").fetchone()
            self.assertEqual((row["model"], row["policy_version"], row["eligible_json"],
                              row["decision_reason"]), ("p/m", None, None, None))
            lease = conn.execute("SELECT * FROM provider_leases").fetchone()
            self.assertEqual((lease["route_key"], lease["reserved_cost"]), (None, 0))

    def test_v4_migration_rejects_unknown_column_and_rolls_back_interruption(self):
        root = self.base / "v4-interrupted"
        root.mkdir()
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(self.v4_schema())
            conn.execute("INSERT INTO events(issue,kind) VALUES(7,'admitted')")
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        original = state._migrate_v4

        def interrupted(conn):
            original(conn)
            raise OSError("interrupted route migration")

        with patch.object(state, "_migrate_v4", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted route migration"):
                state.initialize(root)
        self.assertEqual(self.version(root), 4)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            self.assertNotIn("policy_version", {row[1] for row in conn.execute(
                "PRAGMA table_info(events)")})
            self.assertEqual(conn.execute("SELECT issue,kind FROM events").fetchall(),
                             [(7, "admitted")])
            conn.execute("ALTER TABLE events ADD COLUMN unexpected TEXT")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "schema"):
            state.initialize(root)
        self.assertEqual(self.version(root), 4)

    def test_route_decision_event_bounded_and_redacted(self):
        root = self.base / "route-events"
        with state.connect(root) as conn:
            state.event(conn, 7, "route_decision", policy_version=1,
                        eligible=["p/m", "p/n"], model="p/m",
                        decision_reason="priority")
            state.event(conn, 7, "route_decision", policy_version=1,
                        eligible=[], decision_reason="no_eligible")
            rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
            self.assertEqual(json.loads(rows[0]["eligible_json"]), ["p/m", "p/n"])
            self.assertEqual((rows[0]["model"], rows[0]["decision_reason"]),
                             ("p/m", "priority"))
            self.assertIsNone(rows[1]["model"])
            self.assertNotIn("task", rows[0].keys())
            self.assertNotIn("key", rows[0].keys())
            for kwargs in ({"policy_version": 0, "eligible": [], "decision_reason": "priority"},
                           {"policy_version": 1, "eligible": ["p/m"] * 33,
                            "decision_reason": "priority"},
                           {"policy_version": 1, "eligible": ["p/m secret"],
                            "decision_reason": "priority"},
                           {"policy_version": 1, "eligible": [],
                            "decision_reason": "private error"},
                           {"policy_version": 1, "eligible": [],
                            "decision_reason": "priority", "model": "p/m secret"},
                           {"policy_version": 1, "eligible": [],
                            "decision_reason": "priority", "head_sha": "a" * 40}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    state.event(conn, 7, "route_decision", **kwargs)
            with self.assertRaises(ValueError):
                state.event(conn, 7, "model_selected", policy_version=1)
            self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_attempt_start_is_durable_and_finish_is_idempotent(self):
        root = self.base / "attempts"
        with state.connect(root) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(1,'work-1',?,'running','[]')", ("a" * 40,))
            state.begin_attempt(conn, "attempt-1", 1, "provider", "model", "initial")
        with state.connect(root) as conn:
            row = conn.execute("SELECT * FROM usage_events WHERE attempt_id='attempt-1'").fetchone()
            self.assertEqual((row["result_code"], row["finished_at"],
                              row["estimated_cost"], row["phase"]),
                             (-2, None, None, "initial"))
            self.assertTrue(state.finish_attempt(conn, "attempt-1", None, 75, 12))
            self.assertFalse(state.finish_attempt(conn, "attempt-1", None, 0, 13))
            with self.assertRaises(sqlite3.IntegrityError):
                state.begin_attempt(conn, "attempt-1", 1, "provider", "model", "initial")
        with state.connect(root) as conn:
            row = conn.execute("SELECT * FROM usage_events WHERE attempt_id='attempt-1'").fetchone()
            self.assertEqual((row["result_code"], row["duration_ms"], row["complete"]),
                             (75, 12, 0))

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

    def test_v1_upgrade_adds_bounded_reason_without_changing_job(self):
        root = self.base / "v1-reason"
        create_preview(root, with_image=True)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        before = ledger(root)
        state.initialize(root)
        self.assertEqual(self.version(root), state.SCHEMA_VERSION)
        self.assertEqual(ledger(root), before)
        with state.connect(root) as conn:
            self.assertIsNone(state.job(conn, 7)["reason_code"])
            conn.execute("UPDATE jobs SET reason_code='provider_auth' WHERE issue=7")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE jobs SET reason_code='private error text' WHERE issue=7")

    def test_interrupted_v1_upgrade_rolls_back_new_column(self):
        root = self.base / "v1-interrupted"
        create_preview(root, with_image=True)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        before = ledger(root)
        original = state._migrate_v1

        def interrupted(conn):
            original(conn)
            raise OSError("interrupted reason migration")

        with patch.object(state, "_migrate_v1", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted reason migration"):
                state.initialize(root)
        self.assertEqual(self.version(root), 1)
        self.assertEqual(ledger(root), before)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            self.assertNotIn("reason_code", {row[1] for row in conn.execute("PRAGMA table_info(jobs)")})
        state.initialize(root)
        self.assertEqual(self.version(root), state.SCHEMA_VERSION)

    def test_v2_upgrade_preserves_jobs_retry_fk_and_reason_constraint(self):
        root = self.base / "v2-reasons"
        root.mkdir()
        old = "'provider_auth','provider_quota','local_check_failed'"
        new = old + ",'broker_unavailable','publication_unconfirmed','publish_credential_unavailable'"
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(self.v3_schema().replace(new, old))
            conn.execute("PRAGMA user_version = 2")
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,reason_code) "
                         "VALUES(7,'work-7',?,'retry_wait','[]','provider_quota')", ("a" * 40,))
            conn.execute("INSERT INTO retry_state(issue,attempts,category,resume_status,failure_id) "
                         "VALUES(7,1,'provider','running','failure-7')")
            conn.commit()
        before = ledger(root)
        state.initialize(root)
        self.assertEqual(self.version(root), state.SCHEMA_VERSION)
        self.assertEqual(ledger(root), before)
        with state.connect(root) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA foreign_key_list(retry_state)").fetchone()[2], "jobs")
            conn.execute("UPDATE jobs SET reason_code='publication_unconfirmed' WHERE issue=7")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE jobs SET reason_code='private reply' WHERE issue=7")

    def test_interrupted_v2_rebuild_rolls_back_rows_and_constraint(self):
        root = self.base / "v2-interrupted"
        root.mkdir()
        old = "'provider_auth','provider_quota','local_check_failed'"
        new = old + ",'broker_unavailable','publication_unconfirmed','publish_credential_unavailable'"
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(self.v3_schema().replace(new, old))
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        original = state._migrate_v2

        def interrupted(conn):
            original(conn)
            raise OSError("interrupted table rebuild")

        with patch.object(state, "_migrate_v2", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted table rebuild"):
                state.initialize(root)
        self.assertEqual(self.version(root), 2)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='jobs_v3'").fetchone(), None)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,reason_code) "
                             "VALUES(7,'work-7',?,'blocked','[]','broker_unavailable')", ("a" * 40,))
        state.initialize(root)
        self.assertEqual(self.version(root), state.SCHEMA_VERSION)

    def test_v2_without_bounded_reason_constraint_is_rejected(self):
        root = self.base / "v2-unsafe-reason"
        root.mkdir()
        old = "'provider_auth','provider_quota','local_check_failed'"
        new = old + ",'broker_unavailable','publication_unconfirmed','publish_credential_unavailable'"
        schema = self.v3_schema().replace(new, old)
        schema = schema.replace(f"reason_code TEXT CHECK(reason_code IN ({old}))",
                                "reason_code TEXT")
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.executescript(schema)
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "schema"):
            state.initialize(root)
        self.assertEqual(self.version(root), 2)

    def test_v1_manifest_fails_closed_and_survives_populated_upgrade(self):
        root = self.base / "manifest-preview"
        create_preview(root, with_image=True)
        with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs WHERE issue=7").fetchone()
            admission = conn.execute("SELECT * FROM admissions WHERE issue=7").fetchone()
            identity = {key: job[key] for key in
                        ("issue", "pr", "branch", "base_sha", "head_sha", "image_id")}
            body_sha = admission["body_sha"]
            allowed = tuple(json.loads(admission["allowed_json"]))
        cfg = Config("owner/project", "keeper", root, self.base / "work",
                     identity["image_id"], ("run",), (("check", "all"),),
                     1, 60, (), "main")
        manifest = make_manifest(
            repo=cfg.repo, head_repository=cfg.repo, issue=identity["issue"],
            pr=identity["pr"], branch=identity["branch"],
            base=identity["base_sha"], head=identity["head_sha"],
            body_sha256=body_sha, scope_sha256=_scope_sha256(allowed),
            checks_sha256=_checks_sha256(cfg), paths=["src/worker.py"],
            checks=[{"index": 1, "result": "passed", "log_sha256": "e" * 64}],
            image=identity["image_id"],
            image_config_digest="sha256:" + "f" * 64,
            model="provider/second", attempts=3,
            usage={"observation": "unknown"}, unknowns=["usage"])
        manifest.pop("sha256")
        manifest.pop("image_config_digest")
        manifest["version"] = 1
        manifest["sha256"] = hashlib.sha256(json.dumps(
            manifest, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("utf-8")).hexdigest()
        manifest_path = root / "evidence" / "7" / "manifest-v1.json"
        write_evidence(manifest_path, manifest)
        manifest_bytes = manifest_path.read_bytes()

        def decisions():
            saved = read_evidence(manifest_path)
            with closing(sqlite3.connect(root / "state.sqlite3")) as conn:
                conn.row_factory = sqlite3.Row
                job = conn.execute("SELECT * FROM jobs WHERE issue=7").fetchone()
                admission = conn.execute(
                    "SELECT * FROM admissions WHERE issue=7").fetchone()
                current_identity = {key: job[key] for key in identity}
                current_body_sha = admission["body_sha"]
                current_allowed = tuple(json.loads(admission["allowed_json"]))
            pr = {"headRefName": current_identity["branch"],
                  "baseRefOid": current_identity["base_sha"],
                  "headRefOid": current_identity["head_sha"],
                  "body": (f"Closes #7\nExecution image: "
                           f"`{current_identity['image_id']}`")}
            spec = SimpleNamespace(body_sha256=current_body_sha,
                                   allowed_paths=current_allowed)
            with patch("ermik.evidence_export._github"), \
                 patch("ermik.evidence_export._live",
                       return_value=(pr, current_identity["issue"], spec,
                                     ["src/worker.py"])), \
                 patch("ermik.evidence_export._resolve_image_id",
                       return_value=current_identity["image_id"]):
                current = verify_manifest(cfg, current_identity["pr"], saved)
                pr["headRefOid"] = "e" * 40
                stale = verify_manifest(cfg, current_identity["pr"], saved)
            return current, stale, current_identity

        before = decisions()
        self.assertEqual(before[0], {"status": "stale",
                                     "reason": "image_identity_missing"})
        self.assertEqual(before[1], before[0])
        state.initialize(root)
        self.assertEqual(self.version(root), state.SCHEMA_VERSION)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(decisions(), before)

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

    def test_same_columns_without_preview_constraints_are_rejected(self):
        variants = {
            "branch uniqueness": ("branch TEXT NOT NULL UNIQUE", "branch TEXT NOT NULL"),
            "retry foreign key": ("issue INTEGER PRIMARY KEY REFERENCES jobs(issue)",
                                  "issue INTEGER PRIMARY KEY"),
            "retry attempts check": ("attempts INTEGER NOT NULL CHECK(attempts >= 1)",
                                     "attempts INTEGER NOT NULL"),
            "retry lease check":
                ("CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))",
                 "CHECK (1)"),
            "usage completeness check":
                ("complete INTEGER NOT NULL CHECK(complete IN (0,1))",
                 "complete INTEGER NOT NULL"),
        }
        for label, (old, new) in variants.items():
            with self.subTest(label=label):
                self.assertIn(old, PREVIEW_SCHEMA)
                root = self.base / label.replace(" ", "-")
                create_preview(root, schema=PREVIEW_SCHEMA.replace(old, new))
                before = ledger(root)
                with self.assertRaisesRegex(ValueError, "schema"):
                    state.initialize(root)
                self.assertEqual(self.version(root), 0)
                self.assertEqual(ledger(root), before)

    def test_preview_with_provider_tables_preserves_provider_state(self):
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as conn:
            conn.execute("CREATE TABLE provider_cooldowns ("
                         "provider_id TEXT PRIMARY KEY, retry_at INTEGER NOT NULL, "
                         "reason TEXT NOT NULL CHECK(reason IN ('quota')))")
            conn.execute("CREATE TABLE provider_leases ("
                         "lease_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, "
                         "model_id TEXT NOT NULL, expires_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO provider_cooldowns VALUES('provider',1735900300,'quota')")
            conn.execute("INSERT INTO provider_leases VALUES('lease-1','provider','second',"
                         "1735900400)")
            conn.commit()
            before = {name: conn.execute(f"SELECT * FROM {name}").fetchall()
                      for name in ("provider_cooldowns", "provider_leases")}
        state.initialize(self.root)
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as conn:
            for name, rows in before.items():
                columns = ("lease_id,provider_id,model_id,expires_at"
                           if name == "provider_leases" else "*")
                self.assertEqual(conn.execute(f"SELECT {columns} FROM {name}").fetchall(), rows)
            lease = conn.execute("SELECT route_key,reserved_cost FROM provider_leases").fetchone()
            self.assertEqual(lease, (None, 0))
        self.assertEqual(ledger(self.root), self.before)

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
