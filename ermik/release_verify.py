"""Validate a bounded bundle of release evidence for one exact Git commit."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

from . import release_gate


_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_REPORT = 64 * 1024
_MAX_MANIFEST = 64 * 1024
_MAX_ARCHIVE = 100 * 1024 * 1024
_MAX_UNPACKED = 100 * 1024 * 1024
_MAX_TAR_BYTES = 120 * 1024 * 1024
_MAX_FILES = 10_000


class BundleError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _read(path: Path, limit: int, code: str) -> bytes:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            raise BundleError(code)
        with path.open("rb") as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise BundleError(code)
        return data
    except OSError:
        raise BundleError(code) from None


def _json(path: Path, limit: int, code: str) -> dict:
    try:
        value = json.loads(_read(path, limit, code))
    except (ValueError, UnicodeError):
        raise BundleError(code) from None
    if not isinstance(value, dict):
        raise BundleError(code)
    return value


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 1_000_000


def _host(path: Path, commit: str, platform: str, docker: bool) -> None:
    report = _json(path, _MAX_REPORT, "host_report_invalid")
    if (report.get("commit") != commit or report.get("result") != "passed"
            or report.get("dirty") is not False or report.get("platform") != platform):
        raise BundleError("host_report_failed")
    unit = report.get("unit")
    if not isinstance(unit, dict) or unit.get("result") != "passed":
        raise BundleError("host_report_failed")
    names = ("run", "executed", "skipped", "allowed_skips", "failures",
             "errors", "unexpected_successes")
    if any(not _count(unit.get(name)) for name in names):
        raise BundleError("host_report_invalid")
    if (unit["executed"] == 0 or unit["run"] != unit["executed"] + unit["skipped"]
            or unit["skipped"] != unit["allowed_skips"]
            or any(unit[name] for name in ("failures", "errors", "unexpected_successes"))
            or unit.get("unexpected_skips") != []
            or unit.get("unexpected_skips_truncated") is not False):
        raise BundleError("host_report_failed")
    host = report.get("host")
    if not isinstance(host, dict):
        raise BundleError("host_report_invalid")
    if docker:
        details = report.get("docker")
        if (not isinstance(details, dict)
                or not isinstance(details.get("server_version"), str)
                or not 1 <= len(details["server_version"]) <= 80
                or not isinstance(details.get("image"), str)
                or not 1 <= len(details["image"]) <= 256
                or not isinstance(details.get("image_id"), str)
                or not _IMAGE_ID.fullmatch(details["image_id"])):
            raise BundleError("docker_identity_missing")
        if any(not isinstance(host.get(name), dict)
               or host[name].get("result") != "passed"
               or host[name].get("exit_code") != 0
               for name in ("smoke", "container_recovery")):
            raise BundleError("docker_checks_failed")
    elif host.get("result") != "not_requested":
        raise BundleError("host_report_invalid")


def _git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], capture_output=True, timeout=20,
                                check=False, env=_git_environment())
    except (OSError, subprocess.SubprocessError):
        raise BundleError("git_check_failed") from None
    if result.returncode:
        raise BundleError("git_check_failed")
    try:
        return result.stdout.decode("ascii").strip()
    except UnicodeError:
        raise BundleError("git_check_failed") from None


def _release(commit: str, previous_tag: str) -> None:
    if not _SHA.fullmatch(commit) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", previous_tag):
        raise BundleError("release_identity_invalid")
    if _git("rev-parse", "HEAD") != commit:
        raise BundleError("release_commit_mismatch")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise BundleError("checkout_dirty")
    if _git("rev-parse", "--verify", f"refs/tags/{previous_tag}^{{commit}}") == commit:
        raise BundleError("release_delta_missing")
    try:
        result = subprocess.run(["git", "merge-base", "--is-ancestor",
                                 f"refs/tags/{previous_tag}^{{commit}}", commit],
                                capture_output=True, timeout=20, check=False,
                                env=_git_environment())
    except (OSError, subprocess.SubprocessError):
        raise BundleError("git_check_failed") from None
    if result.returncode:
        raise BundleError("release_delta_invalid")


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _artifacts(commit: str, manifest_path: Path, archive_path: Path,
               checksum_path: Path) -> None:
    manifest = _json(manifest_path, _MAX_MANIFEST, "manifest_invalid")
    stem = f"ermik-source-{commit[:12]}"
    archive_name = stem + ".tar.gz"
    if (manifest.get("commit") != commit or manifest.get("archive") != archive_name
            or archive_path.name != archive_name
            or checksum_path.name != stem + ".sha256"
            or not isinstance(manifest.get("archive_sha256"), str)
            or not _DIGEST.fullmatch(manifest["archive_sha256"])
            or not _count(manifest.get("file_count"))
            or not 0 < manifest["file_count"] <= _MAX_FILES
            or not isinstance(manifest.get("inventory"), dict)):
        raise BundleError("manifest_invalid")
    expected = f'{manifest["archive_sha256"]}  {archive_name}\n'.encode("ascii")
    if _read(checksum_path, 256, "checksum_invalid") not in (expected, expected[:-1] + b"\r\n"):
        raise BundleError("checksum_invalid")
    try:
        tree = {name: (mode, blob) for name, mode, blob in release_gate._tree(commit, Path.cwd())}
    except (release_gate.ReleaseError, OSError, ValueError):
        raise BundleError("git_check_failed") from None
    if not tree or len(tree) > _MAX_FILES:
        raise BundleError("archive_tree_mismatch")
    try:
        if not archive_path.is_file() or archive_path.stat().st_size > _MAX_ARCHIVE:
            raise BundleError("archive_invalid")
        digest = hashlib.sha256()
        with archive_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != manifest["archive_sha256"]:
            raise BundleError("archive_hash_mismatch")
        with gzip.open(archive_path, "rb") as source:
            raw_tar = source.read(_MAX_TAR_BYTES + 1)
        if len(raw_tar) > _MAX_TAR_BYTES:
            raise BundleError("archive_size_limit")
        names: set[str] = set()
        unpacked = 0
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                prefix = stem + "/"
                if not member.isfile() or not member.name.startswith(prefix):
                    raise BundleError("archive_invalid")
                name = member.name[len(prefix):]
                parts = PurePosixPath(name).parts
                if (not name or name in names or len(names) >= _MAX_FILES
                        or member.size > 2_000_000
                        or any(part in {".", "..", ""} for part in parts)):
                    raise BundleError("archive_invalid")
                unpacked += member.size
                if unpacked > _MAX_UNPACKED:
                    raise BundleError("archive_size_limit")
                if name not in tree or member.mode & 0o777 != int(tree[name][0], 8) & 0o777:
                    raise BundleError("archive_tree_mismatch")
                try:
                    original = release_gate._git("cat-file", "blob", tree[name][1], cwd=Path.cwd())
                except release_gate.ReleaseError:
                    raise BundleError("git_check_failed") from None
                source = archive.extractfile(member)
                if source is None or source.read(member.size + 1) != original:
                    raise BundleError("archive_tree_mismatch")
                names.add(name)
        if len(names) != manifest["file_count"]:
            raise BundleError("manifest_count_mismatch")
        if names != tree.keys():
            raise BundleError("archive_tree_mismatch")
    except (OSError, tarfile.TarError, EOFError):
        raise BundleError("archive_invalid") from None


def verify(commit: str, previous_tag: str, windows: Path, linux: Path,
           docker: Path, manifest: Path, archive: Path, checksum: Path) -> None:
    _release(commit, previous_tag)
    _host(windows, commit, "windows", False)
    _host(linux, commit, "linux", False)
    _host(docker, commit, "linux", True)
    _artifacts(commit, manifest, archive, checksum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one source release evidence bundle")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--previous-tag", required=True)
    for name in ("windows", "linux", "docker", "manifest", "archive", "checksum"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        verify(args.commit, args.previous_tag, args.windows, args.linux, args.docker,
               args.manifest, args.archive, args.checksum)
    except BundleError as error:
        print(json.dumps({"result": "failed", "reason": error.code}, separators=(",", ":")))
        return 1
    print('{"result":"passed"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
