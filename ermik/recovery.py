"""Bounded, durable retry scheduling for controller jobs.

This module only manages local intent. A caller must reconcile GitHub and the
checkout before activating a claimed retry; no remote write is authorized here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import sqlite3
from typing import Literal


RETRYABLE = frozenset({"transient", "resource", "provider", "interrupted"})
ResumeStatus = Literal["running", "correcting"]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: int = 60
    max_delay_seconds: int = 3600
    lease_seconds: int = 7200

    def __post_init__(self) -> None:
        if (type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 20
                or type(self.base_delay_seconds) is not int
                or not 1 <= self.base_delay_seconds <= 86400
                or type(self.max_delay_seconds) is not int
                or self.max_delay_seconds < self.base_delay_seconds
                or type(self.lease_seconds) is not int
                or not 1 <= self.lease_seconds <= 86400):
            raise ValueError("invalid retry policy")


@dataclass(frozen=True)
class RecoveryDecision:
    issue: int
    status: str
    attempts: int
    next_retry_at: int | None


@dataclass(frozen=True)
class RecoveryClaim:
    issue: int
    token: str
    resume_status: ResumeStatus
    attempts: int
    expires_at: int


def _epoch(now: datetime | int | float | None) -> int:
    if now is None:
        return int(datetime.now(timezone.utc).timestamp())
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("time must be timezone aware")
        return int(now.timestamp())
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("invalid time")
    return int(now)


def _delay(attempts: int, policy: RetryPolicy) -> int:
    return min(policy.max_delay_seconds,
               policy.base_delay_seconds * 2 ** min(attempts - 1, 30))


def _decision(conn: sqlite3.Connection, issue: int) -> RecoveryDecision:
    row = conn.execute("SELECT j.status,r.attempts,r.next_retry_at FROM jobs j "
                       "JOIN retry_state r ON r.issue=j.issue WHERE j.issue=?",
                       (issue,)).fetchone()
    if row is None:
        raise ValueError("retry state missing")
    return RecoveryDecision(issue, row[0], row[1], row[2])


def record_failure(conn: sqlite3.Connection, issue: int, category: str, *,
                   failure_id: str, resume_status: ResumeStatus,
                   now: datetime | int | float | None = None,
                   lease_token: str | None = None,
                   policy: RetryPolicy = RetryPolicy()) -> RecoveryDecision:
    """Record one failed *execution*, idempotently by issue and failure_id.

    The initial failed execution counts as attempt one. Later attempts are
    counted when leased, including an execution that crashes without reporting.
    An active lease must present its token. The caller commits job and retry
    state together and updates external labels only after that commit.
    """
    if type(issue) is not int or issue <= 0:
        raise ValueError("invalid issue")
    if not isinstance(category, str) or not category or len(category) > 40:
        raise ValueError("invalid failure category")
    if not isinstance(failure_id, str) or not failure_id or len(failure_id) > 128:
        raise ValueError("invalid failure ID")
    if resume_status not in ("running", "correcting"):
        raise ValueError("invalid resume status")
    instant = _epoch(now)
    job = conn.execute("SELECT status FROM jobs WHERE issue=?", (issue,)).fetchone()
    if job is None:
        raise ValueError("job missing")
    prior = conn.execute("SELECT attempts,lease_token,failure_id FROM retry_state "
                         "WHERE issue=?", (issue,)).fetchone()
    if prior is not None and prior[2] == failure_id:
        return _decision(conn, issue)
    if job[0] in ("awaiting_review", "merged"):
        return RecoveryDecision(issue, job[0], prior[0] if prior else 0, None)
    if prior is not None and prior[1] is not None and prior[1] != lease_token:
        raise ValueError("active retry lease token required")
    if prior is None and lease_token is not None:
        raise ValueError("unexpected retry lease token")
    if prior is None and job[0] not in ("running", "correcting"):
        raise ValueError("job is not executing")
    if prior is not None and prior[1] is None and lease_token is not None:
        raise ValueError("retry lease is no longer active")
    if (prior is not None and job[0] not in ("running", "correcting")
            and not (job[0] == "retry_wait" and prior[1] == lease_token
                     and lease_token is not None)):
        raise ValueError("job is not executing")
    attempts = prior[0] if prior is not None else 1
    # A provider-wide quota pause does not mean the task itself failed. Keep
    # it eligible after the provider cooldown, without consuming the bounded
    # execution-error retry allowance.
    retry = category == "provider" or (category in RETRYABLE and attempts < policy.max_attempts)
    status = "retry_wait" if retry else "blocked"
    due = instant + _delay(attempts, policy) if retry else None
    conn.execute("INSERT INTO retry_state(issue,attempts,category,resume_status,"
                 "next_retry_at,lease_token,lease_expires_at,failure_id) "
                 "VALUES(?,?,?,?,?,NULL,NULL,?) ON CONFLICT(issue) DO UPDATE SET "
                 "attempts=excluded.attempts,category=excluded.category,"
                 "resume_status=excluded.resume_status,"
                 "next_retry_at=excluded.next_retry_at,lease_token=NULL,"
                 "lease_expires_at=NULL,failure_id=excluded.failure_id",
                 (issue, attempts, category, resume_status, due, failure_id))
    conn.execute("UPDATE jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE issue=?",
                 (status, issue))
    return RecoveryDecision(issue, status, attempts, due)


def claim_due(conn: sqlite3.Connection, *, now: datetime | int | float | None = None,
              limit: int = 1, policy: RetryPolicy = RetryPolicy()) -> list[RecoveryClaim]:
    """Lease due retries; call activate_claim only after external reconciliation.

    Reclaiming an expired lease consumes an attempt and applies backoff. This
    prevents a crash loop from executing without bound.
    """
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("limit must be 1..8")
    instant = _epoch(now)
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM retry_state WHERE issue IN "
                     "(SELECT issue FROM jobs WHERE status IN ('awaiting_review','merged'))")
        expired = conn.execute("SELECT r.issue,r.attempts,r.lease_expires_at,j.status "
                               "FROM retry_state r JOIN jobs j ON j.issue=r.issue "
                               "WHERE r.lease_token IS NOT NULL AND r.lease_expires_at<=?",
                               (instant,)).fetchall()
        for row in expired:
            attempts = row[1]
            # An activated attempt might have pushed a branch before crashing.
            # It requires explicit remote reconciliation, so fail closed.
            retry = row[3] == "retry_wait" and attempts < policy.max_attempts
            due = row[2] + _delay(attempts, policy) if retry else None
            conn.execute("UPDATE retry_state SET category='interrupted',"
                         "next_retry_at=?,lease_token=NULL,lease_expires_at=NULL "
                         "WHERE issue=?", (due, row[0]))
            conn.execute("UPDATE jobs SET status=?,updated_at=CURRENT_TIMESTAMP "
                         "WHERE issue=? AND status NOT IN ('awaiting_review','merged')",
                         ("retry_wait" if retry else "blocked", row[0]))
        rows = conn.execute("SELECT r.issue,r.attempts,r.resume_status FROM retry_state r "
                            "JOIN jobs j ON j.issue=r.issue WHERE j.status='retry_wait' "
                            "AND r.lease_token IS NULL AND r.next_retry_at<=? "
                            "AND (r.attempts<? OR r.category='provider') "
                            "ORDER BY r.next_retry_at,r.issue LIMIT ?",
                            (instant, policy.max_attempts, limit)).fetchall()
        claims = []
        for row in rows:
            token = secrets.token_urlsafe(24)
            expires = instant + policy.lease_seconds
            conn.execute("UPDATE retry_state SET attempts=attempts+1,lease_token=?,"
                         "lease_expires_at=? WHERE issue=? AND lease_token IS NULL",
                         (token, expires, row[0]))
            claims.append(RecoveryClaim(row[0], token, row[2], row[1] + 1, expires))
        if own_tx:
            conn.commit()
        return claims
    except BaseException:
        if own_tx:
            conn.rollback()
        raise


def activate_claim(conn: sqlite3.Connection, claim: RecoveryClaim,
                   *, now: datetime | int | float | None = None) -> bool:
    """Move a verified, live claim into execution; stale claims cannot activate."""
    instant = _epoch(now)
    changed = conn.execute("UPDATE jobs SET status=?,updated_at=CURRENT_TIMESTAMP "
                           "WHERE issue=? AND status='retry_wait' AND EXISTS "
                           "(SELECT 1 FROM retry_state WHERE issue=? AND lease_token=? "
                           "AND lease_expires_at>?)",
                           (claim.resume_status, claim.issue, claim.issue,
                            claim.token, instant)).rowcount
    return changed == 1


def complete_claim(conn: sqlite3.Connection, claim: RecoveryClaim) -> bool:
    """Discard retry metadata only after a durable successful job transition."""
    changed = conn.execute("DELETE FROM retry_state WHERE issue=? AND lease_token=? "
                           "AND EXISTS (SELECT 1 FROM jobs WHERE issue=? "
                           "AND status IN ('awaiting_review','merged'))",
                           (claim.issue, claim.token, claim.issue)).rowcount
    return changed == 1
