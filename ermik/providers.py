"""Provider identities, quota cooldowns, and noninteractive runner contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
from typing import Literal, Mapping, Sequence
from urllib.parse import urlsplit
import ipaddress


# A provider is an account and endpoint, not merely a model name. These two
# integrations are pinned; further accounts can be declared in trusted config.
PROVIDERS = {
    "kimi-code": ("KIMI_CODE_API_KEY", "https://api.kimi.ai/coding/v1",
                  "@ai-sdk/openai-compatible"),
    "opencode-go": ("OPENCODE_GO_API_KEY", "https://opencode.ai/zen/go/v1",
                    "@ai-sdk/openai-compatible"),
}
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_CUSTOM_PACKAGES = frozenset({"@ai-sdk/openai-compatible", "@ai-sdk/openai"})


def _valid_https_url(value: str) -> bool:
    if not isinstance(value, str) or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or port == 0
            or (parsed.path and not parsed.path.startswith("/")) or "\\" in value):
        return False
    host = parsed.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9.-]+", host) and "." in host
                    and not host.startswith(".") and not host.endswith("."))
    return address.is_global


@dataclass(frozen=True)
class ModelCandidate:
    provider_id: str
    model_id: str
    credential_env: str
    base_url: str | None = None
    package: str | None = None
    max_parallel: int = 1
    effort: str | None = None
    supported_efforts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.provider_id, str) or not _IDENTIFIER.fullmatch(self.provider_id)
                or not isinstance(self.model_id, str) or not _IDENTIFIER.fullmatch(self.model_id)):
            raise ValueError("invalid provider or model ID")
        if self.provider_id in PROVIDERS:
            expected_env, expected_url, expected_package = PROVIDERS[self.provider_id]
            if (self.credential_env != expected_env
                    or self.base_url not in (None, expected_url)
                    or self.package not in (None, expected_package)):
                raise ValueError("provider credential or endpoint mismatch")
        else:
            if (not isinstance(self.credential_env, str) or not _ENV_NAME.fullmatch(self.credential_env)
                    or not self.credential_env.endswith(("_API_KEY", "_ACCESS_KEY", "_TOKEN"))
                    or any(fragment in self.credential_env for fragment in
                           ("GITHUB", "GH_", "GIT_", "SSH_", "ERMIK_", "OPENCODE_"))):
                raise ValueError("invalid provider credential variable")
            if not _valid_https_url(self.base_url):
                raise ValueError("custom provider requires a public HTTPS base URL")
            if self.package not in (None, *_CUSTOM_PACKAGES):
                raise ValueError("custom provider package is not allowed")
        if type(self.max_parallel) is not int or not 1 <= self.max_parallel <= 8:
            raise ValueError("max_parallel must be 1..8")
        if self.effort not in (None, "low", "medium", "high", "max"):
            raise ValueError("invalid reasoning effort")
        if (type(self.supported_efforts) is not tuple
                or any(type(value) is not str for value in self.supported_efforts)
                or len(set(self.supported_efforts)) != len(self.supported_efforts)
                or any(value not in ("low", "medium", "high", "max")
                       for value in self.supported_efforts)):
            raise ValueError("invalid supported reasoning efforts")
        if self.effort is not None and self.effort not in self.supported_efforts:
            raise ValueError("reasoning effort is not declared for the model")

    @property
    def ref(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    @property
    def endpoint(self) -> str:
        return PROVIDERS[self.provider_id][1] if self.provider_id in PROVIDERS else self.base_url  # type: ignore[return-value]

    @property
    def runtime_package(self) -> str:
        return (PROVIDERS[self.provider_id][2] if self.provider_id in PROVIDERS
                else self.package or "@ai-sdk/openai-compatible")


def model_spec(candidate: ModelCandidate) -> str:
    """Serialize only public routing metadata for the disposable worker."""
    fields = {"provider_id": candidate.provider_id,
              "model_id": candidate.model_id,
              "credential_env": candidate.credential_env,
              "base_url": candidate.base_url,
              "package": candidate.package,
              "max_parallel": candidate.max_parallel}
    if candidate.effort is not None:
        fields["effort"] = candidate.effort
        fields["supported_efforts"] = candidate.supported_efforts
    return json.dumps(fields, separators=(",", ":"),
                      sort_keys=True)


def candidate_from_dict(raw: Mapping[str, object]) -> ModelCandidate:
    """Parse a trusted local config entry, rejecting unknown fields and IDs."""
    if not isinstance(raw, Mapping) or not {"provider_id", "model_id", "credential_env"}.issubset(raw):
        raise ValueError("model requires provider_id, model_id, credential_env")
    if set(raw) - {"provider_id", "model_id", "credential_env", "base_url", "package", "max_parallel", "effort", "supported_efforts"}:
        raise ValueError("unknown model option")
    for key, value in raw.items():
        if key in ("max_parallel", "supported_efforts"):
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError("model options must be strings")
    fields = dict(raw)
    if "supported_efforts" in fields:
        if not isinstance(fields["supported_efforts"], list):
            raise ValueError("supported_efforts must be an array")
        fields["supported_efforts"] = tuple(fields["supported_efforts"])
    return ModelCandidate(**fields)  # type: ignore[arg-type]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_cooldowns (
  provider_id TEXT PRIMARY KEY,
  retry_at INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK(reason IN ('quota'))
)
"""
_LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_leases (
  lease_id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  route_key TEXT,
  reserved_cost REAL NOT NULL DEFAULT 0
)
"""


def _epoch(value: datetime | int | float | None) -> int:
    if value is None:
        return int(datetime.now(timezone.utc).timestamp())
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("time must be timezone aware")
        return int(value.timestamp())
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid time")
    return int(value)


def _budget(conn: sqlite3.Connection, provider_id: str, policies: Mapping | None,
            instant: int) -> tuple[float, float] | None:
    if not policies or provider_id not in policies:
        return None
    policy = policies[provider_id]
    ceiling = getattr(policy, "estimated_budget_ceiling", None)
    reservation = getattr(policy, "attempt_reservation", None)
    if (type(ceiling) not in (float, int) or not math.isfinite(ceiling) or ceiling <= 0
            or type(reservation) not in (float, int) or not math.isfinite(reservation)
            or reservation < 0):
        raise ValueError("invalid budget policy")
    start = datetime.fromtimestamp(instant, timezone.utc).strftime("%Y-%m-01 00:00:00")
    year, month = map(int, start[:7].split("-"))
    end = (f"{year + 1:04d}-01-01 00:00:00" if month == 12 else
           f"{year:04d}-{month + 1:02d}-01 00:00:00")
    usage_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='usage_events'").fetchone()
    observed = 0.0
    if usage_table:
        row = conn.execute("SELECT coalesce(sum(estimated_cost),0),"
                           "sum(CASE WHEN estimated_cost IS NULL AND NOT ("
                           "result_code=-2 AND finished_at IS NULL AND EXISTS ("
                           "SELECT 1 FROM provider_leases l WHERE l.lease_id=usage_events.attempt_id "
                           "AND l.provider_id=usage_events.provider_id AND l.expires_at>?)) "
                           "THEN 1 ELSE 0 END) FROM usage_events "
                           "WHERE provider_id=? AND created_at>=? AND created_at<?",
                           (instant, provider_id, start, end)).fetchone()
        if row[1]:
            return (float(ceiling), math.inf)
        observed = float(row[0])
    reserved = conn.execute("SELECT coalesce(sum(reserved_cost),0) "
                            "FROM provider_leases WHERE provider_id=? AND expires_at>?",
                            (provider_id, instant)).fetchone()[0]
    return float(ceiling), observed + float(reserved)


def _budget_allows(conn: sqlite3.Connection, provider_id: str,
                   policies: Mapping | None, instant: int) -> bool:
    budget = _budget(conn, provider_id, policies, instant)
    return budget is None or budget[1] + float(getattr(
        policies[provider_id], "attempt_reservation")) <= budget[0]


def choose_model(conn: sqlite3.Connection, candidates: Sequence[ModelCandidate],
                 now: datetime | int | float | None = None,
                 policies: Mapping | None = None) -> ModelCandidate | None:
    """Select the first available candidate in configured priority order."""
    conn.execute(_SCHEMA)
    conn.execute(_LEASE_SCHEMA)
    instant = _epoch(now)
    for candidate in candidates:
        row = conn.execute("SELECT retry_at FROM provider_cooldowns "
                           "WHERE provider_id=?", (candidate.provider_id,)).fetchone()
        if (row is None or row[0] <= instant) and _budget_allows(
                conn, candidate.provider_id, policies, instant):
            return candidate
    return None


def available_capacity(conn: sqlite3.Connection, candidates: Sequence[ModelCandidate],
                       now: datetime | int | float | None = None,
                       policies: Mapping | None = None) -> int:
    """Count free account slots before claiming jobs in a controller cycle."""
    conn.execute(_SCHEMA)
    conn.execute(_LEASE_SCHEMA)
    instant = _epoch(now)
    accounts = {candidate.provider_id: candidate.max_parallel for candidate in candidates}
    total = 0
    for provider_id, capacity in accounts.items():
        paused = conn.execute("SELECT retry_at FROM provider_cooldowns WHERE provider_id=?",
                              (provider_id,)).fetchone()
        if paused is not None and paused[0] > instant:
            continue
        used = conn.execute("SELECT count(*) FROM provider_leases WHERE provider_id=? "
                            "AND expires_at>?", (provider_id, instant)).fetchone()[0]
        free = max(0, capacity - used)
        budget = _budget(conn, provider_id, policies, instant)
        if budget is not None:
            reservation = float(getattr(policies[provider_id], "attempt_reservation"))
            remaining = budget[0] - budget[1]
            free = min(free, max(0, math.floor(remaining / reservation)) if
                       reservation and math.isfinite(remaining) else
                       (free if remaining >= 0 else 0))
        total += free
    return total


def claim_model(conn: sqlite3.Connection, candidates: Sequence[ModelCandidate],
                lease_id: str, now: datetime | int | float | None = None,
                ttl_seconds: int = 3600) -> ModelCandidate | None:
    """Compatibility wrapper for the priority-only claim API."""
    return claim_model_decision(conn, candidates, lease_id, now=now,
                                ttl_seconds=ttl_seconds)["chosen"]


def claim_model_decision(conn: sqlite3.Connection, candidates: Sequence[ModelCandidate],
                         lease_id: str, *, policies: Mapping | None = None,
                         route_key: str | None = None,
                         now: datetime | int | float | None = None,
                         ttl_seconds: int = 3600) -> dict:
    """Atomically reserve capacity and estimated cost for one model."""
    if not isinstance(lease_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", lease_id):
        raise ValueError("invalid lease ID")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise ValueError("invalid lease duration")
    if route_key is not None and (type(route_key) is not str or len(route_key) > 128):
        raise ValueError("invalid route key")
    capacities: dict[str, int] = {}
    for candidate in candidates:
        prior = capacities.setdefault(candidate.provider_id, candidate.max_parallel)
        if prior != candidate.max_parallel:
            raise ValueError("provider models must share max_parallel")
    conn.execute(_SCHEMA)
    conn.execute(_LEASE_SCHEMA)
    instant = _epoch(now)
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM provider_leases WHERE expires_at<=?", (instant,))
        if conn.execute("SELECT 1 FROM provider_leases WHERE lease_id=?", (lease_id,)).fetchone():
            raise ValueError("lease ID already active")
        eligible = []
        first_skip = None
        for candidate in candidates:
            paused = conn.execute("SELECT retry_at FROM provider_cooldowns "
                                  "WHERE provider_id=?", (candidate.provider_id,)).fetchone()
            if paused is not None and paused[0] > instant:
                first_skip = first_skip or "cooldown"
                continue
            if not _budget_allows(conn, candidate.provider_id, policies, instant):
                first_skip = first_skip or "budget"
                continue
            active = conn.execute("SELECT count(*) FROM provider_leases "
                                  "WHERE provider_id=?",
                                  (candidate.provider_id,)).fetchone()[0]
            if active >= candidate.max_parallel:
                first_skip = first_skip or "capacity"
                continue
            eligible.append(candidate.ref)
        chosen = next((candidate for candidate in candidates
                       if candidate.ref in eligible), None)
        if chosen is not None:
            reserve = (float(getattr(policies[chosen.provider_id], "attempt_reservation"))
                       if policies and chosen.provider_id in policies else 0.0)
            conn.execute("INSERT INTO provider_leases(lease_id,provider_id,model_id,"
                         "expires_at,route_key,reserved_cost) VALUES(?,?,?,?,?,?)",
                         (lease_id, chosen.provider_id, chosen.model_id,
                          instant + ttl_seconds, route_key, reserve))
            if own_tx:
                conn.commit()
            return {"chosen": chosen, "eligible": tuple(eligible),
                    "reason": first_skip or "priority"}
        if own_tx:
            conn.commit()
        return {"chosen": None, "eligible": (),
                "reason": first_skip or "no_eligible"}
    except BaseException:
        if own_tx:
            conn.rollback()
        raise


def release_model(conn: sqlite3.Connection, lease_id: str) -> None:
    conn.execute(_LEASE_SCHEMA)
    conn.execute("DELETE FROM provider_leases WHERE lease_id=?", (lease_id,))


def record_exhaustion(conn: sqlite3.Connection, candidate: ModelCandidate,
                      retry_at: datetime | int | float, reason: str = "quota",
                      now: datetime | int | float | None = None) -> None:
    """Pause the exhausted provider account; caller commits with job state."""
    if reason != "quota":
        raise ValueError("only confirmed quota exhaustion may trigger fallback")
    retry = _epoch(retry_at)
    instant = _epoch(now)
    if not instant < retry <= instant + 86400:
        raise ValueError("retry_at must be within the next 24 hours")
    conn.execute(_SCHEMA)
    conn.execute("INSERT INTO provider_cooldowns(provider_id,retry_at,reason) "
                 "VALUES(?,?,?) ON CONFLICT(provider_id) DO UPDATE SET "
                 "retry_at=max(provider_cooldowns.retry_at,excluded.retry_at),reason=excluded.reason",
                 (candidate.provider_id, retry, reason))


Failure = Literal["quota", "auth", "other"]


def classify_failure(stdout: str, stderr: str, returncode: int) -> Failure:
    """Recognize provider HTTP status; never persist or print provider output."""
    if returncode == 0:
        return "other"
    if returncode == 75:
        return "quota"
    if returncode == 77:
        return "auth"
    # OpenCode emits newline-delimited JSON events. Ignore assistant text events:
    # a task may contain the string '429' without any provider failure.
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") not in ("error", "step_error"):
            continue
        payload = event.get("error") or event.get("data") or event.get("properties") or {}
        if not isinstance(payload, dict):
            continue
        for _ in range(4):
            status = payload.get("status") or payload.get("statusCode")
            if str(status) == "429":
                return "quota"
            if str(status) in ("401", "403"):
                return "auth"
            nested = payload.get("error") or payload.get("data")
            if not isinstance(nested, dict):
                break
            payload = nested
    # CLI stderr may carry an HTTP status when JSON events could not be emitted.
    status_match = re.search(r"(?:HTTP(?:/\d(?:\.\d)?)?\s*|status(?:Code)?\s*[:=]\s*|request failed with status(?:Code)?\s+)(429|401|403)\b",
                             stderr, flags=re.IGNORECASE)
    if status_match:
        return "quota" if status_match.group(1) == "429" else "auth"
    return "other"


def opencode_command(candidate: ModelCandidate, prompt: str) -> tuple[str, ...]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be nonempty")
    return (("opencode", "run", "--model", candidate.ref)
            + (("--variant", candidate.effort) if candidate.effort else ())
            + ("--agent", "build", "--format", "json", "--", prompt))
