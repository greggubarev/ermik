"""Parse a task's explicit scope and check paths before accepting changes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable


_SECTIONS = ("Allowed paths", "Goal", "Acceptance")
_HEADER = re.compile(r"^##\s+(.+?)\s*$")
_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class IssueSpec:
    allowed_paths: tuple[str, ...]
    goal: str
    acceptance: str
    body_sha256: str


def body_sha256(body: str) -> str:
    """Hash the exact UTF-8 issue body, including its whitespace."""
    if not isinstance(body, str):
        raise TypeError("issue body must be text")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or path != path.strip():
        raise ValueError("path must be a nonempty, unpadded relative path")
    if (path.startswith("/") or path.endswith("/") or "\\" in path
            or _DRIVE.match(path) or ":" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(char in path for char in "*?[]")):
        raise ValueError(f"unsafe path: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") or part.casefold() == ".git"
           for part in parts):
        raise ValueError(f"unsafe path: {path!r}")
    return path


def parse_issue(body: str) -> IssueSpec:
    """Require three explicit Markdown sections and a nonempty path scope."""
    digest = body_sha256(body)
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _HEADER.fullmatch(line)
        if match:
            heading = match.group(1)
            if heading in _SECTIONS:
                if heading in sections:
                    raise ValueError(f"duplicate section: {heading}")
                sections[heading] = []
                current = heading
            else:
                current = None
            continue
        if current is not None:
            sections[current].append(line)

    if any(name not in sections for name in _SECTIONS):
        raise ValueError("issue must contain Allowed paths, Goal, and Acceptance")

    raw_paths = [line.strip() for line in sections["Allowed paths"] if line.strip()]
    if not raw_paths:
        raise ValueError("Allowed paths must not be empty")
    allowed_paths = tuple(_safe_path(path) for path in raw_paths)
    if len(set(allowed_paths)) != len(allowed_paths):
        raise ValueError("duplicate allowed path")

    goal = "\n".join(sections["Goal"]).strip()
    acceptance = "\n".join(sections["Acceptance"]).strip()
    if not goal or not acceptance:
        raise ValueError("Goal and Acceptance must not be empty")
    return IssueSpec(allowed_paths, goal, acceptance, digest)


def validate_changed_paths(paths: Iterable[str], allowed_paths: Iterable[str]) -> None:
    """Reject unsafe changes and any path outside the allowed path prefixes."""
    allowed = tuple(_safe_path(path) for path in allowed_paths)
    if not allowed:
        raise ValueError("allowed paths must not be empty")
    for path in paths:
        changed = _safe_path(path)
        if not any(changed == prefix or changed.startswith(prefix + "/")
                   for prefix in allowed):
            raise ValueError(f"changed path outside allowed scope: {changed!r}")
