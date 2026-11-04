"""Private, bounded history of maintainer decisions for exact local reviews."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .config import Config
from .evidence_store import read_evidence, write_evidence


MAX_ENTRIES = 128
MAX_BYTES = 65536
_DECISIONS = frozenset({"reviewed", "changes_requested", "merge_intent",
                        "merged", "merge_incomplete"})
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENTRY_KEYS = frozenset({"repo", "issue", "pr", "base", "head",
                         "evidence_sha256", "decision", "timestamp"})
_JOURNAL_KEYS = frozenset({"version", "repo", "pr", "entries"})


def _number(value: object) -> bool:
    return type(value) is int and value > 0


def _identity(cfg: Config, record: dict) -> tuple[int, int, str, str]:
    if type(record) is not dict or record.get("repo") != cfg.repo:
        raise ValueError("review repository is missing or changed")
    issue, pr = record.get("issue"), record.get("pr")
    base, head = record.get("base"), record.get("head")
    if not _number(issue) or not _number(pr):
        raise ValueError("review issue or PR is invalid")
    if (not isinstance(base, str) or not _SHA.fullmatch(base)
            or not isinstance(head, str) or not _SHA.fullmatch(head)):
        raise ValueError("review commit identity is invalid")
    return issue, pr, base, head


def review_digest(cfg: Config, record: dict) -> str:
    """Hash only validated local CI claims, excluding logs and local paths."""
    _, _, base, head = _identity(cfg, record)
    ci = record.get("ci")
    if type(ci) is not dict or ci.get("result") != "passed":
        raise ValueError("passing local CI proof is required")
    image = ci.get("image_id")
    image_config_digest = ci.get("image_config_digest")
    if (not isinstance(image, str) or not _IMAGE.fullmatch(image)
            or not isinstance(image_config_digest, str)
            or not _IMAGE.fullmatch(image_config_digest)
            or record.get("image_id") != image
            or ci.get("head") != head
            or ci.get("committed_diff_base") != base):
        raise ValueError("local CI identity is missing or changed")
    checks = ci.get("checks")
    if type(checks) is not list or len(checks) != len(cfg.checks):
        raise ValueError("local CI checks do not match configured commands")
    projection = []
    for result, configured in zip(checks, cfg.checks):
        if type(result) is not dict:
            raise ValueError("local CI check is malformed")
        command, log_hash = result.get("command"), result.get("log_sha256")
        if (type(command) is not list or command != list(configured)
                or type(result.get("exit_code")) is not int
                or result["exit_code"] != 0
                or not isinstance(log_hash, str) or not _HASH.fullmatch(log_hash)):
            raise ValueError("local CI check is missing or failed")
        projection.append({"command": command, "exit_code": 0,
                           "log_sha256": log_hash})
    proof = {"base": base, "head": head, "image_id": image,
             "image_config_digest": image_config_digest,
             "result": "passed", "checks": projection}
    encoded = json.dumps(proof, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(b"ermik-local-review-proof-v2\0" + encoded).hexdigest()


def _path(cfg: Config, pr: int) -> Path:
    if not _number(pr):
        raise ValueError("PR number must be a positive integer")
    return cfg.state_dir / "decision-trail" / f"pr-{pr}.json"


def _empty(cfg: Config, pr: int) -> dict:
    return {"version": 1, "repo": cfg.repo, "pr": pr, "entries": []}


def _validate_entry(cfg: Config, pr: int, entry: object) -> None:
    if type(entry) is not dict or set(entry) != _ENTRY_KEYS:
        raise ValueError("decision entry is malformed")
    if (entry["repo"] != cfg.repo or entry["pr"] != pr
            or not _number(entry["issue"])):
        raise ValueError("decision identity is invalid")
    if any(not isinstance(entry[key], str) or not _SHA.fullmatch(entry[key])
           for key in ("base", "head")):
        raise ValueError("decision commit identity is invalid")
    if (not isinstance(entry["evidence_sha256"], str)
            or not _HASH.fullmatch(entry["evidence_sha256"])
            or type(entry["decision"]) is not str
            or entry["decision"] not in _DECISIONS):
        raise ValueError("decision proof or value is invalid")
    stamp = entry["timestamp"]
    if not isinstance(stamp, str):
        raise ValueError("decision timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise ValueError("decision timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("decision timestamp must be UTC")


def _validate_journal(cfg: Config, pr: int, journal: dict) -> None:
    if (type(journal) is not dict or set(journal) != _JOURNAL_KEYS
            or type(journal["version"]) is not int or journal["version"] != 1
            or journal["repo"] != cfg.repo or journal["pr"] != pr
            or type(journal["entries"]) is not list
            or len(journal["entries"]) > MAX_ENTRIES):
        raise ValueError("decision journal is malformed or full")
    seen = set()
    decisions_by_proof: dict[tuple, set[str]] = {}
    for entry in journal["entries"]:
        _validate_entry(cfg, pr, entry)
        proof = (entry["issue"], entry["base"], entry["head"],
                 entry["evidence_sha256"])
        decision = entry["decision"]
        key = (*proof, decision)
        if key in seen:
            raise ValueError("duplicate decision entry")
        prior = decisions_by_proof.setdefault(proof, set())
        if decision != "reviewed" and "reviewed" not in prior:
            raise ValueError("decision requires prior review")
        if ((decision == "changes_requested" and prior.intersection(
                {"merge_intent", "merge_incomplete", "merged"}))
                or (decision in {"merge_intent", "merge_incomplete", "merged"}
                    and "changes_requested" in prior)):
            raise ValueError("conflicting decisions for one review")
        if decision in {"merge_incomplete", "merged"} and "merge_intent" not in prior:
            raise ValueError("merge outcome requires prior intent")
        prior.add(decision)
        seen.add(key)


def read_decisions(cfg: Config, pr: int) -> dict:
    """Read a validated per-PR journal, including during an atomic replacement."""
    path = _path(cfg, pr)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return _empty(cfg, pr)
    if before.st_size > MAX_BYTES:
        raise ValueError("decision journal is too large")
    journal = read_evidence(path)
    after = path.lstat()
    if ((before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)):
        raise ValueError("decision journal changed during read")
    if after.st_size > MAX_BYTES:
        raise ValueError("decision journal is too large")
    _validate_journal(cfg, pr, journal)
    return journal


def check_decision(cfg: Config, record: dict, decision: str) -> None:
    """Validate a prospective decision without changing the journal."""
    if type(decision) is not str or decision not in _DECISIONS:
        raise ValueError("unsupported decision")
    issue, pr, base, head = _identity(cfg, record)
    digest = review_digest(cfg, record)
    journal = read_decisions(cfg, pr)
    for prior in journal["entries"]:
        if (prior["issue"] == issue and prior["base"] == base
                and prior["head"] == head and prior["evidence_sha256"] == digest
                and prior["decision"] == decision):
            return
    if len(journal["entries"]) >= MAX_ENTRIES:
        raise ValueError("decision journal is full")
    prospective = {"repo": cfg.repo, "issue": issue, "pr": pr, "base": base,
                   "head": head, "evidence_sha256": digest,
                   "decision": decision,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
    _validate_journal(cfg, pr,
                      {**journal, "entries": [*journal["entries"], prospective]})


def append_decision(cfg: Config, record: dict, decision: str) -> dict:
    """Append under the caller's state lock, reconciling uncertain writes."""
    if type(decision) is not str or decision not in _DECISIONS:
        raise ValueError("unsupported decision")
    issue, pr, base, head = _identity(cfg, record)
    digest = review_digest(cfg, record)
    path = _path(cfg, pr)
    journal = read_decisions(cfg, pr)
    for prior in journal["entries"]:
        if (prior["issue"] == issue and prior["base"] == base
                and prior["head"] == head and prior["evidence_sha256"] == digest
                and prior["decision"] == decision):
            return prior
    if len(journal["entries"]) >= MAX_ENTRIES:
        raise ValueError("decision journal is full")
    entry = {"repo": cfg.repo, "issue": issue, "pr": pr, "base": base,
             "head": head, "evidence_sha256": digest, "decision": decision,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    updated = {**journal, "entries": [*journal["entries"], entry]}
    _validate_journal(cfg, pr, updated)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        write_evidence(path, updated, mode=0o600, max_bytes=MAX_BYTES)
    except (OSError, ValueError):
        try:
            confirmed = read_decisions(cfg, pr)
        except (OSError, ValueError):
            raise
        if confirmed == updated:
            return entry
        raise
    return entry
