"""Recover abandoned task checkouts without discarding unverified work."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import subprocess


_ISSUE = re.compile(r"issue-([1-9][0-9]*)\Z")
_LIVE = frozenset({"running", "correcting"})
_PUBLISHED = frozenset({"awaiting_review", "merged"})
_HEAD = re.compile(r"[0-9a-f]{40}\Z")


def _canonical_directory(path: Path) -> bool:
    return (path.is_dir() and not path.is_symlink()
            and path.resolve(strict=True) == path.absolute())


def _registered_worktrees(source: Path) -> set[Path]:
    result = subprocess.run(
        ["git", "-C", str(source), "worktree", "list", "--porcelain", "-z"],
        check=True, capture_output=True, text=True,
    )
    return {Path(item.removeprefix("worktree ")).resolve(strict=False)
            for item in result.stdout.split("\0")
            if item.startswith("worktree ")}


def _verified_published_checkout(path: Path, issue: int, row: tuple) -> bool:
    """Require a published ledger identity and the same clean local commit."""
    _, status, branch, head, pr = row
    if (status not in _PUBLISHED or type(pr) is not int
            or pr <= 0 or not isinstance(head, str)
            or _HEAD.fullmatch(head) is None or not isinstance(branch, str)
            or not branch.startswith(f"work/issue-{issue}-")):
        return False
    try:
        current_head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        current_branch = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "-z",
             "--untracked-files=all", "--ignored=matching"],
            check=True, capture_output=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False
    return current_head == head and current_branch == branch and not changes


def recover_stale_worktrees(workspace: Path, conn: sqlite3.Connection) -> dict[str, list[int]]:
    """Remove clean stale issue worktrees under the controller's exclusive lock.

    Call after interrupted jobs are reconciled. Only published or merged jobs
    with a recorded PR and a matching, clean local HEAD are eligible. A crash
    checkout may hold the sole copy of an unpushed commit, even if Git calls it
    clean. Git performs the actual removal without --force.
    """
    workspace = Path(workspace)
    result: dict[str, list[int]] = {
        "removed": [], "preserved": [], "requires_inspection": [],
    }
    if not workspace.exists() and not workspace.is_symlink():
        return result
    if not _canonical_directory(workspace):
        raise ValueError("workspace must be an existing canonical directory")
    jobs = workspace / "jobs"
    if not jobs.exists() and not jobs.is_symlink():
        return result
    if not _canonical_directory(jobs):
        raise ValueError("jobs must be a canonical directory")
    paths = [(path, int(match.group(1))) for path in sorted(jobs.iterdir())
             if (match := _ISSUE.fullmatch(path.name)) is not None]
    if not paths:
        return result
    source = workspace / "source"
    if not source.exists() and not source.is_symlink():
        result["requires_inspection"] = [issue for _, issue in paths]
        return result
    if not _canonical_directory(source):
        raise ValueError("source must be a canonical directory")
    registered = _registered_worktrees(source)
    rows = {row[0]: tuple(row) for row in conn.execute(
        "SELECT issue,status,branch,head_sha,pr FROM jobs")}
    for path, issue in paths:
        row = rows.get(issue)
        if row is not None and row[1] in _LIVE:
            result["preserved"].append(issue)
            continue
        if not _canonical_directory(path) or path not in registered:
            result["requires_inspection"].append(issue)
            continue
        if row is None or not _verified_published_checkout(path, issue, row):
            result["requires_inspection"].append(issue)
            continue
        try:
            # Without --force, Git refuses dirty checkouts and keeps their data.
            subprocess.run(["git", "-C", str(source), "worktree", "remove", str(path)],
                           check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError:
            result["requires_inspection"].append(issue)
        else:
            result["removed"].append(issue)
    return result
