"""Bounded, durable JSON files for controller evidence."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import tempfile


MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_BROKER_EVIDENCE_BYTES = 65536


def _check_existing_file(info: os.stat_result, parent: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("evidence path must be a regular file")
    if os.name != "posix":
        return
    permissions = stat.S_IMODE(info.st_mode)
    legacy_private = permissions == 0o644 and not parent.st_mode & 0o077
    if (info.st_uid != os.geteuid() or info.st_nlink != 1
            or (permissions not in (0o600, 0o640) and not legacy_private)):
        raise ValueError("evidence file has unsafe ownership or permissions")


def _check_path(path: Path) -> None:
    """Reject links and existing files that are unsuitable for evidence."""
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise ValueError("evidence parent must be a real directory")
    if os.name == "posix" and (parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
        raise ValueError("evidence parent has unsafe ownership or permissions")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    _check_existing_file(info, parent)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                           parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid evidence JSON") from exc
    if type(value) is not dict:
        raise ValueError("evidence must be a JSON object")
    return value


def _fsync_directory(parent: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    unsupported = {errno.EINVAL, errno.ENOTSUP, errno.EBADF}
    try:
        fd = os.open(parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(fd)


def _serialize_evidence(payload: dict) -> bytes:
    if type(payload) is not dict:
        raise ValueError("evidence must be a dictionary")
    try:
        return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid evidence data") from exc


def validate_evidence_size(payload: dict, *, max_bytes: int) -> None:
    """Reject evidence whose exact on-disk serialization exceeds max_bytes."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("invalid evidence size limit")
    if len(_serialize_evidence(payload)) > max_bytes:
        raise ValueError("evidence is too large")


def write_evidence(path: Path, payload: dict, *, mode: int = 0o600,
                   max_bytes: int = MAX_EVIDENCE_BYTES) -> None:
    """Atomically replace evidence after syncing a private sibling file."""
    path = Path(path)
    if mode not in (0o600, 0o640):
        raise ValueError("unsupported evidence permissions")
    _check_path(path)
    data = _serialize_evidence(payload)
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("invalid evidence size limit")
    if len(data) > max_bytes or len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError("evidence is too large")

    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode) if hasattr(os, "fchmod") else os.chmod(temporary, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check_path(path)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def read_evidence(path: Path) -> dict:
    """Read one complete JSON object without following a final-path symlink."""
    path = Path(path)
    _check_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        _check_existing_file(info, path.parent.lstat())
        if info.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence is too large")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(data) > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence is too large")
        return _decode(data)
    finally:
        if fd >= 0:
            os.close(fd)
