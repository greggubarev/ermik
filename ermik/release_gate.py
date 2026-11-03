"""Check a source release candidate and package its exact Git tree."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


class ReleaseError(Exception):
    """A candidate cannot be published as a source release."""


MAX_SOURCE_BYTES = 2_000_000
SOURCE_SUFFIXES = {".md", ".py", ".json", ".sh", ".service", ".svg"}
ROOT_FILES = {".gitignore", "AGENTS.md", "LICENSE", "README.md", "SECURITY.md"}
DOCKERFILES = {"examples/Dockerfile.demo", "examples/Dockerfile.opencode"}
DISALLOWED_PARTS = {
    ".codex", ".env", ".git", ".local", ".secrets", "build", "dist",
    "htmlcov", "logs", "state", "workspace", "workspaces", "__pycache__",
}
DISALLOWED_SUFFIXES = {
    ".db", ".db3", ".key", ".log", ".p12", ".pem", ".pyc",
    ".sqlite", ".sqlite3", ".token", ".zip", ".tar", ".gz",
}

# Build signatures in fragments so this scanner's source does not match itself.
SENSITIVE = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE" + r" KEY-----")),
    ("GitHub credential", re.compile(r"\b(?:gh[pousr]_" + r"[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("provider credential", re.compile(r"\bsk-" + r"[A-Za-z0-9_-]{20,}\b")),
    ("OpenCode credential", re.compile(r"\boc_" + r"sk_[A-Za-z0-9_-]{20,}\b")),
    ("provider credential", re.compile(r"\b[a-f0-9]{32}" + r"\.[A-Za-z0-9]{20,}\b")),
    ("Bearer credential", re.compile(r"(?i)\bBearer[ \t]+" + r"[A-Za-z0-9._~+/=-]{20,}\b")),
    ("cloud credential", re.compile(r"\b(?:AKIA|ASIA)" + r"[A-Z0-9]{16}\b")),
    ("credential environment value", re.compile(
        r"(?i)\b(?:GH_TOKEN|GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|ZAI_API_KEY|OPENCODE_API_KEY)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}['\"]?"
    )),
    ("credential assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret[_-]?key)"
        r"\s*[:=]\s*['\"]([A-Za-z0-9_+/=-]{20,})['\"]"
    )),
    ("credential URL", re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s/@:]+:[^\s/@]+@")),
    ("password assignment", re.compile(r"(?i)\bpassword\s*[:=]\s*['\"]" + r"[A-Za-z0-9_!@#$%^&*+=-]{7,}['\"]")),
    ("basic authentication URL", re.compile(r"(?i)\bhttps?://[^/\s:@]+:" + r"[^/\s@]{8,}@")),
    ("private home path", re.compile(
        r"(?i)(?:[A-Z]:[\\/]Users[\\/]|/" + r"Users/|/" + r"home/|/" + r"root/)[^\s/\\]+[/\\]"
    )),
    ("unrelated project reference", re.compile(r"(?i)\b(?:special-equipment-" + r"pwa|wow " + r"modern)\b")),
)


def _safe_location(name: str) -> str:
    return "path-sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _sensitive_category(content: str) -> str | None:
    for category, pattern in SENSITIVE:
        if pattern.search(content):
            return category
    return None


def _git(*args: str, cwd: Path) -> bytes:
    environment = os.environ.copy()
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=environment, check=False)
    if result.returncode:
        # Git stderr may contain source paths or remote URLs with credentials.
        raise ReleaseError(f"git {args[0]} failed")
    return result.stdout


def _allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or path.is_absolute() or any(p in {"", ".", ".."} for p in parts):
        return False
    if any(p.lower() in DISALLOWED_PARTS for p in parts):
        return False
    if path.suffix.lower() in DISALLOWED_SUFFIXES:
        return False
    if name in ROOT_FILES or name in DOCKERFILES:
        return True
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    if len(parts) == 2 and parts[0] in {"docs", "ermik", "tests", "templates", "assets"}:
        if parts[0] == "assets":
            return path.suffix == ".svg"
        return True
    if parts[0] == "examples" and len(parts) in {2, 3}:
        return len(parts) == 2 or (parts[1] == "systemd" and path.suffix == ".service")
    return len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md"


def _tree(commit: str, cwd: Path) -> list[tuple[str, str, str]]:
    entries = []
    for record in _git("ls-tree", "-rz", "--full-tree", commit, cwd=cwd).split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split(" ")
            name = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise ReleaseError("invalid Git tree entry") from None
        category = _sensitive_category(name)
        if category:
            raise ReleaseError(f"{category} in release path: {_safe_location(name)}")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ReleaseError(f"unsupported file type: {_safe_location(name)}")
        if not _allowed_path(name):
            raise ReleaseError(f"unexpected release path: {_safe_location(name)}")
        entries.append((name, mode, blob))
    return entries


def _scan(commit: str, cwd: Path) -> dict[str, bytes]:
    revisions = _git("rev-list", commit, cwd=cwd).decode("ascii").splitlines()
    if not revisions:
        raise ReleaseError("candidate has no reachable history")
    checked: set[str] = set()
    current: dict[str, bytes] = {}
    for revision in revisions:
        commit_record = _git("cat-file", "commit", revision, cwd=cwd).decode("utf-8", errors="replace")
        category = _sensitive_category(commit_record)
        if category:
            raise ReleaseError(f"{category} in commit metadata at {revision[:12]}; treat the value as exposed, rotate it, and remove it from release history")
        for name, _mode, blob in _tree(revision, cwd):
            if blob in checked:
                continue
            checked.add(blob)
            data = _git("cat-file", "blob", blob, cwd=cwd)
            if len(data) > MAX_SOURCE_BYTES or b"\0" in data:
                raise ReleaseError(f"binary or oversized source: {_safe_location(name)} at {revision[:12]}")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ReleaseError(f"non-UTF-8 source: {_safe_location(name)} at {revision[:12]}") from None
            category = _sensitive_category(content)
            if category:
                raise ReleaseError(f"{category} found: {_safe_location(name)} at {revision[:12]}; treat the value as exposed, rotate it, and remove it from release history")
    for name, _mode, blob in _tree(commit, cwd):
        current[name] = _git("cat-file", "blob", blob, cwd=cwd)
    return current


def _verify_archive(raw_tar: bytes, prefix: str, files: dict[str, bytes]) -> None:
    found: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(prefix):
                raise ReleaseError("archive contains an unsupported member")
            name = member.name[len(prefix):]
            if name not in files or name in found:
                raise ReleaseError("archive contents differ from the checked tree")
            source = archive.extractfile(member)
            if source is None or source.read() != files[name]:
                raise ReleaseError("archive contents differ from the checked tree")
            found.add(name)
    if found != files.keys():
        raise ReleaseError("archive contents differ from the checked tree")


def _inventory(files: dict[str, bytes]) -> dict[str, object]:
    images = []
    dependencies = []
    for name, data in sorted(files.items()):
        if name.startswith("examples/Dockerfile."):
            for line in data.decode("utf-8").splitlines():
                if line.startswith("FROM "):
                    images.append({"source": name, "reference": line.split()[1],
                                   "bundled": False, "notice": "Base image is referenced, not redistributed."})
                if line.startswith("RUN apk add "):
                    packages = [part for part in line.split()[3:] if not part.startswith("-")]
                    dependencies.append({"source": name, "packages": packages,
                                         "bundled": False})
    return {
        "bundled_images": [],
        "bundled_dependencies": [],
        "referenced_images": images,
        "referenced_dependencies": dependencies,
        "source_license": "LICENSE",
        "redistribution_notice": "The source archive contains no third-party image or vendored dependency. Review the terms of referenced base images before redistributing them.",
    }


def build_release(commit: str, output: Path, cwd: Path | None = None) -> tuple[Path, Path, Path]:
    cwd = (cwd or Path.cwd()).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
        raise ReleaseError("--commit must be a full lowercase Git object ID")
    head = _git("rev-parse", "HEAD", cwd=cwd).decode("ascii").strip()
    if head != commit:
        raise ReleaseError("candidate commit differs from HEAD")
    if _git("status", "--porcelain", "--untracked-files=all", cwd=cwd):
        raise ReleaseError("checkout is not clean")
    root = Path(_git("rev-parse", "--show-toplevel", cwd=cwd).decode("utf-8").strip()).resolve()
    output = output.resolve()
    if output == root or root in output.parents:
        raise ReleaseError("output directory must be outside the checkout")
    files = _scan(commit, cwd)
    if not files:
        raise ReleaseError("candidate tree is empty")
    stem = f"ermik-source-{commit[:12]}"
    archive = output / f"{stem}.tar.gz"
    checksum = output / f"{stem}.sha256"
    manifest = output / f"{stem}.manifest.json"
    if output.exists():
        raise ReleaseError("release output directory already exists")
    raw_tar = _git("-c", "core.autocrlf=false", "archive", "--format=tar",
                   f"--prefix={stem}/", commit, cwd=cwd)
    _verify_archive(raw_tar, f"{stem}/", files)
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0, compresslevel=9) as writer:
        writer.write(raw_tar)
    payload = stream.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "commit": commit,
        "archive": archive.name,
        "archive_sha256": digest,
        "file_count": len(files),
        "inventory": _inventory(files),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".ermik-release-", dir=output.parent))
    try:
        (stage / archive.name).write_bytes(payload)
        (stage / checksum.name).write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        (stage / manifest.name).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return archive, checksum, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and package an exact source release candidate")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        archive, checksum, manifest = build_release(args.commit, args.output)
    except ReleaseError as error:
        print(f"release gate failed: {error}", file=sys.stderr)
        return 1
    print(f"release gate passed: {archive.name}, {checksum.name}, {manifest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
