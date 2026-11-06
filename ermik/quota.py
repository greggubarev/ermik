"""Bounded, read-only observations of local provider quota windows."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import http.client
import json
import math


_PATHS = {
    "kimi-code": ("kimi-cli-local", "/api/v1/oauth/usage"),
    "opencode-go": ("opencode-local", "/api/experimental/usage/limits"),
}
_KIMI_WINDOWS = ("limit5h", "limit7d", "monthTotal", "monthCode")
_GO_WINDOWS = ("rolling", "weekly", "monthly")
_REASONS = {
    "unavailable", "unauthorized", "rate_limited", "missing_endpoint",
    "invalid_response", "stale", "invalid_configuration",
}
_MAX_RESPONSE = 64 * 1024


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("observed_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def unknown(kind: str, reason: str, observed_at: datetime | None = None) -> dict:
    """Return a safe unknown observation; never propagate arbitrary error text."""
    return {
        "source": _PATHS.get(kind, ("unknown-local", ""))[0],
        "observed_at": _iso(_utc(observed_at)),
        "status": "unknown",
        "unit": "percent",
        "windows": [],
        "reason": reason if isinstance(reason, str) and reason in _REASONS else "invalid_response",
    }


def _reset_rfc3339(value: object, now: datetime) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid reset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid reset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid reset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= now:
        raise _StaleObservation
    return _iso(parsed)


class _StaleObservation(Exception):
    pass


def _number(value: object, upper: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper:
        raise ValueError("invalid percentage")
    return round(float(value), 6)


def _kimi(payload: dict, now: datetime) -> list[dict]:
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("error envelope")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("kind") != "ok":
        raise ValueError("missing usage")
    quota = data.get("quota")
    usages = quota.get("usages") if isinstance(quota, dict) else None
    if not isinstance(usages, dict):
        raise ValueError("missing windows")
    windows = []
    for name in _KIMI_WINDOWS:
        if name not in usages:
            continue
        item = usages[name]
        if not isinstance(item, dict):
            raise ValueError("invalid window")
        windows.append({
            "name": name,
            "used_percent": round(_number(item.get("usedRatio"), 1) * 100, 6),
            "reset_at": _reset_rfc3339(item.get("resetAt"), now),
        })
    if not windows:
        raise ValueError("missing windows")
    return windows


def _go(payload: dict, now: datetime) -> list[dict]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("missing data")
    go = data.get("go")
    if not isinstance(go, dict):
        raise ValueError("missing go")
    windows = []
    for name in _GO_WINDOWS:
        if name not in go:
            continue
        item = go[name]
        if not isinstance(item, dict) or type(item.get("limited")) is not bool:
            raise ValueError("invalid window")
        milliseconds = item.get("resetsAt")
        if type(milliseconds) not in (int, float) or not math.isfinite(milliseconds):
            raise ValueError("invalid reset")
        try:
            reset = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("invalid reset") from exc
        if reset <= now:
            raise _StaleObservation
        windows.append({
            "name": name,
            "used_percent": _number(item.get("percent"), 100),
            "reset_at": _iso(reset),
        })
    if not windows:
        raise ValueError("missing windows")
    return windows


def probe(kind: str, port: int, auth_value: str | None, *,
          observed_at: datetime | None = None) -> dict:
    """GET one fixed loopback endpoint and project quota windows only."""
    now = _utc(observed_at)
    if kind not in _PATHS or type(port) is not int or not 1 <= port <= 65535:
        return unknown(kind, "invalid_configuration", now)
    if auth_value is not None and (not isinstance(auth_value, str) or
                                   not auth_value or "\r" in auth_value or "\n" in auth_value):
        return unknown(kind, "invalid_configuration", now)
    source, path = _PATHS[kind]
    headers = {"Accept": "application/json"}
    if auth_value is not None:
        if kind == "kimi-code":
            headers["Authorization"] = "Bearer " + auth_value
        else:
            password = base64.b64encode(("opencode:" + auth_value).encode("utf-8")).decode("ascii")
            headers["Authorization"] = "Basic " + password
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status in (401, 403):
            return unknown(kind, "unauthorized", now)
        if response.status == 404:
            return unknown(kind, "missing_endpoint", now)
        if response.status == 429:
            return unknown(kind, "rate_limited", now)
        if response.status != 200:
            return unknown(kind, "unavailable", now)
        raw = response.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            return unknown(kind, "invalid_response", now)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid envelope")
        if kind == "kimi-code":
            data = payload.get("data")
            if payload.get("code") == 0 and isinstance(data, dict) and data.get("kind") == "error":
                return unknown(kind, "rate_limited" if data.get("status") == 429 else "unavailable", now)
            windows = _kimi(payload, now)
        else:
            windows = _go(payload, now)
        return {"source": source, "observed_at": _iso(now), "status": "ok",
                "unit": "percent", "windows": windows}
    except _StaleObservation:
        return unknown(kind, "stale", now)
    except (OSError, http.client.HTTPException):
        return unknown(kind, "unavailable", now)
    except (ValueError, UnicodeDecodeError, TypeError):
        return unknown(kind, "invalid_response", now)
    finally:
        connection.close()
