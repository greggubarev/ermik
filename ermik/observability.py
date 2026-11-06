"""Read-only, bounded operational observations of controller state."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sqlite3
from typing import Iterable


ACTIVE = frozenset({"running", "correcting"})
KNOWN_STATUSES = frozenset({"running", "correcting", "retry_wait", "awaiting_review", "blocked", "merged"})
MAX_READY = 1000
MAX_JOBS = 1000
MAX_LOG_WINDOW = 64 * 1024
_RECORDED_ACTIONS = frozenset({"admitted", "claimed", "model_selected", "quota_paused",
                               "checks_passed", "pr_published", "correction_started",
                               "evidence_delivered"})


def _job_guidance(status: str, category: str | None, pr: int | None,
                  typed_reason: str | None = None) -> tuple[str | None, str | None]:
    """Offer only read-only or manual actions from durable, bounded state."""
    if status == "blocked" and typed_reason == "provider_auth":
        return "provider_auth", "verify_provider_credential_manually"
    if status == "blocked" and typed_reason == "local_check_failed":
        return "local_check_failed", "inspect_local_check_results"
    if status == "retry_wait" and typed_reason == "provider_quota":
        return "provider_quota", "wait_for_controller_reconciliation"
    if status == "blocked":
        return "requires_inspection", "reconcile_issue_branch_and_pr_manually"
    if status == "retry_wait":
        if category == "provider":
            return "provider_capacity_wait", "wait_for_controller_reconciliation"
        if category == "interrupted":
            return "interrupted_retry_wait", "wait_for_controller_reconciliation"
        return "scheduled_retry_wait", "wait_for_controller_reconciliation"
    if status == "awaiting_review":
        if type(pr) is int and pr > 0:
            return "review_pending", "verify_current_pr_head_then_review"
        return "pr_identity_unverified", "reconcile_issue_branch_and_pr_manually"
    return None, None


def _read_jobs(db: Path) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    if not db.is_file() or db.is_symlink():
        return [], [], [], [], [], []
    # Opening read-only avoids creating state or taking the controller's lock.
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        model_columns = ",".join(
            name if name in columns else f"NULL AS {name}"
            for name in ("last_model", "result_model", "attempts_count",
                         "review_next_check_at", "reason_code"))
        rows = conn.execute(
            "SELECT issue, pr, status, updated_at, error IS NOT NULL AS has_error, "
            "(SELECT kind FROM events WHERE events.issue=jobs.issue AND kind IN "
            "('admitted','claimed','model_selected','quota_paused','checks_passed',"
            "'pr_published','correction_started','evidence_delivered') "
            "ORDER BY id DESC LIMIT 1) AS last_recorded_action, "
            + model_columns + " FROM jobs ORDER BY issue LIMIT ?", (MAX_JOBS + 1,)
        ).fetchall()
        totals = conn.execute(
            "SELECT status, COUNT(*) AS amount FROM jobs GROUP BY status"
        ).fetchall()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        retries = (conn.execute("SELECT issue,attempts,category,next_retry_at FROM retry_state "
                                "ORDER BY issue LIMIT ?", (MAX_JOBS + 1,)).fetchall()
                   if "retry_state" in tables else [])
        cooldowns = (conn.execute("SELECT provider_id,retry_at FROM provider_cooldowns "
                                  "ORDER BY provider_id LIMIT 32").fetchall()
                     if "provider_cooldowns" in tables else [])
        events = (conn.execute("SELECT issue,kind,model,head_sha,created_at FROM events "
                               "ORDER BY id DESC LIMIT 100").fetchall()
                  if "events" in tables else [])
        usage = (conn.execute("SELECT provider_id,model_id,COUNT(*) AS attempts,"
                              "SUM(complete) AS measured_attempts,"
                              "SUM(COALESCE(input_tokens,0)) AS input_tokens,"
                              "SUM(COALESCE(output_tokens,0)) AS output_tokens,"
                              "SUM(COALESCE(reasoning_tokens,0)) AS reasoning_tokens,"
                              "SUM(COALESCE(cache_read_tokens,0)) AS cache_read_tokens,"
                              "SUM(COALESCE(cache_write_tokens,0)) AS cache_write_tokens,"
                              "SUM(COALESCE(total_tokens,0)) AS total_tokens,"
                              "SUM(COALESCE(estimated_cost,0)) AS estimated_cost "
                              "FROM usage_events GROUP BY provider_id,model_id "
                              "ORDER BY provider_id,model_id LIMIT 32").fetchall()
                 if "usage_events" in tables else [])
        return rows, totals, retries, cooldowns, events, usage
    finally:
        conn.close()


def status_snapshot(state_dir: Path, *, max_parallel: int,
                    ready_issues: Iterable[int] | None = None,
                    observed_at: datetime | None = None) -> dict:
    """Return a safe local snapshot; pass verified remote queue IDs separately.

    None means the remote queue was not queried. This function never contacts a
    provider, initializes state, or exposes task text, paths, branches, or errors.
    """
    if not 1 <= max_parallel <= 8:
        raise ValueError("max_parallel must be 1..8")
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    rows, totals, retries, cooldowns, events, usage = _read_jobs(state_dir / "state.sqlite3")
    truncated = len(rows) > MAX_JOBS
    rows = rows[:MAX_JOBS]
    jobs = []
    counts: dict[str, int] = {}
    for row in totals:
        status = row["status"] if row["status"] in KNOWN_STATUSES else "unknown"
        counts[status] = counts.get(status, 0) + row["amount"]
    for row in rows:
        status = row["status"] if row["status"] in KNOWN_STATUSES else "unknown"
        jobs.append({"issue": row["issue"], "pr": row["pr"], "status": status,
                     "updated_at": row["updated_at"], "has_error": bool(row["has_error"]),
                     "last_model": row["last_model"], "result_model": row["result_model"],
                     "attempts": row["attempts_count"],
                     "next_review_at": row["review_next_check_at"]
                     if status == "awaiting_review" and row["review_next_check_at"] else None})
    active = sum(counts.get(status, 0) for status in ACTIVE)
    ready = None
    if ready_issues is not None:
        unique = set()
        for issue in ready_issues:
            if type(issue) is not int or issue < 1:
                raise ValueError("ready_issues must contain positive integer IDs")
            unique.add(issue)
            if len(unique) > MAX_READY:
                raise ValueError("ready_issues exceeds display limit")
        ready = sorted(unique)
    retry_by_issue = {row["issue"]: row for row in retries}
    for row, job in zip(rows, jobs):
        retry = retry_by_issue.get(job["issue"])
        if job["attempts"] is None:
            job["attempts"] = retry["attempts"] if retry else None
        job["next_retry_at"] = retry["next_retry_at"] if retry else None
        reason, next_action = _job_guidance(job["status"],
                                            retry["category"] if retry else None,
                                            job["pr"], row["reason_code"])
        job["reason_code"] = reason
        job["next_action"] = next_action
        recorded = row["last_recorded_action"]
        job["last_recorded_action"] = recorded if recorded in _RECORDED_ACTIONS else None
    return {
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "queue": {"ready_issues": ready, "count": len(ready) if ready is not None else None},
        "slots": {"capacity": max_parallel, "active": active,
                  "available": max(0, max_parallel - active), "over_capacity": active > max_parallel},
        "counts": counts, "jobs": jobs, "jobs_truncated": truncated,
        "provider_cooldown": [{"provider": row["provider_id"],
                               "retry_at": row["retry_at"]} for row in cooldowns],
        "usage": [{"provider": row["provider_id"], "model": row["model_id"],
                   "attempts": row["attempts"],
                   "measured_attempts": row["measured_attempts"],
                   "unknown_attempts": row["attempts"] - row["measured_attempts"],
                   "input_tokens": row["input_tokens"],
                   "output_tokens": row["output_tokens"],
                   "reasoning_tokens": row["reasoning_tokens"],
                   "cache_read_tokens": row["cache_read_tokens"],
                   "cache_write_tokens": row["cache_write_tokens"],
                   "total_tokens": row["total_tokens"],
                   "estimated_cost": row["estimated_cost"]} for row in usage],
        "attempts": {"retry_jobs": len(retries),
                     "total": sum(row["attempts"] for row in retries)} if rows else None,
        "events": [{"issue": row["issue"], "kind": row["kind"],
                    "model": row["model"], "created_at": row["created_at"]}
                   for row in events],
    }


def resource_guard(workspace: Path, *, min_free_bytes: int,
                   max_worktrees: int, disk_free_bytes: int | None = None,
                   state_dir: Path | None = None,
                   state_disk_free_bytes: int | None = None,
                   retained_issues: frozenset[int] = frozenset()) -> dict:
    """Preflight signal for callers to check before claiming another job.

    No cleanup is performed. Missing roots are measured at their nearest
    existing parents; only direct issue-* directories under jobs are counted.
    A caller may exclude independently verified, retained published checkouts
    from the active slot count; they still consume disk space.
    """
    if min_free_bytes < 0 or max_worktrees < 0:
        raise ValueError("resource thresholds must be nonnegative")
    def existing_probe(root: Path, name: str) -> Path:
        if root.is_symlink():
            raise ValueError(f"{name} cannot be a symlink")
        probe = root
        while not probe.exists():
            if probe.parent == probe:
                raise ValueError(f"no existing parent for {name}")
            probe = probe.parent
        if not probe.is_dir():
            raise ValueError(f"{name} parent is not a directory")
        return probe

    probe = existing_probe(workspace, "workspace")
    free = shutil.disk_usage(probe).free if disk_free_bytes is None else disk_free_bytes
    if free < 0:
        raise ValueError("disk_free_bytes must be nonnegative")
    state_free = None
    if state_dir is not None:
        state_probe = existing_probe(state_dir, "state_dir")
        state_free = (shutil.disk_usage(state_probe).free
                      if state_disk_free_bytes is None else state_disk_free_bytes)
        if state_free < 0:
            raise ValueError("state_disk_free_bytes must be nonnegative")
    elif state_disk_free_bytes is not None:
        raise ValueError("state_dir required for state_disk_free_bytes")
    jobs_dir = workspace / "jobs"
    if jobs_dir.is_symlink():
        raise ValueError("jobs directory cannot be a symlink")
    worktrees = 0
    if jobs_dir.exists():
        if not jobs_dir.is_dir():
            raise ValueError("jobs path is not a directory")
        worktrees = sum(child.name.startswith("issue-")
                        and (not child.name.removeprefix("issue-").isdigit()
                             or int(child.name.removeprefix("issue-")) not in retained_issues
                             or child.is_symlink() or not child.is_dir())
                        for child in jobs_dir.iterdir())
    reasons = []
    if free < min_free_bytes:
        reasons.append("low_disk_space")
    if state_free is not None and state_free < min_free_bytes:
        reasons.append("low_state_disk_space")
    if worktrees >= max_worktrees:
        reasons.append("worktree_limit")
    result = {"admit": not reasons, "disk_free_bytes": free,
              "worktrees": worktrees, "reasons": reasons}
    if state_free is not None:
        result["state_disk_free_bytes"] = state_free
    return result


def log_window_metadata(path: Path, *, max_bytes: int = MAX_LOG_WINDOW) -> dict:
    """Inspect a bounded log window without returning untrusted log content."""
    if not 1 <= max_bytes <= MAX_LOG_WINDOW:
        raise ValueError("max_bytes must be within the log window limit")
    if path.is_symlink() or not path.is_file():
        raise ValueError("log must be a regular file")
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        window = handle.read(max_bytes)
    return {"size_bytes": size, "window_bytes": len(window),
            "truncated": size > max_bytes,
            "window_sha256": hashlib.sha256(window).hexdigest()}
