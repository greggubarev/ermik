"""Order configured model candidates using a complete, private quality snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
from typing import Sequence

from .config import Config
from .providers import ModelCandidate
from .outcomes import audit_cohort, build_quality_snapshot


_MAX_SNAPSHOT_BYTES = 64 * 1024
_SNAPSHOT_FIELDS = {"version", "repo", "observed_at", "providers"}
_PROVIDER_FIELDS = {"samples", "first_review_acceptances", "quality_score"}


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate snapshot field")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid snapshot number")


def _read_snapshot(path: Path) -> dict:
    """Read at most 64 KiB from an owned private regular file without following it."""
    if path.is_symlink():
        raise ValueError("snapshot path is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SNAPSHOT_BYTES:
            raise ValueError("invalid snapshot file")
        if os.name == "posix" and (info.st_uid != os.geteuid()
                                    or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("snapshot file is not private")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(_MAX_SNAPSHOT_BYTES + 1)
        if len(data) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot is too large")
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant)
        if type(result) is not dict:
            raise ValueError("snapshot must be an object")
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _scores(snapshot: dict, cfg: Config, candidates: tuple[ModelCandidate, ...],
            instant: datetime) -> dict[str, float]:
    if (set(snapshot) != _SNAPSHOT_FIELDS or type(snapshot["version"]) is not int
            or snapshot["version"] != 1 or snapshot["repo"] != cfg.repo
            or type(snapshot["observed_at"]) is not str
            or type(snapshot["providers"]) is not dict):
        raise ValueError("invalid snapshot header")
    observed_text = snapshot["observed_at"]
    observed = datetime.fromisoformat(observed_text)
    if (observed.tzinfo is None or observed.utcoffset() is None
            or observed.utcoffset().total_seconds() != 0):
        raise ValueError("snapshot timestamp must be UTC")
    age = (instant - observed).total_seconds()
    if not 0 <= age <= cfg.quality_max_age_seconds:
        raise ValueError("snapshot is stale or future dated")

    scores = {}
    for provider_id, row in snapshot["providers"].items():
        if (type(provider_id) is not str or not provider_id
                or type(row) is not dict or set(row) != _PROVIDER_FIELDS):
            raise ValueError("invalid provider measurement")
        samples = row["samples"]
        accepted = row["first_review_acceptances"]
        score = row["quality_score"]
        if (type(samples) is not int or samples < 0 or type(accepted) is not int
                or not 0 <= accepted <= samples):
            raise ValueError("invalid provider counts")
        if samples == 0:
            if score is not None:
                raise ValueError("zero-sample score must be null")
        elif (type(score) is not float or not math.isfinite(score)
              or not 0 <= score <= 1
              or not math.isclose(score, accepted / samples, rel_tol=0,
                                  abs_tol=1e-12)):
            raise ValueError("score disagrees with counts")
        if samples >= cfg.quality_min_samples:
            scores[provider_id] = float(score)
    if any(candidate.provider_id not in scores for candidate in candidates):
        raise ValueError("incomplete provider measurements")
    return scores


def order_candidates(cfg: Config, candidates: Sequence[ModelCandidate],
                     now: datetime | None = None) -> tuple[tuple[ModelCandidate, ...], str]:
    """Prefer measured quality only when every candidate provider is qualified.

    Invalid or unavailable measurements leave the configured priority untouched.
    The reason contains no snapshot data or local file path.
    """
    priority = tuple(candidates)
    if len({candidate.provider_id for candidate in priority}) <= 1:
        return priority, "priority"
    path = cfg.quality_metrics_file
    if path is None:
        return priority, "priority"
    instant = datetime.now(timezone.utc) if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    try:
        scores = _scores(_read_snapshot(Path(path)), cfg, priority, instant)
    except (OSError, ValueError, UnicodeError, TypeError, OverflowError):
        return priority, "priority"
    return tuple(sorted(priority, key=lambda candidate: -scores[candidate.provider_id])), "quality"


def audit_route_order(cfg: Config, report: dict, snapshot_path: Path,
                      candidates: Sequence[ModelCandidate],
                      now: datetime | None = None) -> dict:
    """Preview a frozen cohort's order; never update routing or controller state."""
    priority = tuple(candidates)
    configured = [candidate.ref for candidate in priority]
    instant = datetime.now(timezone.utc) if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    current, current_policy = order_candidates(cfg, priority, instant)
    effective = [candidate.ref for candidate in current]
    result = {"repo": cfg.repo, "mode": "read_only", "configured_order": configured,
              "current_order": effective, "current_policy": current_policy,
              "proposed_order": effective, "eligible_order": effective,
              "decision": "refused", "fallback": "current_runtime_order",
              "gates": {"admission": "required", "checks": "required",
                        "maintainer_review": "required"},
              "quality_min_samples": cfg.quality_min_samples,
              "quality_max_age_seconds": cfg.quality_max_age_seconds}
    try:
        snapshot = _read_snapshot(Path(snapshot_path))
        if (set(snapshot) != {"version", "repo", "observed_at", "providers",
                              "cohort", "gates", "thresholds"}
                or type(snapshot["version"]) is not int or snapshot["version"] != 2
                or snapshot["repo"] != cfg.repo
                or snapshot["thresholds"] != {
                    "min_samples": cfg.quality_min_samples,
                    "max_age_seconds": cfg.quality_max_age_seconds}
                or snapshot["gates"] != result["gates"]):
            raise ValueError("invalid audit snapshot")
        cohort = audit_cohort(report)
        if cohort != snapshot["cohort"]:
            raise ValueError("mismatched cohort")
        expected = build_quality_snapshot(cfg, cfg.state_dir, source_report=report,
                                          observed_at=datetime.fromisoformat(
                                              snapshot["observed_at"]))
        if snapshot["providers"] != expected["providers"]:
            raise ValueError("mismatched measurements")
        observed = datetime.fromisoformat(snapshot["observed_at"])
        if (observed.tzinfo is None or observed.utcoffset() is None
                or observed.utcoffset().total_seconds() != 0):
            raise ValueError("invalid audit timestamp")
        quality = {key: snapshot[key] for key in _SNAPSHOT_FIELDS}
        quality["version"] = 1
    except (OSError, ValueError, UnicodeError, TypeError, OverflowError, KeyError):
        result["refusal_reason"] = "invalid_or_incomplete_evidence"
        return result
    result.update({"observed_at": observed.isoformat(), "cohort": cohort,
                   "provider_samples": {
                       provider: expected["providers"].get(provider, {}).get("samples")
                       for provider in sorted({item.provider_id for item in priority})}})
    try:
        scores = _scores(quality, cfg, priority, instant)
        if len({candidate.provider_id for candidate in priority}) < 2:
            raise ValueError("fewer than two eligible providers")
        proposed = sorted(priority, key=lambda candidate: -scores[candidate.provider_id])
    except (OSError, ValueError, UnicodeError, TypeError, OverflowError, KeyError):
        result["refusal_reason"] = "insufficient_or_stale_measurements"
        return result
    result.update({"decision": "audited",
                   "proposed_order": [item.ref for item in proposed]})
    return result
