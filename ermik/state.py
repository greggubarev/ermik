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
  image_id TEXT
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS usage_events_issue ON usage_events(issue,created_at);
"""

SCHEMA_VERSION = 1

_TABLE_COLUMNS = {
    "admissions": {"issue", "body_sha", "allowed_json", "admitted_at"},
    "jobs": {"issue", "branch", "base_sha", "head_sha", "pr", "status",
             "allowed_json", "last_request", "error", "updated_at", "last_model",
             "result_model", "attempts_count", "review_next_check_at", "image_id"},
    "retry_state": {"issue", "attempts", "category", "resume_status",
                    "next_retry_at", "lease_token", "lease_expires_at", "failure_id"},
    "events": {"id", "issue", "kind", "model", "head_sha", "created_at"},
    "usage_events": {"attempt_id", "issue", "provider_id", "model_id", "complete",
                     "input_tokens", "output_tokens", "reasoning_tokens",
                     "cache_read_tokens", "cache_write_tokens", "total_tokens",
                     "estimated_cost", "result_code", "created_at"},
}
_PROVIDER_COLUMNS = {
    "provider_cooldowns": {"provider_id", "retry_at", "reason"},
    "provider_leases": {"lease_id", "provider_id", "model_id", "expires_at"},
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


def _validate_schema(conn: sqlite3.Connection, *, preview: bool = False) -> None:
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
            if preview and table == "jobs" and len(actual) == len(expected) - 1:
                expected = expected[:-1]
            if actual != expected:
                raise ValueError("unknown or incomplete SQLite state schema")
        elif {row[1] for row in actual} != _PROVIDER_COLUMNS[table]:
            raise ValueError("unknown or incomplete SQLite state schema")
    for index, columns in (("events_issue_id", ("issue", "id")),
                           ("usage_events_issue", ("issue", "created_at"))):
        actual = tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index})"))
        if actual != columns:
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
        elif version != SCHEMA_VERSION:
            raise ValueError(f"unsupported SQLite state schema version: {version}")
        _validate_schema(conn)
        if version == 0:
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
          model: str | None = None, head_sha: str | None = None) -> None:
    if type(issue) is not int or issue <= 0 or kind not in {
            "admitted", "claimed", "retry_scheduled", "retry_started", "model_selected",
            "quota_paused", "checks_passed", "pr_published", "awaiting_review", "blocked",
            "correction_started", "merged"}:
        raise ValueError("invalid event")
    conn.execute("INSERT INTO events(issue,kind,model,head_sha) VALUES(?,?,?,?)",
                 (issue, kind, model, head_sha))


def record_usage(conn: sqlite3.Connection, attempt_id: str, issue: int,
                 provider_id: str, model_id: str, usage: dict | None,
                 result_code: int) -> None:
    """Persist aggregate usage only; never store a provider transcript."""
    complete = bool(usage and usage.get("complete"))
    value = usage if complete else {}
    conn.execute("INSERT OR IGNORE INTO usage_events("
                 "attempt_id,issue,provider_id,model_id,complete,input_tokens,"
                 "output_tokens,reasoning_tokens,cache_read_tokens,cache_write_tokens,"
                 "total_tokens,estimated_cost,result_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (attempt_id, issue, provider_id, model_id, int(complete),
                  value.get("input"), value.get("output"), value.get("reasoning"),
                  value.get("cache_read"), value.get("cache_write"), value.get("total"),
                  value.get("estimated_cost"), result_code))


def scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for a in left for b in right)


def conflicting(conn: sqlite3.Connection, paths: tuple[str, ...],
                *, exclude: int | None = None) -> bool:
    rows = conn.execute("SELECT issue,allowed_json FROM jobs WHERE status IN "
                        "('running','awaiting_review','correcting','blocked','retry_wait')").fetchall()
    return any(row["issue"] != exclude and scopes_overlap(
        paths, tuple(json.loads(row["allowed_json"]))) for row in rows)
