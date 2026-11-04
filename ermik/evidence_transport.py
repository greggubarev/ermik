"""Bounded GitHub comment delivery for one exact-HEAD evidence manifest."""

from __future__ import annotations

import json

from .config import Config
from .evidence import MAX_BYTES, dumps_manifest, loads_manifest, make_manifest
from .evidence_export import export_manifest, verify_manifest
from .github import GitHub, GitHubError


MARKER = "Ermik evidence v1\n\n```json\n"
_SUFFIX = "```"
MAX_COMMENTS = 1000


class EvidenceRecordError(GitHubError):
    """A claimed publisher record cannot be trusted."""


def _publisher_id(cfg: Config) -> int:
    publisher_id = getattr(cfg, "evidence_publisher_id", None)
    if type(publisher_id) is not int or publisher_id <= 0:
        raise ValueError("evidence_publisher_id is required")
    return publisher_id


def _body(record: dict) -> str:
    raw = dumps_manifest(record)
    if len(raw) > MAX_BYTES + 1:
        raise ValueError("manifest too large")
    return MARKER + raw.decode("utf-8") + _SUFFIX


def _record(body: str) -> dict:
    if (not isinstance(body, str) or not body.startswith(MARKER)
            or not body.endswith(_SUFFIX)):
        raise ValueError("invalid evidence comment")
    raw = body[len(MARKER):-len(_SUFFIX)].encode("utf-8")
    if len(raw) > MAX_BYTES + 1:
        raise ValueError("manifest too large")
    record = loads_manifest(raw)
    if _body(record) != body:
        raise ValueError("noncanonical evidence comment")
    if record["model"] is not None or "model" not in record["unknowns"]:
        raise ValueError("evidence comment exposes execution model")
    return record


def _comments(gh: GitHub, pr_number: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_COMMENTS // 100 + 1):
        batch = gh.api(f"issues/{pr_number}/comments?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise GitHubError("evidence comment listing is invalid")
        if len(batch) > 100 or any(not isinstance(item, dict) for item in batch):
            raise GitHubError("evidence comment listing is invalid")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise GitHubError("evidence comment listing exceeds limit")


def _published_records(cfg: Config, gh: GitHub, pr_number: int) -> list[tuple[dict, dict]]:
    publisher_id = _publisher_id(cfg)
    found: list[tuple[dict, dict]] = []
    for row in _comments(gh, pr_number):
        body = row.get("body")
        if not isinstance(body, str) or not body.startswith(MARKER):
            continue
        user = row.get("user")
        if (not isinstance(user, dict) or type(user.get("id")) is not int
                or user["id"] != publisher_id):
            continue
        try:
            record = _record(body)
        except (ValueError, UnicodeError) as exc:
            raise EvidenceRecordError("publisher evidence comment is malformed") from exc
        if record["repo"].casefold() != cfg.repo.casefold() or record["pr"] != pr_number:
            raise EvidenceRecordError("publisher evidence comment identity mismatch")
        if type(row.get("id")) is not int or row["id"] <= 0:
            raise EvidenceRecordError("publisher evidence comment lacks identity")
        found.append((row, record))
    return found


def _sanitized_export(cfg: Config, pr_number: int) -> dict:
    exported = export_manifest(cfg, pr_number)
    fields = {key: value for key, value in exported.items()
              if key not in ("version", "sha256")}
    fields["model"] = None
    fields["unknowns"] = sorted(set(fields["unknowns"]) | {"model"})
    return make_manifest(**fields)


def publish_manifest(cfg: Config, pr_number: int) -> dict:
    """Post once, reconciling an uncertain POST against the remote comment list."""
    record = _sanitized_export(cfg, pr_number)
    gh = GitHub(cfg.repo)
    publisher_id = _publisher_id(cfg)
    actor = json.loads(gh.command(["api", "user"]))
    if (not isinstance(actor, dict) or type(actor.get("id")) is not int
            or actor["id"] != publisher_id):
        raise GitHubError("evidence publisher identity mismatch")

    def existing() -> dict | None:
        matches = [(row, saved) for row, saved in _published_records(cfg, gh, pr_number)
                   if saved["head"] == record["head"]]
        if len(matches) > 1:
            raise GitHubError("duplicate evidence for PR HEAD")
        if not matches:
            return None
        row, saved = matches[0]
        if saved != record:
            raise GitHubError("conflicting evidence for PR HEAD")
        return {"status": "existing", "comment_id": row["id"],
                "head": record["head"], "sha256": record["sha256"]}

    prior = existing()
    if prior is not None:
        return prior
    # Export checked the live identity; recheck immediately before publication.
    if verify_manifest(cfg, pr_number, record)["status"] != "current":
        raise GitHubError("evidence became stale before publication")
    try:
        response = gh.command(["api", "--method", "POST",
                               f"repos/{cfg.repo}/issues/{pr_number}/comments",
                               "-f", f"body={_body(record)}"])
        posted = json.loads(response)
        user = posted.get("user") if isinstance(posted, dict) else None
        if (not isinstance(posted, dict) or type(posted.get("id")) is not int
                or posted.get("body") != _body(record)
                or not isinstance(user, dict) or type(user.get("id")) is not int
                or user["id"] != publisher_id):
            raise GitHubError("evidence publication response is incomplete")
    except (GitHubError, ValueError, TypeError) as exc:
        recovered = existing()
        if recovered is not None:
            return recovered
        raise GitHubError("evidence publication result is uncertain") from exc
    return {"status": "published", "comment_id": posted["id"],
            "head": record["head"], "sha256": record["sha256"]}


def fetch_manifest(cfg: Config, pr_number: int) -> tuple[dict, dict | None]:
    """Fetch publisher records without opening controller SQLite or private logs."""
    gh = GitHub(cfg.repo)
    try:
        found = _published_records(cfg, gh, pr_number)
    except EvidenceRecordError:
        return {"status": "stale", "reason": "invalid_record"}, None
    if not found:
        return {"status": "missing"}, None
    pr = gh.pr(pr_number)
    head = pr.get("headRefOid")
    counts: dict[str, int] = {}
    for _, record in found:
        counts[record["head"]] = counts.get(record["head"], 0) + 1
    if any(count > 1 for count in counts.values()):
        return {"status": "duplicate", "head": head}, None
    current = [(row, record) for row, record in found if record["head"] == head]
    if current:
        row, record = current[0]
        result = verify_manifest(cfg, pr_number, record)
        if result["status"] == "current":
            return {"status": "current", "comment_id": row["id"],
                    "head": head, "sha256": record["sha256"]}, record
        return {"status": "stale", "reason": result.get("reason", "identity_changed"),
                "comment_id": row["id"], "head": record["head"]}, record
    row, record = found[-1]
    return {"status": "stale", "reason": "head_changed", "comment_id": row["id"],
            "head": record["head"], "current_head": head}, record
