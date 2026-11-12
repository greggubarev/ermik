"""Bounded GitHub comment delivery for one exact-HEAD evidence manifest."""

from __future__ import annotations

import json
import re

from .config import Config
from .evidence import (MAX_BYTES, UnsupportedManifestVersion, dumps_manifest,
                       loads_manifest, make_manifest)
from .evidence_export import export_manifest, verify_manifest
from .github import GitHub, GitHubError


MARKER = "Ermik evidence v2\n\n```json\n"
_MARKERS = {1: "Ermik evidence v1\n\n```json\n", 2: MARKER,
            3: "Ermik evidence v3\n\n```json\n"}
_SUFFIX = "```"
MAX_COMMENTS = 1000
LOOKUP_TIMEOUT = 10
_POINTER = re.compile(r"<!-- Ermik evidence comment head=([0-9a-f]{40}) "
                      r"id=([1-9][0-9]*) sha256=([0-9a-f]{64}) -->")
_POINTER_PREFIX = "<!-- Ermik evidence comment "


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


def _comments(gh: GitHub, pr_number: int, *, allow_limit: bool = False) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_COMMENTS // 100 + 1):
        batch = gh.api(f"issues/{pr_number}/comments?per_page=100&page={page}",
                       timeout=LOOKUP_TIMEOUT)
        if not isinstance(batch, list):
            raise GitHubError("evidence comment listing is invalid")
        if len(batch) > 100 or any(not isinstance(item, dict) for item in batch):
            raise GitHubError("evidence comment listing is invalid")
        rows.extend(batch)
        if len(batch) < 100:
            tail = gh.api(f"issues/{pr_number}/comments?per_page=100&page={page + 1}",
                          timeout=LOOKUP_TIMEOUT)
            if tail != []:
                raise GitHubError("evidence comment pagination is incomplete")
            return rows
    if allow_limit:
        return rows
    raise GitHubError("evidence comment listing exceeds limit")


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


def _pointer(body: str) -> tuple[str, int, str] | None:
    if not isinstance(body, str):
        raise EvidenceRecordError("PR body is unavailable")
    lines = [line for line in body.splitlines() if _POINTER_PREFIX in line]
    if not lines:
        return None
    if len(lines) != 1:
        raise EvidenceRecordError("duplicate evidence pointer")
    match = _POINTER.fullmatch(lines[0])
    if match is None:
        raise EvidenceRecordError("invalid evidence pointer")
    return match.group(1), int(match.group(2)), match.group(3)


def _pointed_record(cfg: Config, gh: GitHub, pr_number: int,
                    pr: dict, *, row: dict | None = None) -> tuple[dict, dict] | None:
    pointer = _pointer(pr.get("body"))
    if pointer is None:
        return None
    head, comment_id, digest = pointer
    if head != pr.get("headRefOid"):
        raise EvidenceRecordError("evidence pointer is stale")
    if row is None:
        row = gh.api(f"issues/comments/{comment_id}", timeout=LOOKUP_TIMEOUT)
    if (not isinstance(row, dict) or type(row.get("id")) is not int
            or row["id"] != comment_id
            or row.get("issue_url") !=
            f"https://api.github.com/repos/{cfg.repo}/issues/{pr_number}"):
        raise EvidenceRecordError("evidence pointer has wrong PR identity")
    user = row.get("user")
    if (not isinstance(user, dict) or type(user.get("id")) is not int
            or user["id"] != _publisher_id(cfg)):
        raise EvidenceRecordError("evidence pointer has wrong author")
    try:
        record = _record(row.get("body"))
    except (ValueError, UnicodeError, UnsupportedManifestVersion) as exc:
        raise EvidenceRecordError("evidence pointer has invalid record") from exc
    if (record["repo"].casefold() != cfg.repo.casefold()
            or record["pr"] != pr_number or record["head"] != head
            or record["sha256"] != digest):
        raise EvidenceRecordError("evidence pointer has conflicting record")
    return row, record


