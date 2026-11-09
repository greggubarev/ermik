"""Small durable ledger for local admissions and published work."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS admissions (
  issue INTEGER PRIMARY KEY, body_sha TEXT NOT NULL,
  allowed_json TEXT NOT NULL, admitted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS jobs (
  issue INTEGER PRIMARY KEY, branch TEXT NOT NULL UNIQUE,
  base_sha TEXT NOT NULL, head_sha TEXT, pr INTEGER,
  status TEXT NOT NULL, allowed_json TEXT NOT NULL,
  last_request INTEGER NOT NULL DEFAULT 0,
  error TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_model TEXT, result_model TEXT, attempts_count INTEGER NOT NULL DEFAULT 0,
  review_next_check_at INTEGER NOT NULL DEFAULT 0,
  image_id TEXT,
  reason_code TEXT CHECK(reason_code IN ('provider_auth','provider_quota','local_check_failed','broker_unavailable','publication_unconfirmed','publish_credential_unavailable')),
  route_key TEXT,
  complexity TEXT,
  profile_sha TEXT,
  result_effort TEXT
);
CREATE TABLE IF NOT EXISTS retry_state (
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
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue INTEGER NOT NULL,
  kind TEXT NOT NULL,
  model TEXT,
  head_sha TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  policy_version INTEGER,
  eligible_json TEXT,
  decision_reason TEXT,
  effort TEXT,
  complexity TEXT
);
CREATE INDEX IF NOT EXISTS events_issue_id ON events(issue,id);
CREATE TABLE IF NOT EXISTS usage_events (
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  phase TEXT CHECK(phase IN ('initial','correction')),
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0)
);
CREATE INDEX IF NOT EXISTS usage_events_issue ON usage_events(issue,created_at);
"""

SCHEMA_VERSION = 6

_TABLE_COLUMNS = {
    "admissions": {"issue", "body_sha", "allowed_json", "admitted_at"},
    "jobs": {"issue", "branch", "base_sha", "head_sha", "pr", "status",
             "allowed_json", "last_request", "error", "updated_at", "last_model",
             "result_model", "attempts_count", "review_next_check_at", "image_id",
             "reason_code", "route_key", "complexity", "profile_sha", "result_effort"},
    "retry_state": {"issue", "attempts", "category", "resume_status",
                    "next_retry_at", "lease_token", "lease_expires_at", "failure_id"},
    "events": {"id", "issue", "kind", "model", "head_sha", "created_at",
               "policy_version", "eligible_json", "decision_reason", "effort",
               "complexity"},
    "usage_events": {"attempt_id", "issue", "provider_id", "model_id", "complete",
                     "input_tokens", "output_tokens", "reasoning_tokens",
                     "cache_read_tokens", "cache_write_tokens", "total_tokens",
                     "estimated_cost", "result_code", "created_at", "phase",
                     "finished_at", "duration_ms"},
}
_PROVIDER_COLUMNS = {
    "provider_cooldowns": {"provider_id", "retry_at", "reason"},
    "provider_leases": {"lease_id", "provider_id", "model_id", "expires_at",
                        "route_key", "reserved_cost"},
}
_INDEXES = {"events_issue_id", "usage_events_issue"}


