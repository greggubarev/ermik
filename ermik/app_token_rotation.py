"""Durable rotation of a GitHub App user token obtained through Device Flow.

Only the trusted broker should open this file. A refresh request consumes the old
pair at GitHub, so an interrupted request with no locally saved replacement is
ambiguous and must be resolved by a fresh Device Flow authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_MAX_BYTES = 65536
_ENDPOINT = "https://github.com/login/oauth/access_token"


class TokenRotationError(RuntimeError):
    """A credential refusal with an operator-safe message."""


@dataclass(frozen=True, repr=False)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_token_expires_at: int

    def due(self, now: float | None = None, margin: int = 3600) -> bool:
        if now is None:
            now = time.time()
        return now + margin >= self.expires_at


def _validate_pair(value: object) -> TokenPair:
    if not isinstance(value, dict) or set(value) != {
        "access_token", "refresh_token", "expires_at", "refresh_token_expires_at"
    }:
        raise TokenRotationError("invalid token pair; reauthorize with Device Flow")
    access, refresh = value["access_token"], value["refresh_token"]
    expires, refresh_expires = value["expires_at"], value["refresh_token_expires_at"]
    if (not isinstance(access, str) or not access or any(c.isspace() for c in access)
            or not isinstance(refresh, str) or not refresh
            or any(c.isspace() for c in refresh)
            or type(expires) is not int or expires <= 0
            or type(refresh_expires) is not int or refresh_expires <= expires):
        raise TokenRotationError("invalid token pair; reauthorize with Device Flow")
    return TokenPair(access, refresh, expires, refresh_expires)


def _directory_sync(directory: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_read(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES
                or (os.name == "posix" and (info.st_uid != os.geteuid()
                                             or stat.S_IMODE(info.st_mode) != 0o600))):
            raise TokenRotationError("token store permissions invalid; contact operator")
        data = os.read(fd, _MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise TokenRotationError("token store invalid; contact operator")
        return data
    finally:
        os.close(fd)


def _fingerprint(pair: TokenPair) -> str:
    return hashlib.sha256(
        (pair.access_token + "\0" + pair.refresh_token).encode("utf-8")
    ).hexdigest()


def _private_create(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.pending = self.path.with_name(self.path.name + ".pending")
        self.temporary = self.path.with_name(self.path.name + ".new")
        self.reauthorization_marker = self.path.with_name(self.path.name + ".pending.reauth")

    def _check_location(self) -> None:
        parent = self.path.parent
        if not self.path.is_absolute() or not parent.is_dir() or parent.is_symlink():
            raise TokenRotationError("token store location invalid; contact operator")
        if os.name == "posix":
            info = parent.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise TokenRotationError("token store directory permissions invalid")

    def _read_pair(self, path: Path) -> TokenPair:
        try:
            return _validate_pair(json.loads(_private_read(path)))
        except (OSError, ValueError, UnicodeError) as exc:
            raise TokenRotationError(
                "token pair unavailable; reauthorize with Device Flow"
            ) from None

    @staticmethod
    def _serialize(pair: TokenPair) -> bytes:
        return json.dumps({
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_at": pair.expires_at,
            "refresh_token_expires_at": pair.refresh_token_expires_at,
        }, separators=(",", ":")).encode("utf-8")

    def initialize(self, pair: TokenPair) -> TokenPair:
        """Install a Device Flow pair once, without replacing existing state."""
        self._check_location()
        if not isinstance(pair, TokenPair):
            raise TokenRotationError("invalid token pair; reauthorize with Device Flow")
        pair = _validate_pair({
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_at": pair.expires_at,
            "refresh_token_expires_at": pair.refresh_token_expires_at,
        })
        if any(os.path.lexists(path) for path in
               (self.path, self.pending, self.temporary, self.reauthorization_marker)):
            raise TokenRotationError("token store already exists; contact operator")
        try:
            _private_create(self.path, self._serialize(pair))
            _directory_sync(self.path.parent)
        except OSError:
            raise TokenRotationError("cannot initialize token store; contact operator") from None
        return pair

    def reauthorize(self, pair: TokenPair) -> TokenPair:
        """Install a newly authorized Device Flow pair after operator action."""
        self._check_location()
        if not isinstance(pair, TokenPair):
            raise TokenRotationError("invalid token pair; reauthorize with Device Flow")
        replacement = _validate_pair({
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_at": pair.expires_at,
            "refresh_token_expires_at": pair.refresh_token_expires_at,
        })
        current = self._read_pair(self.path)
        if (replacement.access_token == current.access_token
                or replacement.refresh_token == current.refresh_token):
            raise TokenRotationError("new Device Flow token pair required")
        marker = json.dumps({"old_pair_sha256": _fingerprint(current)},
                            separators=(",", ":")).encode("ascii")
        try:
            if os.path.lexists(self.reauthorization_marker):
                if _private_read(self.reauthorization_marker) != marker:
                    raise TokenRotationError(
                        "reauthorization state invalid; contact operator"
                    )
            else:
                _private_create(self.reauthorization_marker, marker)
            _directory_sync(self.path.parent)
            # The operator's new pair supersedes any unfinished refresh result.
            if os.path.lexists(self.temporary):
                self.temporary.unlink()
                _directory_sync(self.path.parent)
            _private_create(self.temporary, self._serialize(replacement))
            os.replace(self.temporary, self.path)
            _directory_sync(self.path.parent)
            self.pending.unlink(missing_ok=True)
            _directory_sync(self.path.parent)
            self.reauthorization_marker.unlink()
            _directory_sync(self.path.parent)
        except OSError:
            raise TokenRotationError("token reauthorization incomplete; contact operator") from None
        return replacement

    def load(self) -> TokenPair:
        """Return the durable pair, refusing an ambiguous interrupted refresh."""
        self._check_location()
        pair = self._read_pair(self.path)
        if os.path.lexists(self.reauthorization_marker):
            try:
                marker = json.loads(_private_read(self.reauthorization_marker))
            except (OSError, ValueError, UnicodeError):
                raise TokenRotationError(
                    "reauthorization state invalid; contact operator"
                ) from None
            if (not isinstance(marker, dict)
                    or set(marker) != {"old_pair_sha256"}
                    or not isinstance(marker["old_pair_sha256"], str)
                    or len(marker["old_pair_sha256"]) != 64
                    or marker["old_pair_sha256"] == _fingerprint(pair)
                    or os.path.lexists(self.temporary)):
                raise TokenRotationError(
                    "reauthorization incomplete; contact operator"
                )
            # The new pair is durable. Finish removing old refresh state.
            self.pending.unlink(missing_ok=True)
            self.reauthorization_marker.unlink()
            _directory_sync(self.path.parent)
            return pair
        try:
            marker = json.loads(_private_read(self.pending))
        except FileNotFoundError:
            if os.path.lexists(self.temporary):
                raise TokenRotationError(
                    "token replacement state invalid; reauthorize with Device Flow"
                )
            return pair
        except (OSError, ValueError, UnicodeError) as exc:
            raise TokenRotationError(
                "token rotation state invalid; reauthorize with Device Flow"
            ) from None
        if (not isinstance(marker, dict) or set(marker) != {"old_pair_sha256"}
                or not isinstance(marker["old_pair_sha256"], str)
                or len(marker["old_pair_sha256"]) != 64):
            raise TokenRotationError(
                "token rotation state invalid; reauthorize with Device Flow"
            )
        if marker["old_pair_sha256"] == _fingerprint(pair):
            if os.path.lexists(self.temporary):
                candidate = self._read_pair(self.temporary)
                if (candidate.access_token == pair.access_token
                        or candidate.refresh_token == pair.refresh_token):
                    raise TokenRotationError(
                        "token replacement state invalid; reauthorize with Device Flow"
                    )
                try:
                    os.replace(self.temporary, self.path)
                    _directory_sync(self.path.parent)
                    self.pending.unlink()
                    _directory_sync(self.path.parent)
                except OSError:
                    raise TokenRotationError(
                        "token replacement incomplete; reauthorize with Device Flow"
                    ) from None
                return candidate
            raise TokenRotationError(
                "token refresh outcome unknown; reauthorize with Device Flow"
            )
        # The complete new pair was atomically installed before the process died.
        if os.path.lexists(self.temporary):
            raise TokenRotationError(
                "token replacement state invalid; reauthorize with Device Flow"
            )
        self.pending.unlink()
        _directory_sync(self.path.parent)
        return pair

    def refresh(self, client_id: str, *, opener=urlopen,
                now: float | None = None) -> TokenPair:
        """Refresh when within one hour of expiry and persist both tokens together."""
        if now is None:
            now = time.time()
        pair = self.load()
        if not pair.due(now):
            return pair
        if (not isinstance(client_id, str) or not client_id
                or any(c.isspace() for c in client_id)):
            raise TokenRotationError("GitHub App client ID unavailable; contact operator")
        if now >= pair.refresh_token_expires_at:
            raise TokenRotationError("refresh token expired; reauthorize with Device Flow")
        marker = json.dumps({"old_pair_sha256": _fingerprint(pair)},
                            separators=(",", ":")).encode("ascii")
        try:
            _private_create(self.pending, marker)
            _directory_sync(self.path.parent)
        except FileExistsError:
            raise TokenRotationError(
                "token refresh already pending; reauthorize with Device Flow"
            ) from None
        except OSError:
            raise TokenRotationError("cannot prepare token refresh; contact operator") from None
        body = urlencode({"client_id": client_id, "grant_type": "refresh_token",
                          "refresh_token": pair.refresh_token}).encode("ascii")
        request = Request(_ENDPOINT, data=body, method="POST",
                          headers={"Accept": "application/json",
                                   "Content-Type": "application/x-www-form-urlencoded",
                                   "User-Agent": "ermik-pr-broker"})
        try:
            with opener(request, timeout=20) as response:
                try:
                    raw = response.read(_MAX_BYTES + 1)
                except TypeError:
                    # Simple test doubles may expose only read() without a size.
                    raw = response.read()
            if len(raw) > _MAX_BYTES:
                raise ValueError("oversized response")
            result = json.loads(raw)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("refresh refusal")
            access, refresh = result.get("access_token"), result.get("refresh_token")
            access_life = result.get("expires_in")
            refresh_life = result.get("refresh_token_expires_in")
            if (not isinstance(access, str) or not access or access == pair.access_token
                    or not isinstance(refresh, str) or not refresh
                    or refresh == pair.refresh_token
                    or type(access_life) is not int or access_life <= 3600
                    or type(refresh_life) is not int or refresh_life <= access_life
                    or result.get("token_type", "bearer").lower() != "bearer"):
                raise ValueError("invalid refresh response")
            replacement = _validate_pair({
                "access_token": access, "refresh_token": refresh,
                "expires_at": int(now) + access_life,
                "refresh_token_expires_at": int(now) + refresh_life,
            })
        except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError,
                UnicodeError, AttributeError):
            raise TokenRotationError(
                "GitHub token refresh failed or outcome unknown; reauthorize with Device Flow"
            ) from None
        data = self._serialize(replacement)
        try:
            _private_create(self.temporary, data)
            os.replace(self.temporary, self.path)
            _directory_sync(self.path.parent)
            self.pending.unlink()
            _directory_sync(self.path.parent)
        except OSError:
            # GitHub may have invalidated the old pair. Keep the pending marker,
            # so restart cannot accidentally publish with it.
            raise TokenRotationError(
                "token replacement incomplete; reauthorize with Device Flow"
            ) from None
        return replacement