def _publish_pointer(cfg: Config, gh: GitHub, pr_number: int,
                     record: dict, comment_id: int) -> None:
    pr = gh.pr(pr_number)
    if pr.get("headRefOid") != record["head"]:
        raise EvidenceRecordError("evidence became stale before pointer publication")
    body = pr.get("body")
    prior = _pointer(body)
    if prior is not None:
        if prior == (record["head"], comment_id, record["sha256"]):
            return
        if prior[0] == record["head"]:
            raise EvidenceRecordError("conflicting evidence pointer for PR HEAD")
        body = "\n".join(line for line in body.splitlines()
                         if _POINTER_PREFIX not in line)
    pointer = (f"<!-- Ermik evidence comment head={record['head']} "
               f"id={comment_id} sha256={record['sha256']} -->")
    updated = body.rstrip() + "\n\n" + pointer
    try:
        response = json.loads(gh.command(["api", "--method", "PATCH",
                                          f"repos/{cfg.repo}/pulls/{pr_number}",
                                          "-f", f"body={updated}"]))
        if (not isinstance(response, dict) or response.get("body") != updated
                or (response.get("head") or {}).get("sha") != record["head"]):
            raise GitHubError("evidence pointer response is incomplete")
    except (GitHubError, ValueError, TypeError) as exc:
        observed = gh.pr(pr_number)
        if observed.get("headRefOid") != record["head"]:
            raise EvidenceRecordError("evidence became stale before pointer publication") from exc
        if (observed.get("headRefOid") == record["head"]
                and _pointer(observed.get("body")) ==
                (record["head"], comment_id, record["sha256"])):
            return
        raise GitHubError("evidence pointer publication is uncertain") from exc


def _ensure_pointer(cfg: Config, gh: GitHub, pr_number: int,
                    record: dict, comment_id: int) -> str:
    try:
        _publish_pointer(cfg, gh, pr_number, record, comment_id)
    except EvidenceRecordError:
        raise
    except GitHubError:
        # A publisher with Issues write but no Pull requests write retains the
        # original comment transport. The receiver must not infer a pointer.
        return "unavailable"
    return "confirmed"


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

    def existing() -> dict | None:
        pr = gh.pr(pr_number)
        rows = _comments(gh, pr_number, allow_limit=True)
        found = _published_records(cfg, gh, pr_number, rows=rows)
        if len(rows) == MAX_COMMENTS:
            pointed = _pointed_record(cfg, gh, pr_number, pr)
            if pointed is None:
                raise GitHubError("evidence comment listing exceeds limit")
            if not any(row["id"] == pointed[0]["id"] for row, _ in found):
                found.append(pointed)
        matches = [(row, saved) for row, saved in found
                   if saved["head"] == record["head"]]
        if len(matches) > 1:
            raise GitHubError("duplicate evidence for PR HEAD")
        if not matches:
            return None
        row, saved = matches[0]
        if saved != record:
            raise GitHubError("conflicting evidence for PR HEAD")
        pointer_status = _ensure_pointer(cfg, gh, pr_number, record, row["id"])
        return {"status": "existing", "comment_id": row["id"],
                "head": record["head"], "sha256": record["sha256"],
                "pointer_status": pointer_status}

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
    pointer_status = _ensure_pointer(cfg, gh, pr_number, record, posted["id"])
    return {"status": "published", "comment_id": posted["id"],
            "head": record["head"], "sha256": record["sha256"],
            "pointer_status": pointer_status}


def fetch_manifest(cfg: Config, pr_number: int, *, _live_result=None,
                   _records_out: list | None = None) -> tuple[dict, dict | None]:
    """Fetch publisher records without opening controller SQLite or private logs.

    A packet caller may reuse its first live read and the fetched comment list;
    it must recheck remote identity before presenting a current result.
    """
    gh = GitHub(cfg.repo)
    pr = _live_result[0] if _live_result is not None else gh.pr(pr_number)
    try:
        pointer = _pointer(pr.get("body"))
        if pointer is not None and pointer[0] != pr.get("headRefOid"):
            return {"status": "stale", "reason": "head_changed",
                    "head": pointer[0], "current_head": pr.get("headRefOid")}, None
        rows = _comments(gh, pr_number, allow_limit=True)
        found = _published_records(cfg, gh, pr_number, rows=rows)
        if len(rows) == MAX_COMMENTS:
            pointed = _pointed_record(cfg, gh, pr_number, pr)
            if pointed is None:
                raise GitHubError("evidence comment listing exceeds limit")
            if not any(row["id"] == pointed[0]["id"] for row, _ in found):
                found.append(pointed)
        else:
            if pointer is not None:
                pointed_row = next((row for row in rows if row.get("id") == pointer[1]),
                                   None)
                if pointed_row is None:
                    raise EvidenceRecordError("evidence pointer is outside complete listing")
                _pointed_record(cfg, gh, pr_number, pr, row=pointed_row)
    except UnsupportedEvidenceVersion:
        return {"status": "unsupported_version"}, None
    except EvidenceRecordError:
        return {"status": "stale", "reason": "invalid_record"}, None
    if _records_out is not None:
        _records_out.extend(found)
    if not found:
        return {"status": "missing"}, None
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