def _schema_objects(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    rows = conn.execute("SELECT type,name FROM sqlite_master WHERE type IN "
                        "('table','index','view','trigger')").fetchall()
    tables = {name for kind, name in rows if kind == "table" and name != "sqlite_sequence"}
    indexes = {name for kind, name in rows if kind == "index"
               and not name.startswith("sqlite_autoindex_")}
    other = {name for kind, name in rows if kind in ("view", "trigger")}
    if other:
        raise ValueError("unknown SQLite state schema objects")
    return tables, indexes


def _validate_schema(conn: sqlite3.Connection, *, preview: bool = False,
                     v1: bool = False, v2: bool = False,
                     v3: bool = False, v4: bool = False,
                     v5: bool = False) -> None:
    tables, indexes = _schema_objects(conn)
    if (not set(_TABLE_COLUMNS).issubset(tables)
            or tables - set(_TABLE_COLUMNS) - set(_PROVIDER_COLUMNS)
            or indexes != _INDEXES):
        raise ValueError("unknown or incomplete SQLite state schema")
    reference = _reference_columns()
    for table in tables:
        actual = [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
        if table in reference:
            expected = reference[table]
            if table == "jobs":
                if preview and len(actual) in (len(expected) - 6, len(expected) - 5):
                    expected = expected[:len(actual)]
                elif v1:
                    expected = expected[:-5]
                elif v2 or v3:
                    expected = expected[:-4]
                elif v4 or v5:
                    expected = expected[:-3]
            elif table == "usage_events" and (preview or v1 or v2 or v3):
                expected = expected[:-3]
            elif table == "events":
                if preview or v1 or v2 or v3 or v4:
                    expected = expected[:-5]
                elif v5:
                    expected = expected[:-2]
            if actual != expected:
                raise ValueError("unknown or incomplete SQLite state schema")
        elif table == "provider_leases":
            expected = [(0, "lease_id", "TEXT", 0, None, 1),
                        (1, "provider_id", "TEXT", 1, None, 0),
                        (2, "model_id", "TEXT", 1, None, 0),
                        (3, "expires_at", "INTEGER", 1, None, 0)]
            if not (preview or v1 or v2 or v3 or v4):
                expected += [(4, "route_key", "TEXT", 0, None, 0),
                             (5, "reserved_cost", "REAL", 1, "0", 0)]
            if actual != expected:
                raise ValueError("unknown or incomplete SQLite state schema")
        elif {row[1] for row in actual} != _PROVIDER_COLUMNS[table]:
            raise ValueError("unknown or incomplete SQLite state schema")
    for index, table, columns in (("events_issue_id", "events", ("issue", "id")),
                                  ("usage_events_issue", "usage_events",
                                   ("issue", "created_at"))):
        actual = tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index})"))
        owner = conn.execute("SELECT tbl_name FROM sqlite_master WHERE name=?",
                             (index,)).fetchone()
        if actual != columns or owner[0] != table:
            raise ValueError("unknown or incomplete SQLite state schema")
    unique_branch = any(
        row[2] and not row[4] and
        tuple(column[2] for column in conn.execute(f"PRAGMA index_info({row[1]})"))
        == ("branch",)
        for row in conn.execute("PRAGMA index_list(jobs)"))
    if not unique_branch:
        raise ValueError("unknown or incomplete SQLite state schema")
    foreign_keys = [tuple(row[2:5]) for row in conn.execute(
        "PRAGMA foreign_key_list(retry_state)")]
    if foreign_keys != [("jobs", "issue", "issue")]:
        raise ValueError("unknown or incomplete SQLite state schema")
    checks = {
        "retry_state": ("CHECK(attempts>=1)",
                        "CHECK(resume_statusIN('running','correcting'))",
                        "CHECK((lease_tokenISNULL)=(lease_expires_atISNULL))"),
        "usage_events": (("CHECK(completeIN(0,1))",) if
                         (preview or v1 or v2 or v3) else
                         ("CHECK(completeIN(0,1))",
                          "CHECK(phaseIN('initial','correction'))",
                          "CHECK(duration_msISNULLORduration_ms>=0)")),
    }
    if "reason_code" in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}:
        reason_check = ("CHECK(reason_codeIN('provider_auth','provider_quota',"
                        "'local_check_failed'"
                        + ("" if v2 else ",'broker_unavailable','publication_unconfirmed',"
                           "'publish_credential_unavailable'") + "))")
        checks["jobs"] = (reason_check,)
    if "provider_cooldowns" in tables:
        checks["provider_cooldowns"] = ("CHECK(reasonIN('quota'))",)
    for table, required in checks.items():
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        normalized = re.sub(r"\s+", "", sql).upper()
        if any(expression.upper() not in normalized for expression in required):
            raise ValueError("unknown or incomplete SQLite state schema")


def _create_schema(conn: sqlite3.Connection) -> None:
    # executescript() commits a pending transaction, so execute each statement
    # separately to keep creation and user_version in the same transaction.
    statement = ""
    for line in SCHEMA.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("incomplete SQLite schema definition")


@lru_cache(maxsize=1)
def _reference_columns() -> dict[str, list[tuple]]:
    with sqlite3.connect(":memory:") as reference:
        _create_schema(reference)
        return {table: [tuple(row) for row in reference.execute(
            f"PRAGMA table_info({table})")] for table in _TABLE_COLUMNS}


