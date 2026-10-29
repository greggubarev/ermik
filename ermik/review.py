"""Local maintainer review and explicit merge; no server-side automatic merge."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .config import Config
from .github import GitHub, GitHubError
from .policy import parse_issue, validate_changed_paths
from .runtime import WorkflowError, git, local_checks, mirror, main_sha
from . import state


_CLOSES = re.compile(r"(?im)\bCloses\s+#(\d+)\b")
_BRANCH = re.compile(r"^work/issue-(\d+)-[a-z0-9-]+$")


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
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if path.stat().st_size > 20000:
        raise WorkflowError("review record unexpectedly large")


def _load(cfg: Config, number: int) -> dict:
    path = cfg.state_dir / "reviews" / f"pr-{number}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def review(cfg: Config, number: int) -> dict:
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    pr, issue_num, issue = _identity(cfg, gh, number)
    if pr["baseRefOid"] != main_sha(gh, cfg):
        raise WorkflowError("PR base is not current base branch")
    spec = parse_issue(issue["body"])
    with state.exclusive(cfg.state_dir):
        checkout = _checkout_pr(cfg, number, pr)
        try:
            if git(checkout, "rev-parse", "HEAD") != pr["headRefOid"]:
                raise WorkflowError("review checkout HEAD differs")
            actual = set(git(checkout, "diff", "--name-only", pr["baseRefOid"] + "...HEAD").splitlines())
            published = {row["path"] for row in pr["files"]}
            if actual != published:
                raise WorkflowError("published and fetched PR diffs differ")
            validate_changed_paths(actual, spec.allowed_paths)
            proof = local_checks(cfg, checkout)
        finally:
            if checkout.exists():
                git(cfg.workspace / "source", "worktree", "remove", str(checkout))
    current = gh.pr(number)
    if current["headRefOid"] != pr["headRefOid"] or current["baseRefOid"] != pr["baseRefOid"]:
        raise WorkflowError("PR changed during local review")
    result = {"pr": number, "issue": issue_num, "base": pr["baseRefOid"],
              "head": pr["headRefOid"], "body_sha256": spec.body_sha256,
              "paths": sorted(actual), "reviewed_at": datetime.now(timezone.utc).isoformat(),
              "ci": proof, "decision": "reviewed"}
    _save(cfg, number, result)
    return result


def request_changes(cfg: Config, number: int, note_file: Path) -> dict:
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    pr, issue_num, _issue = _identity(cfg, gh, number, allow_blocked=True)
    record = _load(cfg, number)
    if record["head"] != pr["headRefOid"] or record["base"] != pr["baseRefOid"]:
        raise WorkflowError("review record is stale")
    note = note_file.read_text(encoding="utf-8").strip()
    if not 10 <= len(note) <= 4000:
        raise WorkflowError("review note must be 10..4000 characters")
    marker = f"SEMI_REWORK issue={issue_num} head={record['head']}"
    body = marker + "\n\n" + note
    existing = [row for row in gh.comments(number) if row.get("body") == body and
                row.get("user", {}).get("login") == cfg.maintainer]
    if not existing:
        gh.comment(number, body)
    return {"pr": number, "issue": issue_num, "head": record["head"],
            "request": "already-present" if existing else "posted"}


def merge(cfg: Config, number: int) -> dict:
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    record = _load(cfg, number)
    if gh.identity() != cfg.maintainer:
        raise WorkflowError("current GitHub account is not configured maintainer")
    pr = gh.pr(number)
    issue_num = record["issue"]
    if pr["state"] == "OPEN":
        pr, issue_num, issue = _identity(cfg, gh, number)
        if (record["head"] != pr["headRefOid"] or record["base"] != pr["baseRefOid"]
                or record["body_sha256"] != parse_issue(issue["body"]).body_sha256
                or sorted(row["path"] for row in pr["files"]) != record["paths"]):
            raise WorkflowError("review record is stale")
        # An unresolved exact-head correction request overrides an older review.
        marker = f"SEMI_REWORK issue={issue_num} head={record['head']}"
        if any(marker in (comment.get("body") or "") and
               comment.get("user", {}).get("login") == cfg.maintainer
               for comment in gh.comments(number)):
            raise WorkflowError("PR has a pending exact-head correction request")
        if pr["isDraft"]:
            gh.command(["pr", "ready", str(number), "-R", cfg.repo])
            pr = gh.pr(number)
            if pr["isDraft"]:
                raise WorkflowError("PR did not become ready")
        if pr["headRefOid"] != record["head"] or pr["baseRefOid"] != record["base"]:
            raise WorkflowError("PR changed before merge")
        if main_sha(gh, cfg) != record["base"]:
            raise WorkflowError("base branch moved since review")
        try:
            gh.command(["pr", "merge", str(number), "-R", cfg.repo,
                    "--squash", "--delete-branch", "--match-head-commit",
                    record["head"]], timeout=180)
        except GitHubError:
            observed = gh.pr(number)
            if observed["state"] != "MERGED" or observed["headRefOid"] != record["head"]:
                raise
    observed = gh.pr(number)
    gh.validate_pr_identity(observed, branch=pr["headRefName"],
                            base_branch=cfg.base_branch, number=number,
                            head_sha=record["head"], state="MERGED")
    if observed["state"] != "MERGED" or observed["headRefOid"] != record["head"]:
        raise WorkflowError("merge outcome not confirmed")
    merge_commit = observed.get("mergeCommit")
    if not isinstance(merge_commit, dict) or not merge_commit.get("oid"):
        raise WorkflowError("merge commit is not available yet; retry merge verification")
    if "merged" in record and record["merged"].get("merge_commit") == merge_commit["oid"]:
        return record["merged"]
    new_main = main_sha(gh, cfg)
    if merge_commit["oid"] != new_main:
        raise WorkflowError("base branch moved after merge; inspect before checking")
    source = mirror(cfg)
    path = cfg.workspace / "reviews" / f"main-after-{number}"
    if path.exists():
        raise WorkflowError("existing main CI checkout requires inspection")
    git(source, "worktree", "add", "--detach", str(path), new_main)
    try:
        proof = local_checks(cfg, path)
    finally:
        if path.exists():
            git(source, "worktree", "remove", str(path))
    result = {"pr": number, "issue": issue_num, "merge_commit": new_main,
              "main_ci": proof, "issue_closed": gh.api(f"issues/{issue_num}")["state"] == "closed"}
    _save(cfg, number, {**record, "merged": result})
    return result
