"""Read-only, bounded maintainer view of one live PR and its evidence."""

from __future__ import annotations

import re

from .config import Config
from .evidence_export import _github, _live
from .evidence_transport import _published_records, fetch_manifest
from .github import GitHub, GitHubError
from .review import _checks_sha256, _load, _review_policy
from .runtime import (WorkflowError, published_image_id,
                      published_image_config_digest)


_UNSAFE_TEXT = re.compile(
    r"(?i)(?:https?://|[/\\]|@|\b(?:token|password|secret|credential|bearer|"
    r"authorization|private[ _-]?key)\b|\b[A-Za-z0-9_+=.-]{32,}\b)"
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_PROSE = re.compile(r"[A-Za-z0-9 .,;:!?()'\"-]+\Z")


def _excerpt(value: str, issue: int, limit: int) -> str:
    """Show simple short issue prose; refer to GitHub for unsafe or long text."""
    if not isinstance(value, str) or _UNSAFE_TEXT.search(value):
        return f"See Issue #{issue}"
    text = " ".join(value.split())
    if not text or len(text) > limit or not _SAFE_PROSE.fullmatch(text):
        return f"See Issue #{issue}"
    return text


def _review_summary(cfg: Config, number: int, expected: dict) -> dict:
    path = cfg.state_dir / "reviews" / f"pr-{number}.json"
    if not path.exists():
        return {"status": "missing"}
    try:
        record = _load(cfg, number)
    except (OSError, ValueError, WorkflowError):
        return {"status": "invalid"}
    prior = record.get("head")
    summary = {"status": "stale"}
    if isinstance(prior, str) and _SHA.fullmatch(prior):
        summary["head"] = prior
    if (record.get("decision") != "reviewed"
            or any(record.get(key) != value for key, value in expected.items())
            or not isinstance(record.get("ci"), dict)
            or record["ci"].get("result") != "passed"
            or record.get("merge_attempt") is not None):
        return summary
    try:
        _review_policy(cfg, record, check_reference=True)
    except WorkflowError:
        return summary
    return {"status": "current", "head": expected["head"]}


def build_review_packet(cfg: Config, pr_number: int) -> dict:
    """Collect live identity, publisher claims, and local exact-HEAD review state.

    No checkout, state mutation, comment publication, or local check is performed.
    A current packet means current publisher evidence, not approval to merge.
    """
    if type(pr_number) is not int or pr_number <= 0:
        return {"status": "incomplete", "reason": "invalid_pr_number"}
    try:
        pr, issue, spec, paths = _live(cfg, _github(cfg), pr_number)
        image = published_image_id(pr.get("body") or "")
        image_config_digest = published_image_config_digest(pr.get("body") or "")
        if not (_SHA.fullmatch(pr["baseRefOid"])
                and _SHA.fullmatch(pr["headRefOid"])):
            raise WorkflowError("invalid commit identity")
    except (WorkflowError, GitHubError, ValueError, KeyError, TypeError):
        return {"status": "stale", "reason": "remote_identity"}
    packet = {
        "status": "incomplete", "repo": cfg.repo, "issue": issue, "pr": pr_number,
        "branch": pr["headRefName"], "base": pr["baseRefOid"],
        "head": pr["headRefOid"], "previous_head": None,
        "diff_url": f"https://github.com/{cfg.repo}/compare/"
                    f"{pr['baseRefOid']}...{pr['headRefOid']}",
        "goal": _excerpt(spec.goal, issue, 240),
        "acceptance": _excerpt(spec.acceptance, issue, 400),
        "paths": paths,
    }
    expected = {
        "repo": cfg.repo, "issue": issue, "pr": pr_number,
        "head_repository": cfg.publish_repo or cfg.repo,
        "branch": pr["headRefName"], "base": pr["baseRefOid"],
        "head": pr["headRefOid"], "body_sha256": spec.body_sha256,
        "paths": paths, "checks_sha256": _checks_sha256(cfg),
        "published_image_id": image,
        "image_config_digest": image_config_digest,
    }
    packet["review"] = _review_summary(cfg, pr_number, expected)
    old_head = packet["review"].get("head")
    if old_head and old_head != packet["head"]:
        packet["previous_head"] = old_head
    try:
        fetched, record = fetch_manifest(cfg, pr_number)
    except (OSError, ValueError, WorkflowError, GitHubError):
        packet.update(status="incomplete", evidence={"status": "unavailable"})
        return packet
    status = fetched.get("status")
    if status not in {"current", "missing", "duplicate", "stale",
                      "unsupported_version"}:
        packet.update(status="incomplete", evidence={"status": "unavailable"})
        return packet
    if status == "current" and not isinstance(record, dict):
        packet.update(status="incomplete", evidence={"status": "unavailable"})
        return packet
    evidence = {"status": status}
    if status == "current" and record is not None:
        manifest_identity = {
            "version": 2,
            "repo": cfg.repo, "issue": issue, "pr": pr_number,
            "head_repository": cfg.publish_repo or cfg.repo,
            "branch": pr["headRefName"], "base": pr["baseRefOid"],
            "head": pr["headRefOid"], "body_sha256": spec.body_sha256,
            "paths": paths, "checks_sha256": _checks_sha256(cfg),
            "image": image,
            "image_config_digest": image_config_digest,
        }
        if any(record.get(key) != value for key, value in manifest_identity.items()):
            packet.update(status="stale", evidence={"status": "stale",
                                                    "reason": "identity_changed"})
            return packet
        evidence["head"] = record["head"]
        evidence["sha256"] = record["sha256"]
        evidence["checks"] = [{"index": row["index"], "result": row["result"]}
                              for row in record["checks"]]
        evidence["observation"] = "publisher_claim"
        # The preceding publisher record is history, never a current check claim.
        try:
            history = _published_records(cfg, GitHub(cfg.repo), pr_number)
        except (GitHubError, ValueError):
            packet.update(status="incomplete", evidence={"status": "unavailable"})
            return packet
        prior_heads = [saved["head"] for _, saved in history
                       if saved["head"] != packet["head"]]
        if prior_heads:
            packet["previous_head"] = prior_heads[-1]
    elif status == "stale":
        reason = fetched.get("reason")
        if reason in {"head_changed", "identity_changed", "remote_identity",
                      "image_unavailable", "image_changed", "image_identity_missing",
                      "invalid_record"}:
            evidence["reason"] = reason
        prior = fetched.get("head")
        if (packet["previous_head"] is None and isinstance(prior, str)
                and _SHA.fullmatch(prior) and prior != packet["head"]):
            packet["previous_head"] = prior
    packet["evidence"] = evidence
    packet["status"] = status
    if status == "current":
        try:
            final_pr, final_issue, final_spec, final_paths = _live(
                cfg, _github(cfg), pr_number)
            final_image = published_image_id(final_pr.get("body") or "")
            final_image_config_digest = published_image_config_digest(
                final_pr.get("body") or "")
            unchanged = (
                final_issue == issue
                and final_spec.body_sha256 == spec.body_sha256
                and final_pr["headRefName"] == pr["headRefName"]
                and final_pr["baseRefOid"] == pr["baseRefOid"]
                and final_pr["headRefOid"] == pr["headRefOid"]
                and final_paths == paths and final_image == image
                and final_image_config_digest == image_config_digest
            )
        except (WorkflowError, GitHubError, ValueError, KeyError, TypeError):
            unchanged = False
        if not unchanged:
            packet["status"] = "stale"
            packet["evidence"] = {"status": "stale", "reason": "identity_changed"}
    return packet