def _migrate_preview(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "image_id" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN image_id TEXT")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE jobs ADD COLUMN reason_code TEXT CHECK(reason_code IN "
                 "('provider_auth','provider_quota','local_check_failed'))")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    # Rebuild the constrained table in one transaction. Keep the jobs name as
    # the retry_state FK target, and verify all existing references before commit.
    columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
    jobs_ddl = next(statement.strip() for statement in SCHEMA.split(";")
                    if statement.lstrip().startswith("CREATE TABLE IF NOT EXISTS jobs ("))
    jobs_ddl = jobs_ddl.replace(",\n  route_key TEXT", "")
    jobs_ddl = jobs_ddl.replace(",\n  complexity TEXT", "").replace(
        ",\n  profile_sha TEXT", "").replace(",\n  result_effort TEXT", "")
    conn.execute(jobs_ddl.replace("CREATE TABLE IF NOT EXISTS jobs (",
                                  "CREATE TABLE jobs_v3 (", 1))
    names = ",".join(columns)
    conn.execute(f"INSERT INTO jobs_v3({names}) SELECT {names} FROM jobs")
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_v3 RENAME TO jobs")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite state foreign key check failed")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    if "route_key" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("ALTER TABLE jobs ADD COLUMN route_key TEXT")
    for statement in (
            "ALTER TABLE usage_events ADD COLUMN phase TEXT CHECK(phase IN ('initial','correction'))",
            "ALTER TABLE usage_events ADD COLUMN finished_at TEXT",
            "ALTER TABLE usage_events ADD COLUMN duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0)"):
        conn.execute(statement)
    conn.execute("UPDATE usage_events SET finished_at=created_at")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    for statement in (
            "ALTER TABLE events ADD COLUMN policy_version INTEGER",
            "ALTER TABLE events ADD COLUMN eligible_json TEXT",
            "ALTER TABLE events ADD COLUMN decision_reason TEXT"):
        conn.execute(statement)
    if "provider_leases" in _schema_objects(conn)[0]:
        conn.execute("ALTER TABLE provider_leases ADD COLUMN route_key TEXT")
        conn.execute("ALTER TABLE provider_leases ADD COLUMN reserved_cost REAL NOT NULL DEFAULT 0")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    for statement in (
            "ALTER TABLE jobs ADD COLUMN complexity TEXT",
            "ALTER TABLE jobs ADD COLUMN profile_sha TEXT",
            "ALTER TABLE jobs ADD COLUMN result_effort TEXT",
            "ALTER TABLE events ADD COLUMN effort TEXT",
            "ALTER TABLE events ADD COLUMN complexity TEXT"):
        conn.execute(statement)


