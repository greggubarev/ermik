"""Read-only, conservative advice from frozen local outcome histories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from .config import Config
from .outcomes import build_outcome_report
from .policy import parse_issue
from .runtime import _profile_sha, _route_key, _task_set, labels


TIERS = ("easy", "medium", "hard")
MAX_MERGE_AGE = timedelta(days=30)
RATE_TOLERANCE = 0.05


def recommend_effort(cfg: Config, issue: dict, maintainer_state_dir,
                     *, observed_at: datetime | None = None,
                     source_report: dict | None = None) -> dict:
    """Return a bounded recommendation or a specific refusal; never mutate policy."""
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("observed_at must have a timezone")
    now = now.astimezone(timezone.utc)
    number = issue.get("number")
    answer = {"decision": "refused", "repo": cfg.repo, "issue": number,
              "observed_at": now.isoformat(), "proposed_tier": None}

    def refuse(reason: str) -> dict:
        return {**answer, "refusal_reason": reason}

    names = labels(issue)
    if issue.get("state") != "open" or "queue:worker" not in names or names.intersection(
            {"state:proposed", "state:ready"}) != {"state:proposed"}:
        return refuse("issue_must_be_open_proposed_worker")
    try:
        parse_issue(issue.get("body") or "")
    except ValueError:
        return refuse("issue_scope_invalid")
    try:
        task_set = _task_set(issue)
    except ValueError:
        return refuse("task_set_missing_or_conflicting")
    if task_set is None:
        return refuse("task_set_missing_or_conflicting")
    route = _route_key(issue)
    if route is None or (route != "route:auto" and route not in cfg.routes):
        return refuse("route_missing_or_unconfigured")
    if not cfg.complexity_profiles:
        return refuse("complexity_profiles_unconfigured")
    digests = {}
    for tier in TIERS:
        try:
            digests[tier] = _profile_sha(cfg, tier, route)
        except (KeyError, ValueError):
            continue
    if len(digests) < 2:
        return refuse("fewer_than_two_configured_tiers")
    report = (build_outcome_report(cfg, maintainer_state_dir)
              if source_report is None else source_report)
    rows = [row for row in report["jobs"]
            if row["task_set"] == task_set and row["route"] == route]
    if not rows:
        return refuse("cohort_empty")
    if any(row["complexity"] not in digests for row in rows):
        return refuse("cohort_tier_unconfigured_or_unknown")
    if any(row["profile_sha256"] != digests[row["complexity"]] for row in rows):
        return refuse("cohort_profile_changed_or_unknown")
    if any(not row["attempt_history_complete"] or row["pending_attempts"]
           or row["unknown_token_attempts"] or row["unknown_cost_attempts"]
           or row["unknown_duration_attempts"] or row["legacy_phase_attempts"]
           for row in rows):
        return refuse("cohort_attempt_usage_or_cost_incomplete")
    if any(row["pr"] is None and row["job_status"] != "blocked" for row in rows):
        return refuse("cohort_unfinished_job_without_pr")
    if any((row["pr"] and row["decision_state"] != "confirmed_merged")
           or (row["accepted"] and not row["first_review_observed"])
           or (not row["accepted"] and row["pr"])
           for row in rows):
        return refuse("cohort_review_or_merge_unconfirmed")
    if any(row["accepted"] and (not row["accepted_at"]
           or not now - MAX_MERGE_AGE <= datetime.fromisoformat(row["accepted_at"]) <= now)
           for row in rows):
        return refuse("cohort_merge_stale_or_future")
    grouped = {}
    for tier in digests:
        items = [row for row in rows if row["complexity"] == tier]
        accepted = sum(row["accepted"] for row in items)
        reviewed = sum(row["first_review_observed"] for row in items)
        attempts = sum(row["attempts"] for row in items)
        cost = sum(row["observed_estimated_cost"] for row in items)
        grouped[tier] = {
            "jobs": len(items), "accepted_prs": accepted, "attempts": attempts,
            "failed_attempts": sum(row["failed_attempts"] for row in items),
            "failed_attempt_rate": sum(row["failed_attempts"] for row in items) / attempts
            if attempts else None,
            "first_review_acceptances": sum(row["first_review_accepted"] for row in items),
            "first_review_denominator": reviewed,
            "first_review_acceptance_rate": sum(row["first_review_accepted"] for row in items)
            / reviewed if reviewed else None,
            "observed_total_tokens": sum(row["observed_total_tokens"] for row in items),
            "estimated_cost_all_attempts": round(cost, 8),
            "estimated_cost_per_accepted_pr": round(cost / accepted, 8) if accepted else None,
            "profile_sha256": digests[tier],
        }
    eligible = [tier for tier in TIERS if tier in grouped
                and grouped[tier]["accepted_prs"] >= cfg.quality_min_samples]
    if len(eligible) < 2:
        return {**refuse("insufficient_accepted_prs_per_tier"), "cohort": grouped,
                "min_samples_per_tier": cfg.quality_min_samples}
    best_acceptance = max(grouped[tier]["first_review_acceptance_rate"] for tier in eligible)
    best_failure = min(grouped[tier]["failed_attempt_rate"] for tier in eligible)
    qualifying = [tier for tier in eligible
                  if grouped[tier]["first_review_acceptance_rate"] >= best_acceptance - RATE_TOLERANCE
                  and grouped[tier]["failed_attempt_rate"] <= best_failure + RATE_TOLERANCE]
    if not qualifying:
        return {**refuse("no_tier_within_quality_and_failure_tolerance"),
                "cohort": grouped}
    identities = sorted((row["issue"], row["head"], row["profile_sha256"])
                        for row in rows)
    cohort_digest = hashlib.sha256(json.dumps(identities, separators=(",", ":"),
                                              ensure_ascii=True).encode("ascii")).hexdigest()
    return {**answer, "decision": "recommended", "proposed_tier": qualifying[0],
            "task_set": task_set, "route": route, "profile_sha256": digests[qualifying[0]],
            "cohort": grouped, "cohort_jobs": len(rows),
            "cohort_identity_sha256": cohort_digest,
            "eligible_tiers": eligible,
            "min_samples_per_tier": cfg.quality_min_samples,
            "max_merge_age_days": MAX_MERGE_AGE.days,
            "rate_tolerance_percentage_points": 5,
            "usage_basis": "all recorded job attempts, complete token observations",
            "cost_basis": "estimated, all attempts including failures and retries; not billed",
            "caveat": "Observational task-set labels do not prove equal difficulty or causal effort effects. Manual admission and review remain required."}
