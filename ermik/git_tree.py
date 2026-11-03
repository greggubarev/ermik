"""Inspect changed paths in exact Git tree objects before reviewing a PR."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from .policy import _safe_path, validate_changed_paths


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REGULAR = {"100644", "100755"}
_MAX_DIFF_BYTES = 100 * 1024


class TreeError(ValueError):
    """The fetched tree cannot establish a safe, complete changed-file set."""


def _git(checkout: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(checkout), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TreeError("Git tree inspection failed") from exc
    if len(result.stdout) > _MAX_DIFF_BYTES:
        raise TreeError("Git tree inspection output too large")
    return result.stdout


def _path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
        if len(raw) > 512 or _safe_path(path) != path:
            raise ValueError("unsafe path")
    except (UnicodeError, ValueError) as exc:
        raise TreeError("unsafe changed path") from exc
    return path


def _entry(checkout: Path, sha: str, path: str) -> tuple[str, str] | None:
    """Get one exact path entry, never a pathspec match for its descendants."""
    data = _git(checkout, "ls-tree", "-z", "--full-tree", sha, "--", path)
    if not data:
        return None
    rows = data.rstrip(b"\0").split(b"\0")
    if len(rows) != 1 or b"\t" not in rows[0]:
        raise TreeError("ambiguous tree entry")
    header, name = rows[0].split(b"\t", 1)
    if name != path.encode("utf-8"):
        raise TreeError("unexpected tree entry")
    parts = header.split(b" ")
    if len(parts) != 3:
        raise TreeError("malformed tree entry")
    try:
        mode, kind = parts[0].decode("ascii"), parts[1].decode("ascii")
    except UnicodeError as exc:
        raise TreeError("malformed tree mode") from exc
    return mode, kind


def _inspect(checkout: Path, sha: str, path: str, *, required: bool) -> None:
    parts = path.split("/")
    for index in range(1, len(parts)):
        parent = "/".join(parts[:index])
        entry = _entry(checkout, sha, parent)
        if entry is None:
            if required:
                raise TreeError("changed path parent missing")
            return
        if entry != ("040000", "tree"):
            raise TreeError("changed path has a non-tree parent")
    entry = _entry(checkout, sha, path)
    if entry is None:
        if required:
            raise TreeError("changed path missing from tree")
    elif entry[0] not in _REGULAR or entry[1] != "blob":
        raise TreeError("changed path is not a regular file")


def verify_changed_tree(checkout: Path, base_sha: str, head_sha: str,
                        *, allowed_paths: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Return GitHub-style changed names after checking both sides of the exact diff.

    A rename contributes its new name to the return value; both old and new
    names are inspected. Deletions contribute their old name. The caller must
    compare the returned names with the complete published PR file list.
    """
    if (type(base_sha) is not str or not _SHA.fullmatch(base_sha)
            or type(head_sha) is not str or not _SHA.fullmatch(head_sha)
            or base_sha == head_sha):
        raise TreeError("invalid commit identity")
    if _git(checkout, "rev-parse", "HEAD").strip() != head_sha.encode("ascii"):
        raise TreeError("checkout HEAD differs from fetched PR HEAD")
    data = _git(checkout, "diff", "--name-status", "-z", "--find-renames",
                "--no-ext-diff", f"{base_sha}...{head_sha}", "--")
    if not data or not data.endswith(b"\0"):
        raise TreeError("empty or malformed PR diff")
    fields = data[:-1].split(b"\0")
    published: list[str] = []
    changes: list[tuple[str, str, str | None]] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor]
        cursor += 1
        if not status or status[:1] not in (b"A", b"M", b"D", b"T", b"R", b"C"):
            raise TreeError("unexpected diff status")
        rename = status[:1] in (b"R", b"C")
        if (rename and not re.fullmatch(rb"[RC][0-9]{1,3}", status)) or (
                not rename and status not in (b"A", b"M", b"D", b"T")):
            raise TreeError("malformed diff status")
        needed = 2 if rename else 1
        if cursor + needed > len(fields):
            raise TreeError("truncated diff entry")
        old = _path(fields[cursor])
        new = _path(fields[cursor + 1]) if rename else None
        cursor += needed
        changes.append((status.decode("ascii"), old, new))
        published.append(new or old)
        if len(changes) > 100:
            raise TreeError("PR changes too many files")
    if len(set(published)) != len(published):
        raise TreeError("duplicate changed path")
    if allowed_paths is not None:
        all_names = [name for _, old, new in changes
                     for name in (old, new) if name is not None]
        try:
            validate_changed_paths(all_names, allowed_paths)
        except ValueError as exc:
            raise TreeError("changed path outside admitted scope") from exc
    for status, old, new in changes:
        kind = status[0]
        if new is not None:
            _inspect(checkout, base_sha, old, required=True)
            _inspect(checkout, head_sha, old, required=False)
            _inspect(checkout, head_sha, new, required=True)
        elif kind == "A":
            _inspect(checkout, base_sha, old, required=False)
            _inspect(checkout, head_sha, old, required=True)
        elif kind == "D":
            _inspect(checkout, base_sha, old, required=True)
            _inspect(checkout, head_sha, old, required=False)
        else:
            _inspect(checkout, base_sha, old, required=True)
            _inspect(checkout, head_sha, old, required=True)
    return tuple(sorted(published))
