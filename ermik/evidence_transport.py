"""Bounded GitHub comment delivery for one exact-HEAD evidence manifest."""

from __future__ import annotations

import json
import re
import time

from .config import Config
from .evidence import (MAX_BYTES, UnsupportedManifestVersion, dumps_manifest,
                       loads_manifest, make_manifest)
from .evidence_export import export_manifest, verify_manifest
from .github import GitHub, GitHubError


MARKER = "Ermik evidence v2\n\n```json\n"
_MARKERS = {1: "Ermik evidence v1\n\n```json\n", 2: MARKER,
            3: "Ermik evidence v3\n\n```json\n"}
_SUFFIX = "```"
MAX_COMMENTS = 2000
LOOKUP_TIMEOUT = 10
LOOKUP_BUDGET = 60
_clock = time.monotonic


class EvidenceRecordError(GitHubError):
    """A claimed publisher record cannot be trusted."""


class UnsupportedEvidenceVersion(EvidenceRecordError):
    """The pinned publisher supplied an evidence version this reader cannot use."""


def _publisher_id(cfg: Config) -> int:
    publisher_id = getattr(cfg, "evidence_publisher_id", None)
    if type(publisher_id) is not int or publisher_id <= 0:
        raise ValueError("evidence_publisher_id is required")
    return publisher_id


def _body(record: dict) -> str:
    raw = dumps_manifest(record)
    if len(raw) > MAX_BYTES + 1:
        raise ValueError("manifest too large")
    return _MARKERS[record["version"]] + raw.decode("utf-8") + _SUFFIX


def _record(body: str) -> dict:
    if not isinstance(body, str) or not body.endswith(_SUFFIX):
        raise ValueError("invalid evidence comment")
    marker = next((value for value in _MARKERS.values()
                   if body.startswith(value)), None)
    if marker is None:
        raise ValueError("invalid evidence comment")
    raw = body[len(marker):-len(_SUFFIX)].encode("utf-8")
    if len(raw) > MAX_BYTES + 1:
        raise ValueError("manifest too large")
    record = loads_manifest(raw)
    if _body(record) != body:
        raise ValueError("noncanonical evidence comment")
    if record["model"] is not None or "model" not in record["unknowns"]:
        raise ValueError("evidence comment exposes execution model")
    return record


