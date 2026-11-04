"""Conservative inventory and explicit retention of controller artifacts."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time

from . import state
from .github import GitHub
from .evidence_store import read_evidence
from .resources import (_registered_worktrees, _verified_published_checkout)


_ISSUE = re.compile(r"issue-([1-9][0-9]*)\Z")
_NUMBER = re.compile(r"[1-9][0-9]*\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_LOG = re.compile(r"check-[1-9][0-9]*\.log\Z")
_EVIDENCE = re.compile(r"([0-9a-f]{40})\.json\Z")
_MAX_SCAN = 100_000


def _epoch(now: datetime | int | float | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("now must have a timezone")
        return now.timestamp()
    if type(now) not in (int, float) or not 0 <= now < 10**12:
        raise ValueError("invalid current time")
    return float(now)


def _safe(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("unsafe artifact type")
    if path.resolve(strict=True) != path.absolute():
        raise ValueError("artifact path escapes configured root")
    if not directory and info.st_nlink != 1:
        raise ValueError("hard-linked artifact")
    if os.name == "posix":
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("unsafe artifact ownership or permissions")
    return info


def _root(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    _safe(path, directory=True)
    return True


def _tree(path: Path, budget: list[int]) -> tuple[int, float]:
    """Measure without following links; refuse unsafe or oversized trees."""
    info = _safe(path, directory=True)
    size, newest = 0, info.st_mtime
    stack = [path]
    while stack:
        directory = stack.pop()
        _safe(directory, directory=True)
        for child in directory.iterdir():
            budget[0] -= 1
            if budget[0] < 0:
                raise ValueError("artifact inventory exceeds scan limit")
            mode = child.lstat().st_mode
            if stat.S_ISDIR(mode):
                child_info = _safe(child, directory=True)
                stack.append(child)
            else:
                child_info = _safe(child, directory=False)
                size += child_info.st_size
            newest = max(newest, child_info.st_mtime)
    return size, newest


def _items(cfg, now: float) -> list[dict]:
    items: list[dict] = []
    budget = [_MAX_SCAN]
    workspace = Path(cfg.workspace)
    state_dir = Path(cfg.state_dir)
    for root in (workspace, state_dir):
        _root(root)
    jobs = workspace / "jobs"
    if _root(jobs):
        for path in sorted(jobs.iterdir()):
            if path.is_symlink():
                raise ValueError("symlink in artifact root")
            match = _ISSUE.fullmatch(path.name)
            if match is None:
                continue
            budget[0] -= 1
            if budget[0] < 0:
                raise ValueError("artifact inventory exceeds scan limit")
            size, newest = _tree(path, budget)
            items.append({"kind": "worktrees", "issue": int(match.group(1)),
                          "head": None, "bytes": size, "mtime": newest, "path": path})
    logs = state_dir / "ci-logs"
    if _root(logs):
        for directory in sorted(logs.iterdir()):
            if directory.is_symlink():
                raise ValueError("symlink in artifact root")
            if _HEAD.fullmatch(directory.name) is None:
                continue
            _safe(directory, directory=True)
            for path in sorted(directory.iterdir()):
                if path.is_symlink():
                    raise ValueError("symlink in artifact root")
                if _LOG.fullmatch(path.name) is None:
                    continue
                budget[0] -= 1
                if budget[0] < 0:
                    raise ValueError("artifact inventory exceeds scan limit")
                info = _safe(path, directory=False)
                items.append({"kind": "logs", "issue": None,
                              "head": directory.name, "bytes": info.st_size,
                              "mtime": info.st_mtime, "path": path})
    evidence = state_dir / "evidence"
    if _root(evidence):
        for directory in sorted(evidence.iterdir()):
            if directory.is_symlink():
                raise ValueError("symlink in artifact root")
            if _NUMBER.fullmatch(directory.name) is None:
                continue
            _safe(directory, directory=True)
            for path in sorted(directory.iterdir()):
                if path.is_symlink():
                    raise ValueError("symlink in artifact root")
                match = _EVIDENCE.fullmatch(path.name)
                if match is None:
                    continue
                budget[0] -= 1
                if budget[0] < 0:
                    raise ValueError("artifact inventory exceeds scan limit")
                info = _safe(path, directory=False)
                items.append({"kind": "evidence", "issue": int(directory.name),
                              "head": match.group(1), "bytes": info.st_size,
                              "mtime": info.st_mtime, "path": path})
    return items


def inventory(cfg, *, now: datetime | int | float | None = None) -> dict:
    """Return local byte and age totals without reading artifact content."""
    current = _epoch(now)
    categories = {name: {"bytes": 0, "items": 0, "oldest_age_seconds": 0}
                  for name in ("worktrees", "logs", "evidence")}
    for item in _items(cfg, current):
        group = categories[item["kind"]]
        group["bytes"] += item["bytes"]
        group["items"] += 1
        group["oldest_age_seconds"] = max(group["oldest_age_seconds"],
                                          max(0, int(current - item["mtime"])))
    total = sum(group["bytes"] for group in categories.values())
    warning_mb = cfg.artifact_warn_mb
    if type(warning_mb) is not int or warning_mb < 0:
        raise ValueError("invalid artifact warning limit")
    return {"categories": categories, "total_bytes": total,
            "warning": bool(warning_mb and total >= warning_mb * 1024**2),
            "truncated": False}


def _remote_merged(cfg, gh: GitHub, row, cutoff: float) -> bool:
    issue, branch, head, pr = row["issue"], row["branch"], row["head_sha"], row["pr"]
    if (row["status"] != "merged" or type(issue) is not int or issue <= 0
            or type(pr) is not int or pr <= 0 or not isinstance(head, str)
            or _HEAD.fullmatch(head) is None or not isinstance(branch, str)
            or not branch.startswith(f"work/issue-{issue}-")):
        return False
    try:
        remote = gh.pr(pr)
        gh.validate_pr_identity(remote, branch=branch, base_branch=cfg.base_branch,
                                number=pr, head_sha=head, state="MERGED")
        if remote.get("state") != "MERGED" or remote.get("headRefOid") != head:
            return False
        if (remote.get("headRepository") or {}).get("nameWithOwner", "").casefold() != (
                cfg.publish_repo or cfg.repo).casefold():
            return False
        if re.findall(r"(?im)\bcloses\s+#([1-9][0-9]*)\b", remote.get("body") or "") != [str(issue)]:
            return False
        merged_at = datetime.fromisoformat(remote["mergedAt"].replace("Z", "+00:00"))
        if merged_at.tzinfo is None or merged_at.timestamp() > cutoff:
            return False
        linked = gh.issue(issue, allow_closed=True)
        return linked.get("state") == "closed" and linked.get("number", issue) == issue
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def _remote_pr_heads(gh: GitHub, pr: int) -> set[str] | None:
    """Read a bounded, complete commit list for one PR; uncertainty blocks cleanup."""
    heads: set[str] = set()
    try:
        for page in range(1, 11):
            commits = gh.api(f"pulls/{pr}/commits?per_page=100&page={page}")
            if type(commits) is not list or len(commits) > 100:
                return None
            if any(type(commit) is not dict or type(commit.get("sha")) is not str
                   or _HEAD.fullmatch(commit["sha"]) is None for commit in commits):
                return None
            heads.update(commit["sha"] for commit in commits)
            if len(commits) < 100:
                return heads
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None
    return None


def _prune_empty(parent: Path, root: Path) -> None:
    if parent != root:
        try:
            parent.rmdir()
        except OSError:
            pass


def _valid_evidence(path: Path, row, *, head: str | None = None) -> bool:
    try:
        proof = read_evidence(path)
    except (OSError, ValueError):
        return False
    return (proof.get("issue") == row["issue"]
            and proof.get("pr") == row["pr"]
            and proof.get("branch") == row["branch"]
            and proof.get("head") == (head or row["head_sha"])
            and proof.get("base") == row["base_sha"])


def _review_allows_cleanup(cfg, row) -> bool:
    """An existing maintainer record must not have unfinished merge work."""
    path = Path(cfg.state_dir) / "reviews" / f"pr-{row['pr']}.json"
    if not path.exists() and not path.is_symlink():
        return True  # A separate controller installation has no review record.
    try:
        record = read_evidence(path)
    except (OSError, ValueError):
        return False
    if (record.get("pr") != row["pr"] or record.get("issue") != row["issue"]
            or record.get("head") != row["head_sha"]
            or record.get("base") != row["base_sha"]
            or record.get("branch") != row["branch"]
            or record.get("repo") != cfg.repo):
        return False
    if record.get("merge_pending") is not None:
        return False
    merged = record.get("merged")
    if (not isinstance(merged, dict) or merged.get("status") != "complete"
            or merged.get("pr") != row["pr"]
            or merged.get("issue") != row["issue"]
            or not isinstance(merged.get("merge_commit"), str)
            or not _HEAD.fullmatch(merged["merge_commit"])
            or record.get("merge_commit") != merged["merge_commit"]):
        return False
    attempt = record.get("merge_attempt")
    return attempt is None or attempt == {
        "head": record["head"], "base": record.get("base"),
        "branch": record["branch"]}


def cleanup(cfg, *, gh: GitHub | None = None, dry_run: bool = True,
            max_items: int = 32, max_bytes: int = 268_435_456,
            now: datetime | int | float | None = None) -> dict:
    """Remove a bounded set of old, remotely verified merged artifacts."""
    if type(dry_run) is not bool or type(max_items) is not int or not 1 <= max_items <= 1000:
        raise ValueError("invalid cleanup item limit")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**4:
        raise ValueError("invalid cleanup byte limit")
    current = _epoch(now)
    days = cfg.retention_days
    if type(days) is not int or days < 0:
        raise ValueError("invalid retention window")
    result = {"dry_run": dry_run, "selected": [], "removed": [],
              "skipped": 0, "bytes": 0, "truncated": False}
    if days == 0:
        return result
    if not Path(cfg.state_dir).exists() and not Path(cfg.state_dir).is_symlink():
        return result
    _safe(Path(cfg.state_dir), directory=True)
    db = Path(cfg.state_dir) / "state.sqlite3"
    if not db.exists() and not db.is_symlink():
        return result
    _safe(db, directory=False)
    # A preview must not initialize or migrate an older state database.
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as preview:
        version = preview.execute("PRAGMA user_version").fetchone()[0]
        if version != state.SCHEMA_VERSION:
            raise ValueError("storage cleanup requires current state schema")
        state._validate_schema(preview)
    cutoff = current - days * 86400
    with state.exclusive(cfg.state_dir):
        with state.connect(cfg.state_dir) as conn:
            rows = {row["issue"]: row for row in state.jobs(conn)}
        items = _items(cfg, current)
        # A correction can supersede the ledger HEAD. Its private publication
        # record supplies the exact old Issue/PR/branch/base/HEAD binding.
        old_proofs: dict[str, list] = {}
        unbound_heads: set[str] = set()
        for proof_item in items:
            if proof_item["kind"] != "evidence":
                continue
            row = rows.get(proof_item["issue"])
            if row is None or not _valid_evidence(proof_item["path"], row,
                                                  head=proof_item["head"]):
                unbound_heads.add(proof_item["head"])
            elif proof_item["head"] != row["head_sha"]:
                old_proofs.setdefault(proof_item["head"], []).append(row)
        client = gh or GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
        source = Path(cfg.workspace) / "source"
        registered = None
        remote_cache: dict[tuple[int, int, str], bool] = {}
        old_head_cache: dict[int, set[str] | None] = {}
        unsafe_checkout_issues: set[int] = set()
        worktrees = [item for item in items if item["kind"] == "worktrees"]
        if worktrees:
            if _root(source):
                registered = _registered_worktrees(source)
            else:
                registered = set()
            for tree in worktrees:
                row = rows.get(tree["issue"])
                if row is None:
                    unsafe_checkout_issues.add(tree["issue"])
                    continue
                legacy = (row["issue"], row["status"], row["branch"],
                          row["head_sha"], row["pr"])
                if (tree["path"] not in registered
                        or not _verified_published_checkout(tree["path"],
                                                            tree["issue"], legacy)):
                    unsafe_checkout_issues.add(tree["issue"])
        selected_bytes = 0
        for item in items:
            issue = item["issue"]
            if item["kind"] == "logs":
                if item["head"] in unbound_heads:
                    result["skipped"] += 1
                    continue
                refs = [row for row in rows.values() if row["head_sha"] == item["head"]]
                refs.extend(old_proofs.get(item["head"], []))
                if not refs:
                    result["skipped"] += 1
                    continue
            else:
                row = rows.get(issue)
                refs = [row] if row is not None else []
            if (not refs or item["mtime"] > cutoff
                    or any(row["issue"] in unsafe_checkout_issues for row in refs)
                    or (item["kind"] == "evidence" and not _valid_evidence(
                        item["path"], refs[0], head=item["head"]))):
                result["skipped"] += 1
                continue
            for row in refs:
                key = (row["issue"], row["pr"], row["head_sha"])
                if key not in remote_cache:
                    remote_cache[key] = _remote_merged(cfg, client, row, cutoff)
            if any(not remote_cache[(row["issue"], row["pr"], row["head_sha"])]
                   or not _review_allows_cleanup(cfg, row) for row in refs):
                result["skipped"] += 1
                continue
            if item["kind"] in ("logs", "evidence"):
                for row in refs:
                    if item["head"] == row["head_sha"]:
                        continue
                    if row["pr"] not in old_head_cache:
                        old_head_cache[row["pr"]] = _remote_pr_heads(client, row["pr"])
                if any(item["head"] != row["head_sha"]
                       and (old_head_cache[row["pr"]] is None
                            or item["head"] not in old_head_cache[row["pr"]])
                       for row in refs):
                    result["skipped"] += 1
                    continue
            if (item["kind"] == "evidence" and item["head"] != refs[0]["head_sha"]
                    and any(other["kind"] == "logs" and other["head"] == item["head"]
                            and other["path"].exists() for other in items)):
                result["skipped"] += 1
                continue
            if item["kind"] == "worktrees":
                row = refs[0]
                legacy = (row["issue"], row["status"], row["branch"],
                          row["head_sha"], row["pr"])
                if item["path"] not in registered or not _verified_published_checkout(
                        item["path"], issue, legacy):
                    result["skipped"] += 1
                    continue
            if (len(result["selected"]) >= max_items
                    or selected_bytes + item["bytes"] > max_bytes):
                result["truncated"] = True
                continue
            public = {key: item[key] for key in ("kind", "issue", "head", "bytes")}
            result["selected"].append(public)
            selected_bytes += item["bytes"]
            if dry_run:
                continue
            # The private controller lock prevents cooperating processes from
            # replacing paths. Check the target again immediately before removal.
            try:
                if item["kind"] == "worktrees":
                    current_size, _ = _tree(item["path"], [_MAX_SCAN])
                    if (current_size != item["bytes"]
                            or current_size > max_bytes - result["bytes"]):
                        raise ValueError("checkout size changed or exceeds cleanup limit")
                    if not _verified_published_checkout(item["path"], issue, legacy):
                        raise ValueError("checkout changed before cleanup")
                    subprocess.run(["git", "-C", str(source), "worktree", "remove",
                                    str(item["path"])], check=True, capture_output=True,
                                   text=True)
                else:
                    info = _safe(item["path"], directory=False)
                    if info.st_size != item["bytes"] or info.st_mtime != item["mtime"]:
                        raise ValueError("artifact changed before cleanup")
                    if item["kind"] == "evidence" and not _valid_evidence(
                            item["path"], refs[0], head=item["head"]):
                        raise ValueError("evidence changed before cleanup")
                    item["path"].unlink()
                    _prune_empty(item["path"].parent,
                                 Path(cfg.state_dir) / ("ci-logs" if item["kind"] == "logs"
                                                        else "evidence"))
            except (OSError, ValueError, subprocess.CalledProcessError):
                result["skipped"] += 1
                continue
            result["removed"].append(public)
            result["bytes"] += item["bytes"]
    if dry_run:
        result["bytes"] = selected_bytes
    return result
