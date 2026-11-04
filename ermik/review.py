"""Local maintainer review and explicit merge; no server-side automatic merge."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .config import Config
from .decision_trail import append_decision, check_decision, review_digest
from .evidence_store import read_evidence, write_evidence
from .github import GitHub, GitHubError
from .git_tree import verify_changed_tree
from .policy import parse_issue, validate_changed_paths
from .runtime import (WorkflowError, _approved_on_github, git, local_checks,
                      mirror, main_sha, _portable_image_digest,
                      _resolve_image_id, published_image_id,
                      published_image_config_digest)
from . import state


_CLOSES = re.compile(r"(?im)\bCloses\s+#(\d+)\b")
_BRANCH = re.compile(r"^work/issue-(\d+)-[a-z0-9-]+$")
_MAX_REVIEW_BYTES = 20000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _checks_sha256(cfg: Config) -> str:
    """Hash the configured commands as an ordered JSON array of argument arrays."""
    encoded = json.dumps(cfg.checks, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _review_policy(cfg: Config, record: dict, *, check_reference: bool) -> str:
    checks_hash = record.get("checks_sha256")
    image_id = record.get("image_id")
    image_config_digest = record.get("image_config_digest")
    published_image = record.get("published_image_id")
    if (not isinstance(checks_hash, str) or not _SHA256.fullmatch(checks_hash)
            or checks_hash != _checks_sha256(cfg)):
        raise WorkflowError("review check policy is missing or changed")
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise WorkflowError("review image ID is missing or malformed")
    if (not isinstance(image_config_digest, str)
            or not _IMAGE_ID.fullmatch(image_config_digest)
            or not isinstance(published_image, str)
            or not _IMAGE_ID.fullmatch(published_image)):
        raise WorkflowError("review portable image identity is missing or malformed")
    if check_reference:
        if (_resolve_image_id(cfg.image) != image_id
                or _portable_image_digest(cfg.image, image_id) != image_config_digest):
            raise WorkflowError("configured image changed since review")
    return image_id


def _identity(cfg: Config, gh: GitHub, number: int, *,
              allow_blocked: bool = False) -> tuple[dict, int, dict]:
    if gh.identity() != cfg.maintainer:
        raise WorkflowError("current GitHub account is not configured maintainer")
    pr = gh.pr(number)
    if pr["state"] != "OPEN":
        raise WorkflowError("PR is not open")
    if pr["baseRefName"] != cfg.base_branch:
        raise WorkflowError("PR targets another base branch")
    if pr["changedFiles"] != len(pr["files"]) or pr["changedFiles"] > 100:
        raise WorkflowError("complete PR file list unavailable")
    match = _BRANCH.fullmatch(pr["headRefName"])
    links = _CLOSES.findall(pr.get("body") or "")
    if not match or links != [match.group(1)]:
        raise WorkflowError("PR branch and one closing issue link required")
    gh.validate_pr_identity(pr, branch=pr["headRefName"],
                            base_branch=cfg.base_branch, number=number,
                            state="OPEN")
    issue_num = int(match.group(1))
    issue = gh.issue(issue_num)
    names = {row["name"] for row in issue.get("labels", [])}
    valid_states = {"state:in-progress", "state:blocked"} if allow_blocked else {"state:in-progress"}
    if "queue:worker" not in names or not names.intersection(valid_states):
        raise WorkflowError("issue queue or state changed")
    spec = parse_issue(issue.get("body") or "")
    if not _approved_on_github(gh, cfg, issue_num, spec):
        raise WorkflowError("issue lacks current maintainer admission")
    validate_changed_paths((row["path"] for row in pr["files"]), spec.allowed_paths)
    return pr, issue_num, issue


def _checkout_pr(cfg: Config, number: int, pr: dict) -> Path:
    source = mirror(cfg)
    path = cfg.workspace / "reviews" / f"pr-{number}"
    if path.exists():
        raise WorkflowError("existing review checkout requires inspection")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(source, "fetch", "--no-tags", "origin",
        f"+refs/pull/{number}/head:refs/remotes/origin/review-{number}")
    if git(source, "rev-parse", f"refs/remotes/origin/review-{number}") != pr["headRefOid"]:
        raise WorkflowError("PR head moved during review fetch")
    git(source, "worktree", "add", "--detach", str(path), pr["headRefOid"])
    return path


def _save(cfg: Config, number: int, value: dict) -> None:
    root = cfg.state_dir / "reviews"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"pr-{number}.json"
    try:
        write_evidence(path, value, mode=0o600, max_bytes=_MAX_REVIEW_BYTES)
    except ValueError as exc:
        if str(exc) == "evidence is too large":
            raise WorkflowError("review record unexpectedly large") from exc
        raise


def _load(cfg: Config, number: int) -> dict:
    path = cfg.state_dir / "reviews" / f"pr-{number}.json"
    before = path.lstat()
    if before.st_size > _MAX_REVIEW_BYTES:
        raise WorkflowError("review record unexpectedly large")
    record = read_evidence(path)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise WorkflowError("review record changed during read")
    if after.st_size > _MAX_REVIEW_BYTES:
        raise WorkflowError("review record unexpectedly large")
    return record


def _record_decision(cfg: Config, record: dict, decision: str) -> dict:
    """Append a local observation; the private review record remains the gate."""
    review_digest(cfg, record)
    return append_decision(cfg, record, decision)


def review(cfg: Config, number: int) -> dict:
    with state.exclusive(cfg.state_dir):
        return _review_locked(cfg, number)


def _review_locked(cfg: Config, number: int) -> dict:
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    record_path = cfg.state_dir / "reviews" / f"pr-{number}.json"
    prior = None
    if record_path.exists():
        prior = _load(cfg, number)
        if prior.get("merge_attempt") is not None:
            raise WorkflowError("review cannot replace a pending merge attempt")
        if "ci" in prior:
            _record_decision(cfg, prior, "reviewed")
    pr, issue_num, issue = _identity(cfg, gh, number)
    if prior is not None and prior.get("head") == pr["headRefOid"]:
        marker = f"SEMI_REWORK issue={issue_num} head={pr['headRefOid']}"
        if any(marker in (row.get("body") or "") and
               row.get("user", {}).get("login") == cfg.maintainer
               for row in gh.comments(number)):
            raise WorkflowError("PR has a pending exact-head correction request")
    if pr["baseRefOid"] != main_sha(gh, cfg):
        raise WorkflowError("PR base is not current base branch")
    spec = parse_issue(issue["body"])
    published_image = published_image_id(pr.get("body") or "")
    published_config = published_image_config_digest(pr.get("body") or "")
    image_id = _resolve_image_id(cfg.image)
    if _portable_image_digest(cfg.image, image_id) != published_config:
        raise WorkflowError("published execution image config differs from configured image")
    checkout = _checkout_pr(cfg, number, pr)
    try:
        if git(checkout, "rev-parse", "HEAD") != pr["headRefOid"]:
            raise WorkflowError("review checkout HEAD differs")
        actual = set(verify_changed_tree(checkout, pr["baseRefOid"],
                                         pr["headRefOid"],
                                         allowed_paths=spec.allowed_paths))
        published = {row["path"] for row in pr["files"]}
        if actual != published:
            raise WorkflowError("published and fetched PR diffs differ")
        validate_changed_paths(actual, spec.allowed_paths)
        proof = local_checks(cfg, checkout, image_id=image_id)
        actual_image = proof.get("image_id") if isinstance(proof, dict) else None
        if (actual_image != image_id
                or proof.get("image_config_digest") != published_config):
            raise WorkflowError("review checks did not use the published execution image")
    finally:
        if checkout.exists():
            git(cfg.workspace / "source", "worktree", "remove", str(checkout))
    current = gh.pr(number)
    if current["headRefOid"] != pr["headRefOid"] or current["baseRefOid"] != pr["baseRefOid"]:
        raise WorkflowError("PR changed during local review")
    if (published_image_id(current.get("body") or "") != published_image
            or published_image_config_digest(current.get("body") or "")
            != published_config):
        raise WorkflowError("published execution image changed during local review")
    _, _, current_issue = _identity(cfg, gh, number)
    if parse_issue(current_issue["body"]).body_sha256 != spec.body_sha256:
        raise WorkflowError("issue changed during local review")
    result = {"pr": number, "issue": issue_num, "repo": cfg.repo,
              "head_repository": cfg.publish_repo or cfg.repo,
              "branch": pr["headRefName"], "base": pr["baseRefOid"],
              "head": pr["headRefOid"], "body_sha256": spec.body_sha256,
              "paths": sorted(actual), "reviewed_at": datetime.now(timezone.utc).isoformat(),
              "ci": proof, "checks_sha256": _checks_sha256(cfg),
              "image_id": image_id, "published_image_id": published_image,
              "image_config_digest": published_config, "decision": "reviewed"}
    _save(cfg, number, result)
    _record_decision(cfg, result, "reviewed")
    return result


def request_changes(cfg: Config, number: int, note_file: Path) -> dict:
    with state.exclusive(cfg.state_dir):
        return _request_changes_locked(cfg, number, note_file)


def _request_changes_locked(cfg: Config, number: int, note_file: Path) -> dict:
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    pr, issue_num, issue = _identity(cfg, gh, number, allow_blocked=True)
    record = _load(cfg, number)
    if record.get("merge_attempt") is not None:
        raise WorkflowError("correction request cannot follow a pending merge attempt")
    if (record.get("head") != pr["headRefOid"] or record.get("base") != pr["baseRefOid"]
            or record.get("body_sha256") != parse_issue(issue["body"]).body_sha256
            or record.get("paths") != sorted(row["path"] for row in pr["files"])):
        raise WorkflowError("review record is stale")
    note = note_file.read_text(encoding="utf-8").strip()
    if not 10 <= len(note) <= 4000:
        raise WorkflowError("review note must be 10..4000 characters")
    _record_decision(cfg, record, "reviewed")
    check_decision(cfg, record, "changes_requested")
    marker = f"SEMI_REWORK issue={issue_num} head={record['head']}"
    body = marker + "\n\n" + note
    comments = gh.comments(number)
    existing = [row for row in comments if row.get("body") == body and
                row.get("user", {}).get("login") == cfg.maintainer]
    already_present = bool(existing)
    if not already_present:
        if any(marker in (row.get("body") or "") and
               row.get("user", {}).get("login") == cfg.maintainer
               for row in comments):
            _record_decision(cfg, record, "changes_requested")
            raise WorkflowError("exact-head correction request already exists")
        gh.comment(number, body)
        existing = [row for row in gh.comments(number) if row.get("body") == body and
                    row.get("user", {}).get("login") == cfg.maintainer]
        if not existing:
            raise WorkflowError("exact-head correction comment is not confirmed")
    _record_decision(cfg, record, "changes_requested")
    return {"pr": number, "issue": issue_num, "head": record["head"],
            "request": "already-present" if already_present else "posted"}


def _merged_identity(cfg: Config, gh: GitHub, number: int,
                     record: dict, pr: dict) -> str:
    """Bind a completed squash merge to the exact reviewed PR and base."""
    if (record.get("pr") != number
            or not isinstance(record.get("issue"), int)
            or record["issue"] <= 0
            or record.get("repo") != cfg.repo
            or record.get("head_repository") != (cfg.publish_repo or cfg.repo)
            or not isinstance(record.get("branch"), str)
            or not _BRANCH.fullmatch(record["branch"])
            or record.get("issue") != int(_BRANCH.fullmatch(record["branch"]).group(1))
            or _CLOSES.findall(pr.get("body") or "") != [str(record["issue"])]):
        raise WorkflowError("reviewed PR identity is missing or changed")
    gh.validate_pr_identity(pr, branch=record["branch"],
                            base_branch=cfg.base_branch, number=number,
                            head_sha=record["head"], state="MERGED")
    if pr.get("state") != "MERGED" or pr.get("headRefOid") != record["head"]:
        raise WorkflowError("merge outcome not confirmed")
    issue = gh.issue(record["issue"], allow_closed=True)
    if parse_issue(issue.get("body") or "").body_sha256 != record.get("body_sha256"):
        raise WorkflowError("reviewed issue changed after merge")
    merge_commit = pr.get("mergeCommit")
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}", oid):
        raise WorkflowError("merge commit is not available yet; retry merge verification")
    if record.get("merge_commit") is not None and record["merge_commit"] != oid:
        raise WorkflowError("merge commit changed after confirmation")
    parents = gh.api(f"git/commits/{oid}").get("parents")
    if (not isinstance(parents, list) or len(parents) != 1
            or not isinstance(parents[0], dict)
            or parents[0].get("sha") != record.get("base")):
        raise WorkflowError("merge commit is not based on reviewed base")
    return oid


def _incomplete(number: int, issue_num: int, reason: str,
                merge_commit: str | None = None) -> dict:
    result = {"pr": number, "issue": issue_num, "status": "incomplete",
              "reason": reason}
    if merge_commit is not None:
        result.update(merge_commit=merge_commit, main_ci=None, issue_closed=None)
    return result


def _post_merge_verification(cfg: Config, gh: GitHub, number: int,
                             record: dict, merge_commit: str) -> dict:
    try:
        image_id = _review_policy(cfg, record, check_reference=False)
    except Exception:
        raise WorkflowError("review policy unavailable after merge") from None
    try:
        new_main = main_sha(gh, cfg)
    except Exception:
        raise WorkflowError("base branch could not be verified") from None
    if merge_commit != new_main:
        raise WorkflowError("base branch moved after merge")
    try:
        source = mirror(cfg)
        path = cfg.workspace / "reviews" / f"main-after-{number}"
        if path.exists():
            raise WorkflowError("existing main CI checkout requires inspection")
        git(source, "worktree", "add", "--detach", str(path), new_main)
    except Exception:
        raise WorkflowError("post-merge checkout failed") from None
    try:
        try:
            proof = local_checks(cfg, path, image_id=image_id)
        except Exception:
            raise WorkflowError("post-merge checks failed") from None
    finally:
        if path.exists():
            try:
                git(source, "worktree", "remove", str(path))
            except Exception:
                raise WorkflowError("post-merge checkout cleanup failed") from None
    if (not isinstance(proof, dict) or proof.get("result") != "passed"
            or proof.get("image_id") != image_id
            or proof.get("image_config_digest") != record["image_config_digest"]):
        raise WorkflowError("post-merge checks failed")
    try:
        issue_closed = gh.api(f"issues/{record['issue']}")["state"] == "closed"
    except Exception:
        raise WorkflowError("linked issue closure could not be verified") from None
    if not issue_closed:
        raise WorkflowError("linked issue is not closed")
    return proof


def merge(cfg: Config, number: int) -> dict:
    with state.exclusive(cfg.state_dir):
        return _merge_locked(cfg, number)


def _merge_locked(cfg: Config, number: int) -> dict:
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    record = _load(cfg, number)
    if gh.identity() != cfg.maintainer:
        raise WorkflowError("current GitHub account is not configured maintainer")
    pr = gh.pr(number)
    issue_num = record["issue"]
    if (record.get("pr") != number
            or not isinstance(issue_num, int) or issue_num <= 0
            or record.get("repo") != cfg.repo
            or record.get("head_repository") != (cfg.publish_repo or cfg.repo)
            or not isinstance(record.get("branch"), str)):
        raise WorkflowError("reviewed PR identity is missing")
    if (published_image_id(pr.get("body") or "") != record.get("published_image_id")
            or published_image_config_digest(pr.get("body") or "")
            != record.get("image_config_digest")):
        raise WorkflowError("published execution image changed since review")
    if pr["state"] == "OPEN":
        if record.get("merge_attempt") is not None:
            if record["merge_attempt"] != {"head": record["head"],
                                           "base": record["base"],
                                           "branch": record["branch"]}:
                raise WorkflowError("merge attempt identity changed")
            gh.validate_pr_identity(pr, branch=record["branch"],
                                    base_branch=cfg.base_branch, number=number,
                                    head_sha=record["head"], base_sha=record["base"],
                                    state="OPEN")
            _record_decision(cfg, record, "reviewed")
            _record_decision(cfg, record, "merge_intent")
            result = _incomplete(number, issue_num, "merge outcome is unconfirmed")
            if record.get("merge_pending") != result:
                record["merge_pending"] = result
                _save(cfg, number, record)
            _record_decision(cfg, record, "merge_incomplete")
            return result
        pr, issue_num, issue = _identity(cfg, gh, number)
        if (record["branch"] != pr["headRefName"]
                or record["head"] != pr["headRefOid"] or record["base"] != pr["baseRefOid"]
                or record["body_sha256"] != parse_issue(issue["body"]).body_sha256
                or sorted(row["path"] for row in pr["files"]) != record["paths"]):
            raise WorkflowError("review record is stale")
        # An unresolved exact-head correction request overrides an older review.
        marker = f"SEMI_REWORK issue={issue_num} head={record['head']}"
        if any(marker in (comment.get("body") or "") and
               comment.get("user", {}).get("login") == cfg.maintainer
               for comment in gh.comments(number)):
            raise WorkflowError("PR has a pending exact-head correction request")
        _review_policy(cfg, record, check_reference=True)
        _record_decision(cfg, record, "reviewed")
        check_decision(cfg, record, "merge_intent")
        if pr["isDraft"]:
            gh.command(["pr", "ready", str(number), "-R", cfg.repo])
            pr = gh.pr(number)
            if (published_image_id(pr.get("body") or "") != record["published_image_id"]
                    or published_image_config_digest(pr.get("body") or "")
                    != record["image_config_digest"]):
                raise WorkflowError("published execution image changed while making PR ready")
            if pr["isDraft"]:
                raise WorkflowError("PR did not become ready")
        gh.validate_pr_identity(pr, branch=record["branch"],
                                base_branch=cfg.base_branch, number=number,
                                head_sha=record["head"], base_sha=record["base"],
                                state="OPEN")
        if main_sha(gh, cfg) != record["base"]:
            raise WorkflowError("base branch moved since review")
        current_pr, _, current_issue = _identity(cfg, gh, number)
        if parse_issue(current_issue["body"]).body_sha256 != record["body_sha256"]:
            raise WorkflowError("issue changed before merge")
        if (published_image_id(current_pr.get("body") or "")
                != record["published_image_id"]
                or published_image_config_digest(current_pr.get("body") or "")
                != record["image_config_digest"]):
            raise WorkflowError("published execution image changed before merge")
        _review_policy(cfg, record, check_reference=True)
        record["merge_attempt"] = {"head": record["head"],
                                   "base": record["base"],
                                   "branch": record["branch"]}
        _record_decision(cfg, record, "merge_intent")
        _save(cfg, number, record)
        try:
            gh.command(["pr", "merge", str(number), "-R", cfg.repo,
                    "--squash", "--delete-branch", "--match-head-commit",
                    record["head"]], timeout=180)
        except GitHubError:
            pass
    observed = gh.pr(number)
    if observed["state"] == "OPEN" and record.get("merge_attempt") is not None:
        gh.validate_pr_identity(observed, branch=record["branch"],
                                base_branch=cfg.base_branch, number=number,
                                head_sha=record["head"], base_sha=record["base"],
                                state="OPEN")
        result = _incomplete(number, issue_num, "merge outcome is unconfirmed")
        record["merge_pending"] = result
        _save(cfg, number, record)
        _record_decision(cfg, record, "merge_incomplete")
        return result
    if observed["state"] != "MERGED":
        raise WorkflowError("PR closed without a confirmed merge")
    merge_commit = _merged_identity(cfg, gh, number, record, observed)
    _record_decision(cfg, record, "reviewed")
    _record_decision(cfg, record, "merge_intent")
    if record.get("merge_commit") != merge_commit or "merge_pending" in record:
        record["merge_commit"] = merge_commit
        record.pop("merge_pending", None)
        _save(cfg, number, record)
    completed = record.get("merged")
    if (isinstance(completed, dict) and completed.get("merge_commit") == merge_commit
            and completed.get("status", "complete") == "complete"):
        _record_decision(cfg, record, "merged")
        return completed
    try:
        proof = _post_merge_verification(cfg, gh, number, record, merge_commit)
        result = {"pr": number, "issue": issue_num, "merge_commit": merge_commit,
                  "main_ci": proof, "issue_closed": True,
                  "status": "complete"}
    except WorkflowError as exc:
        result = _incomplete(number, issue_num, str(exc),
                             merge_commit)
    _save(cfg, number, {**record, "merged": result})
    _record_decision(cfg, record, "merged" if result["status"] == "complete"
                     else "merge_incomplete")
    return result