def _comments(gh: GitHub, pr_number: int) -> list[dict]:
    deadline = _clock() + LOOKUP_BUDGET

    def read(endpoint: str):
        remaining = deadline - _clock()
        if remaining <= 2:
            raise GitHubError("evidence comment lookup timed out")
        # GitHub.command can make three read attempts with two short backoffs.
        value = gh.api(endpoint, timeout=min(LOOKUP_TIMEOUT, (remaining - 2) / 3))
        if _clock() >= deadline:
            raise GitHubError("evidence comment lookup timed out")
        return value

    before = read(f"issues/{pr_number}")
    count = before.get("comments") if isinstance(before, dict) else None
    updated = before.get("updated_at") if isinstance(before, dict) else None
    if (type(count) is not int or count < 0 or not isinstance(updated, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated)):
        raise GitHubError("evidence comment count is invalid")
    if count > MAX_COMMENTS:
        raise GitHubError("evidence comment listing exceeds limit")
    rows: list[dict] = []
    for page in range(1, (count + 99) // 100 + 1):
        batch = read(f"issues/{pr_number}/comments?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise GitHubError("evidence comment listing is invalid")
        expected = min(100, count - len(rows))
        if (len(batch) != expected
                or any(not isinstance(item, dict)
                       or type(item.get("id")) is not int or item["id"] <= 0
                       for item in batch)):
            raise GitHubError("evidence comment listing is invalid")
        if rows and batch[0]["id"] <= rows[-1]["id"]:
            raise GitHubError("evidence comment pagination is incomplete")
        if any(left["id"] >= right["id"] for left, right in zip(batch, batch[1:])):
            raise GitHubError("evidence comment pagination is incomplete")
        rows.extend(batch)
    after = read(f"issues/{pr_number}")
    if (not isinstance(after, dict) or type(after.get("comments")) is not int
            or after["comments"] != count or after.get("updated_at") != updated):
        raise GitHubError("evidence comment pagination changed")
    return rows


def _published_records(cfg: Config, gh: GitHub, pr_number: int,
                       *, rows: list[dict] | None = None) -> list[tuple[dict, dict]]:
    publisher_id = _publisher_id(cfg)
    found: list[tuple[dict, dict]] = []
    for row in rows if rows is not None else _comments(gh, pr_number):
        body = row.get("body")
        if not isinstance(body, str):
            continue
        user = row.get("user")
        if (not isinstance(user, dict) or type(user.get("id")) is not int
                or user["id"] != publisher_id):
            continue
        marker = re.match(r"Ermik evidence v([0-9]+)(?:\n|\Z)", body)
        if body.startswith("Ermik evidence v") and marker is None:
            raise EvidenceRecordError("publisher evidence comment is malformed")
        if marker:
            try:
                bounded = len(body.encode("utf-8")) <= MAX_BYTES + 1 + len(MARKER) + len(_SUFFIX)
            except UnicodeError:
                bounded = False
            if not bounded:
                raise EvidenceRecordError("publisher evidence comment is malformed")
            if marker.group(1) not in ("1", "2", "3"):
                raise UnsupportedEvidenceVersion("publisher evidence version is unsupported")
        if not any(body.startswith(value) for value in _MARKERS.values()):
            if marker:
                raise EvidenceRecordError("publisher evidence comment is malformed")
            continue
        try:
            record = _record(body)
        except UnsupportedManifestVersion as exc:
            raise UnsupportedEvidenceVersion("publisher evidence version is unsupported") from exc
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
    if "effort" in fields:
        fields["effort"] = None
        fields["unknowns"] = sorted(set(fields["unknowns"]) | {"effort"})
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

    scanned_count = 0

    def existing() -> dict | None:
        nonlocal scanned_count
        rows = _comments(gh, pr_number)
        scanned_count = len(rows)
        found = _published_records(cfg, gh, pr_number, rows=rows)
        matches = [(row, saved) for row, saved in found
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
    if scanned_count == MAX_COMMENTS:
        raise GitHubError("evidence comment listing is full")
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


def fetch_manifest(cfg: Config, pr_number: int, *, _live_result=None,
                   _records_out: list | None = None) -> tuple[dict, dict | None]:
    """Fetch publisher records without opening controller SQLite or private logs.

    A packet caller may reuse its first live read and the fetched comment list;
    it must recheck remote identity before presenting a current result.
    """
    gh = GitHub(cfg.repo)
    try:
        found = _published_records(cfg, gh, pr_number)
    except UnsupportedEvidenceVersion:
        return {"status": "unsupported_version"}, None
    except EvidenceRecordError:
        return {"status": "stale", "reason": "invalid_record"}, None
    if _records_out is not None:
        _records_out.extend(found)
    if not found:
        return {"status": "missing"}, None
    pr = _live_result[0] if _live_result is not None else gh.pr(pr_number)
    head = pr.get("headRefOid")
    counts: dict[str, int] = {}
    for _, record in found:
        counts[record["head"]] = counts.get(record["head"], 0) + 1
    if any(count > 1 for count in counts.values()):
        return {"status": "duplicate", "head": head}, None
    current = [(row, record) for row, record in found if record["head"] == head]
    if current:
        row, record = current[0]
        result = (verify_manifest(cfg, pr_number, record,
                                  _live_result=_live_result)
                  if _live_result is not None else
                  verify_manifest(cfg, pr_number, record))
        if result["status"] == "current":
            return {"status": "current", "comment_id": row["id"],
                    "head": head, "sha256": record["sha256"]}, record
        return {"status": "stale", "reason": result.get("reason", "identity_changed"),
                "comment_id": row["id"], "head": record["head"]}, record
    row, record = found[-1]
    return {"status": "stale", "reason": "head_changed", "comment_id": row["id"],
            "head": record["head"], "current_head": head}, record