def initialize(root: Path) -> Path:
    if root.is_symlink():
        raise ValueError("state_dir cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        if root.stat().st_uid != os.geteuid():
            raise ValueError("state_dir belongs to another user")
        os.chmod(root, 0o700)
    db = root / "state.sqlite3"
    fresh = not db.exists()
    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and fresh:
            tables, indexes = _schema_objects(conn)
            if tables or indexes:
                raise ValueError("unknown or incomplete SQLite state schema")
            _create_schema(conn)
        elif version == 0:
            _validate_schema(conn, preview=True)
            _migrate_preview(conn)
            _migrate_v1(conn)
        elif version == 1:
            _validate_schema(conn, v1=True)
            _migrate_v1(conn)
        elif version in (2, 3, 4, 5):
            pass
        elif version != SCHEMA_VERSION:
            raise ValueError(f"unsupported SQLite state schema version: {version}")
        if version in (0, 1, 2) and not fresh:
            _validate_schema(conn, v2=True)
            _migrate_v2(conn)
        if version in (0, 1, 2, 3) and not fresh:
            _validate_schema(conn, v3=True)
            _migrate_v3(conn)
        if version in (0, 1, 2, 3, 4) and not fresh:
            _validate_schema(conn, v4=True)
            _migrate_v4(conn)
        if version in (0, 1, 2, 3, 4, 5) and not fresh:
            _validate_schema(conn, v5=True)
            _migrate_v5(conn)
        _validate_schema(conn)
        if version != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if os.name != "nt":
        os.chmod(db, 0o600)
    return db


@contextmanager
def connect(root: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(initialize(root), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def exclusive(root: Path) -> Iterator[None]:
    """Prevent two controller processes using the same state directory."""
    initialize(root)
    path = root / "controller.lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def admission(conn: sqlite3.Connection, issue: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM admissions WHERE issue=?", (issue,)).fetchone()


def job(conn: sqlite3.Connection, issue: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE issue=?", (issue,)).fetchone()


def bind_image(conn: sqlite3.Connection, issue: int, image_id: str) -> str:
    """Persist one immutable image identity for a job before container execution."""
    if (type(issue) is not int or issue <= 0 or type(image_id) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None):
        raise ValueError("invalid job image identity")
    conn.execute("UPDATE jobs SET image_id=? WHERE issue=? AND image_id IS NULL",
                 (image_id, issue))
    row = conn.execute("SELECT image_id FROM jobs WHERE issue=?", (issue,)).fetchone()
    if row is None:
        raise ValueError("job does not exist")
    if row[0] != image_id:
        raise ValueError("job image identity changed")
    return image_id


def jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs ORDER BY issue").fetchall()


def event(conn: sqlite3.Connection, issue: int, kind: str, *,
          model: str | None = None, head_sha: str | None = None,
          effort: str | None = None,
          complexity: str | None = None,
          policy_version: int | None = None,
          eligible: tuple[str, ...] | list[str] | None = None,
          decision_reason: str | None = None) -> None:
    if type(issue) is not int or issue <= 0 or kind not in {
            "admitted", "claimed", "retry_scheduled", "retry_started", "model_selected",
            "quota_paused", "checks_passed", "pr_published", "awaiting_review", "blocked",
            "correction_started", "merged", "evidence_delivered",
            "route_decision"}:
        raise ValueError("invalid event")
    if kind == "route_decision":
        model_ref = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}/[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
        if (type(policy_version) is not int or policy_version < 1
                or type(decision_reason) is not str or decision_reason not in {
                    "priority", "quality", "quota_fallback", "no_eligible",
                    "budget", "cooldown", "capacity"}
                or type(eligible) not in (list, tuple) or len(eligible) > 32
                or any(type(ref) is not str or model_ref.fullmatch(ref) is None
                       for ref in eligible)
                or (model is not None and (type(model) is not str
                                           or model_ref.fullmatch(model) is None))
                or head_sha is not None):
            raise ValueError("invalid route decision")
    elif any(value is not None for value in (policy_version, eligible, decision_reason)):
        raise ValueError("route metadata requires route_decision")
    if effort not in (None, "low", "medium", "high"):
        raise ValueError("invalid event effort")
    if complexity not in (None, "easy", "medium", "hard"):
        raise ValueError("invalid event complexity")
    conn.execute("INSERT INTO events(issue,kind,model,head_sha,policy_version,"
                 "eligible_json,decision_reason,effort,complexity) VALUES(?,?,?,?,?,?,?,?,?)",
                 (issue, kind, model, head_sha, policy_version,
                  json.dumps(eligible, separators=(",", ":")) if eligible is not None else None,
                  decision_reason, effort, complexity))


def record_usage(conn: sqlite3.Connection, attempt_id: str, issue: int,
                 provider_id: str, model_id: str, usage: dict | None,
                 result_code: int) -> None:
    """Compatibility helper for already-finished aggregate observations."""
    if conn.execute("SELECT 1 FROM usage_events WHERE attempt_id=?",
                    (attempt_id,)).fetchone() is not None:
        return
    begin_attempt(conn, attempt_id, issue, provider_id, model_id, "initial")
    finish_attempt(conn, attempt_id, usage, result_code, None)


def begin_attempt(conn: sqlite3.Connection, attempt_id: str, issue: int,
                  provider_id: str, model_id: str, phase: str) -> None:
    """Commit this row before invoking a worker; pending result_code is -2."""
    if (not attempt_id or type(issue) is not int or issue <= 0
            or type(provider_id) is not str or type(model_id) is not str
            or phase not in ("initial", "correction")):
        raise ValueError("invalid attempt identity")
    conn.execute("INSERT INTO usage_events(attempt_id,issue,provider_id,model_id,"
                 "complete,result_code,phase) VALUES(?,?,?,?,0,-2,?)",
                 (attempt_id, issue, provider_id, model_id, phase))


def finish_attempt(conn: sqlite3.Connection, attempt_id: str,
                   usage: dict | None, result_code: int,
                   duration_ms: int | None) -> bool:
    """Finish once with aggregate counters; keep missing counters as NULL."""
    if (type(result_code) is not int or result_code == -2
            or (duration_ms is not None and
                (type(duration_ms) is not int or duration_ms < 0))):
        raise ValueError("invalid attempt result")
    complete = type(usage) is dict and usage.get("complete") is True
    value = usage if complete else {}
    changed = conn.execute(
        "UPDATE usage_events SET complete=?,input_tokens=?,output_tokens=?,"
        "reasoning_tokens=?,cache_read_tokens=?,cache_write_tokens=?,total_tokens=?,"
        "estimated_cost=?,result_code=?,finished_at=CURRENT_TIMESTAMP,duration_ms=? "
        "WHERE attempt_id=? AND finished_at IS NULL",
        (int(complete), value.get("input"), value.get("output"),
         value.get("reasoning"), value.get("cache_read"),
         value.get("cache_write"), value.get("total"),
         value.get("estimated_cost"), result_code, duration_ms, attempt_id))
    if changed.rowcount:
        return True
    if conn.execute("SELECT 1 FROM usage_events WHERE attempt_id=?",
                    (attempt_id,)).fetchone() is None:
        raise ValueError("attempt does not exist")
    return False


def scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for a in left for b in right)


def conflicting(conn: sqlite3.Connection, paths: tuple[str, ...],
                *, exclude: int | None = None) -> bool:
    rows = conn.execute("SELECT issue,allowed_json FROM jobs WHERE status IN "
                        "('running','awaiting_review','correcting','blocked','retry_wait')").fetchall()
    return any(row["issue"] != exclude and scopes_overlap(
        paths, tuple(json.loads(row["allowed_json"]))) for row in rows)
