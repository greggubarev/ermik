"""Read-only, bounded outcome report across controller and maintainer state."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .config import Config
from .decision_trail import read_decisions
from .state import SCHEMA_VERSION


MAX_JOBS = 10000
MAX_ATTEMPTS = 100000
MAX_EVENTS = 100000
USAGE_COLUMNS = ("input_tokens", "output_tokens", "reasoning_tokens",
                 "cache_read_tokens", "cache_write_tokens", "total_tokens")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("outcome timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _db_time(value: str) -> datetime:
    # SQLite CURRENT_TIMESTAMP is UTC but has no suffix.
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _usage_summary(rows: list[sqlite3.Row], missing_rows: int) -> dict:
    """Sum complete attempts; shares require every attempt and a nonzero denominator.

    Cache-read share divides cache reads by input plus cache reads (prompt
    tokens). Output share divides output by reported total tokens. The latter
    already includes every token category, so categories are never added to it.
    """
    known = [row for row in rows if row["complete"] == 1
             and all(row[column] is not None for column in USAGE_COLUMNS)]
    unknown = missing_rows + len(rows) - len(known)
    totals = {column: sum(row[column] for row in known) for column in USAGE_COLUMNS}
    prompt = totals["input_tokens"] + totals["cache_read_tokens"]
    total = totals["total_tokens"]
    return {
        **{f"observed_{column}": value for column, value in totals.items()},
        "unknown_token_attempts": unknown,
        "cache_read_share_denominator_tokens": prompt,
        "cache_read_share": totals["cache_read_tokens"] / prompt
        if not unknown and prompt else None,
        "output_share_denominator_tokens": total,
        "output_share": totals["output_tokens"] / total
        if not unknown and total else None,
    }


def _read_controller(cfg: Config) -> tuple[list[sqlite3.Row], list[sqlite3.Row], dict[int, str], list[sqlite3.Row]]:
    db = cfg.state_dir / "state.sqlite3"
    if not db.is_file() or db.is_symlink():
        raise ValueError("controller state database is missing or unsafe")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError(f"outcome export requires controller state schema v{SCHEMA_VERSION}")
        jobs = conn.execute(
            "SELECT issue,pr,head_sha,status,route_key,task_set,complexity,profile_sha,attempts_count "
            "FROM jobs ORDER BY issue LIMIT ?",
            (MAX_JOBS + 1,)).fetchall()
        attempts = conn.execute(
            "SELECT issue,phase,provider_id,model_id,complete,input_tokens,output_tokens,"
            "reasoning_tokens,cache_read_tokens,cache_write_tokens,total_tokens,"
            "estimated_cost,result_code,created_at,finished_at,duration_ms "
            "FROM usage_events ORDER BY issue,created_at,rowid LIMIT ?",
            (MAX_ATTEMPTS + 1,)).fetchall()
        admissions = conn.execute("SELECT issue,admitted_at FROM admissions "
                                  "ORDER BY issue LIMIT ?", (MAX_JOBS + 1,)).fetchall()
        events = conn.execute("SELECT issue,kind FROM events WHERE kind IN "
                              "('retry_scheduled','retry_started','quota_paused') "
                              "ORDER BY id LIMIT ?", (MAX_EVENTS + 1,)).fetchall()
        if (len(jobs) > MAX_JOBS or len(attempts) > MAX_ATTEMPTS
                or len(admissions) > MAX_JOBS or len(events) > MAX_EVENTS):
            raise ValueError("outcome export exceeds bounded state size")
        return jobs, attempts, {row["issue"]: row["admitted_at"] for row in admissions}, events
    finally:
        conn.close()


def build_outcome_report(cfg: Config, maintainer_state_dir: Path,
                         *, period: str | None = None) -> dict:
    """Report admission-month cohorts; null means unknown, never zero.

    `period` is YYYY-MM. A PR belongs to its admission month even if the
    maintainer merges it later. Costs include every recorded attempt for all
    jobs in that cohort, including failed jobs and retries. The first-review
    rate divides accepted-on-first-review PRs by PRs with a recorded review.
    Elapsed mean covers accepted PRs with a known admission and merge time;
    missing elapsed values are counted separately. Observed token categories
    are summed over complete attempts only. Shares are unavailable if any
    attempt has missing usage or if their documented denominator is zero.
    """
    if period is not None:
        try:
            parsed = datetime.strptime(period, "%Y-%m")
        except ValueError as exc:
            raise ValueError("period must be YYYY-MM") from exc
        if parsed.strftime("%Y-%m") != period:
            raise ValueError("period must be YYYY-MM")
    maintainer_state_dir = Path(maintainer_state_dir)
    if (maintainer_state_dir.resolve() == cfg.state_dir.resolve()
            or maintainer_state_dir.is_symlink()):
        raise ValueError("maintainer state must be a separate directory")
    maintainer_cfg = replace(cfg, state_dir=maintainer_state_dir)
    jobs, attempts, admitted, events = _read_controller(cfg)
    by_issue: dict[int, list[sqlite3.Row]] = {}
    for attempt in attempts:
        by_issue.setdefault(attempt["issue"], []).append(attempt)
    events_by_issue: dict[int, dict[str, int]] = {}
    for event in events:
        counts = events_by_issue.setdefault(event["issue"], {})
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    results = []
    for job in jobs:
        issue, pr = job["issue"], job["pr"]
        admitted_at = admitted.get(issue)
        admission_month = _db_time(admitted_at).strftime("%Y-%m") if admitted_at else None
        if period is not None and admission_month != period:
            continue
        route = job["route_key"] or "unknown"
        journal_entries = read_decisions(maintainer_cfg, pr)["entries"] if pr else []
        matching = [entry for entry in journal_entries
                    if entry["issue"] == issue and entry["head"] == job["head_sha"]]
        merged = [entry for entry in matching if entry["decision"] == "merged"]
        accepted = bool(merged)
        first_review = next((entry for entry in journal_entries
                             if entry["issue"] == issue
                             and entry["decision"] == "reviewed"), None)
        accepted_at = _utc(merged[-1]["timestamp"]) if merged else None
        correction_heads = {entry["head"] for entry in journal_entries
                            if entry["issue"] == issue
                            and entry["decision"] == "changes_requested"}
        if accepted:
            decision_state = "confirmed_merged"
        elif journal_entries and not matching:
            decision_state = "stale"
        elif not journal_entries:
            decision_state = "missing"
        else:
            decision_state = "unaccepted"
        elapsed_ms = (round((accepted_at - _db_time(admitted_at)).total_seconds() * 1000)
                      if accepted_at and admitted_at else None)
        if elapsed_ms is not None and elapsed_ms < 0:
            elapsed_ms = None
        rows = by_issue.get(issue, [])
        recorded_attempts = job["attempts_count"]
        attempt_history_complete = recorded_attempts == len(rows) and bool(rows)
        missing_attempt_rows = max(0, recorded_attempts - len(rows))
        event_counts = events_by_issue.get(issue, {})
        usage = _usage_summary(rows, missing_attempt_rows)
        observed_cost = sum(row["estimated_cost"] for row in rows
                            if row["estimated_cost"] is not None)
        unknown_cost = missing_attempt_rows + sum(
            row["estimated_cost"] is None for row in rows)
        providers = [row["provider_id"] for row in rows if row["provider_id"]]
        results.append({
            "issue": issue, "pr": pr, "head": job["head_sha"],
            "period": admission_month, "route": route, "task_set": job["task_set"],
            "complexity": job["complexity"], "profile_sha256": job["profile_sha"],
            "accepted": accepted, "decision_state": decision_state,
            "accepted_at": accepted_at.isoformat() if accepted_at else None,
            "elapsed_ms": elapsed_ms,
            "first_review_accepted": bool(accepted and first_review
                                          and first_review["head"] == merged[-1]["head"]
                                          and first_review["evidence_sha256"]
                                          == merged[-1]["evidence_sha256"]
                                          and not correction_heads),
            "first_review_observed": first_review is not None,
            "distinct_corrections": len(correction_heads),
            "attempts": len(rows),
            "expected_attempts": recorded_attempts,
            "missing_attempt_rows": missing_attempt_rows,
            "attempt_history_complete": attempt_history_complete,
            "missing_attempt_history": not attempt_history_complete,
            "initial_attempts": sum(row["phase"] == "initial" for row in rows),
            "correction_attempts": sum(row["phase"] == "correction" for row in rows),
            "legacy_phase_attempts": sum(row["phase"] is None for row in rows),
            "observed_providers": sorted(set(providers)),
            "provider_attribution_complete": all(bool(row["provider_id"])
                                                  for row in rows),
            "provider_switches": sum(left != right for left, right
                                     in zip(providers, providers[1:])),
            "failed_attempts": sum(row["result_code"] not in (0, -2) for row in rows),
            "pending_attempts": sum(row["finished_at"] is None for row in rows),
            "retry_scheduled_events": event_counts.get("retry_scheduled", 0),
            "retry_started_events": event_counts.get("retry_started", 0),
            "quota_paused_events": event_counts.get("quota_paused", 0),
            "rate_limit_attempts": sum(row["result_code"] == 75 for row in rows),
            "observed_tokens": usage["observed_total_tokens"],
            **usage,
            "observed_attempt_duration_ms": sum(row["duration_ms"] for row in rows
                                                if row["duration_ms"] is not None),
            "unknown_duration_attempts": missing_attempt_rows + sum(
                row["duration_ms"] is None for row in rows),
            "observed_estimated_cost": round(observed_cost, 8),
            "unknown_cost_attempts": unknown_cost,
            "billed_cost": None,
        })
    groups: dict[tuple[str | None, str, str | None], list[dict]] = {}
    for row in results:
        groups.setdefault((row["period"], row["route"], row["task_set"]), []).append(row)
    cohorts = []
    for (month, route, task_set), rows in sorted(
            groups.items(), key=lambda item: (item[0][0] or "", item[0][1], item[0][2] or "")):
        accepted = sum(row["accepted"] for row in rows)
        cost = sum(row["observed_estimated_cost"] for row in rows)
        missing = sum(row["unknown_cost_attempts"] for row in rows)
        usage_totals = {f"observed_{column}": sum(row[f"observed_{column}"]
                         for row in rows) for column in USAGE_COLUMNS}
        unknown_usage = sum(row["unknown_token_attempts"] for row in rows)
        prompt = (usage_totals["observed_input_tokens"]
                  + usage_totals["observed_cache_read_tokens"])
        total = usage_totals["observed_total_tokens"]
        cohorts.append({
            "period": month, "route": route, "task_set": task_set, "jobs": len(rows),
            "accepted_prs": accepted,
            "first_review_acceptances": sum(row["first_review_accepted"] for row in rows),
            "first_review_rate_denominator": sum(row["first_review_observed"] for row in rows),
            "first_review_acceptance_rate": (
                sum(row["first_review_accepted"] for row in rows)
                / sum(row["first_review_observed"] for row in rows)
                if any(row["first_review_observed"] for row in rows) else None),
            "elapsed_observed_prs": sum(row["accepted"] and row["elapsed_ms"] is not None
                                        for row in rows),
            "elapsed_unknown_prs": sum(row["accepted"] and row["elapsed_ms"] is None
                                       for row in rows),
            "observed_mean_elapsed_ms": (
                round(sum(row["elapsed_ms"] for row in rows
                          if row["accepted"] and row["elapsed_ms"] is not None)
                      / sum(row["accepted"] and row["elapsed_ms"] is not None
                            for row in rows))
                if any(row["accepted"] and row["elapsed_ms"] is not None
                       for row in rows) else None),
            "distinct_corrections": sum(row["distinct_corrections"] for row in rows),
            "attempts": sum(row["attempts"] for row in rows),
            "missing_attempt_history_jobs": sum(row["missing_attempt_history"] for row in rows),
            "missing_attempt_rows": sum(row["missing_attempt_rows"] for row in rows),
            "initial_attempts": sum(row["initial_attempts"] for row in rows),
            "correction_attempts": sum(row["correction_attempts"] for row in rows),
            "legacy_phase_attempts": sum(row["legacy_phase_attempts"] for row in rows),
            "provider_switches": sum(row["provider_switches"] for row in rows),
            "failed_attempts": sum(row["failed_attempts"] for row in rows),
            "pending_attempts": sum(row["pending_attempts"] for row in rows),
            "retry_scheduled_events": sum(row["retry_scheduled_events"] for row in rows),
            "retry_started_events": sum(row["retry_started_events"] for row in rows),
            "quota_paused_events": sum(row["quota_paused_events"] for row in rows),
            "rate_limit_attempts": sum(row["rate_limit_attempts"] for row in rows),
            "observed_tokens": total,
            **usage_totals,
            "unknown_token_attempts": unknown_usage,
            "cache_read_share_denominator_tokens": prompt,
            "cache_read_share": usage_totals["observed_cache_read_tokens"] / prompt
            if not unknown_usage and prompt else None,
            "output_share_denominator_tokens": total,
            "output_share": usage_totals["observed_output_tokens"] / total
            if not unknown_usage and total else None,
            "observed_attempt_duration_ms": sum(row["observed_attempt_duration_ms"]
                                                for row in rows),
            "unknown_duration_attempts": sum(row["unknown_duration_attempts"]
                                             for row in rows),
            "observed_estimated_cost": round(cost, 8),
            "unknown_cost_attempts": missing,
            "estimated_cost_per_accepted_pr": round(cost / accepted, 8)
            if accepted and not missing and all(row["attempt_history_complete"]
                                                for row in rows) else None,
            "billed_cost": None, "billed_cost_per_accepted_pr": None,
        })
    return {"version": 1, "repo": cfg.repo, "period_basis": "admission_month_utc",
            "period_filter": period, "jobs": results, "cohorts": cohorts}


def build_quality_snapshot(cfg: Config, maintainer_state_dir: Path,
                           *, observed_at: datetime | None = None,
                           source_report: dict | None = None) -> dict:
    """Measure first-review acceptance among confirmed, single-provider merges.

    Each eligible merged PR contributes one sample. Incomplete or ambiguous
    attempt histories cannot establish provider attribution and are excluded.
    """
    instant = datetime.now(timezone.utc) if observed_at is None else observed_at
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("observed_at must have a timezone")
    report = (build_outcome_report(cfg, maintainer_state_dir)
              if source_report is None else source_report)
    providers: dict[str, dict] = {
        provider_id: {"samples": 0, "first_review_acceptances": 0,
                      "quality_score": None}
        for provider_id in sorted({model.provider_id for model in cfg.models})
    }
    for job in report["jobs"]:
        observed = job["observed_providers"]
        if (job["decision_state"] != "confirmed_merged"
                or not job["attempt_history_complete"]
                or job["pending_attempts"]
                or not job["provider_attribution_complete"]
                or len(observed) != 1):
            continue
        counts = providers.setdefault(observed[0], {
            "samples": 0, "first_review_acceptances": 0, "quality_score": None,
        })
        counts["samples"] += 1
        counts["first_review_acceptances"] += int(job["first_review_accepted"])
    for counts in providers.values():
        counts["quality_score"] = (counts["first_review_acceptances"]
                                   / counts["samples"] if counts["samples"] else None)
    return {"version": 1, "repo": cfg.repo,
            "observed_at": instant.astimezone(timezone.utc).isoformat(),
            "providers": providers}


def audit_cohort(report: dict) -> dict:
    """Identify the exact accepted PRs and the full bounded source report."""
    accepted = sorted(({"issue": row["issue"], "pr": row["pr"],
                        "head": row["head"],
                        "provider": row["observed_providers"][0],
                        "first_review_accepted": row["first_review_accepted"]}
                       for row in report["jobs"]
                       if row["decision_state"] == "confirmed_merged"
                       and row["attempt_history_complete"]
                       and not row["pending_attempts"]
                       and row["provider_attribution_complete"]
                       and len(row["observed_providers"]) == 1),
                      key=lambda row: (row["issue"], row["pr"]))
    if len(accepted) > 256:
        raise ValueError("audit cohort exceeds 256 accepted PRs")
    def digest(value: object) -> str:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")
        return hashlib.sha256(data).hexdigest()
    return {"accepted_prs": accepted, "accepted_prs_sha256": digest(accepted),
            "source_report_sha256": digest(report)}
