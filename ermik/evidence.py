"""Bounded, canonical PR handoff record.

The embedded digest detects accidental changes to the serialized record. It is
not a signature or an attestation of the controller, checks, or remote state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from .policy import _safe_path


VERSION = 2
MAX_BYTES = 16384
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
_BRANCH = re.compile(r"work/issue-([1-9][0-9]*)-[a-z0-9-]+\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_V1_FIELDS = {"version", "repo", "head_repository", "issue", "pr", "branch",
              "base", "head", "body_sha256", "scope_sha256", "checks_sha256",
              "paths", "checks", "image", "model", "attempts", "usage", "unknowns"}
_V2_FIELDS = _V1_FIELDS | {"image_config_digest"}
_V3_FIELDS = _V2_FIELDS | {"complexity", "profile_sha256", "effort"}
_TOKENS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")


class UnsupportedManifestVersion(ValueError):
    """The manifest declares a version this reader does not support."""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _valid_path(path: object) -> bool:
    if type(path) is not str:
        return False
    try:
        return len(path.encode("utf-8")) <= 512 and _safe_path(path) == path
    except (UnicodeError, ValueError):
        return False


def validate_manifest(record: dict) -> dict:
    """Validate an exact v1 or v2 record without changing its version."""
    _require(type(record) is dict and "version" in record, "invalid manifest fields")
    if type(record["version"]) is not int or record["version"] not in (1, 2, 3):
        raise UnsupportedManifestVersion("unsupported manifest version")
    fields = (_V1_FIELDS if record["version"] == 1 else
              _V2_FIELDS if record["version"] == 2 else _V3_FIELDS)
    _require(set(record) == fields | {"sha256"}, "invalid manifest fields")
    for key in ("repo", "head_repository"):
        _require(type(record[key]) is str and bool(_REPO.fullmatch(record[key]))
                 and record[key].split("/", 1)[1] not in {".", ".."},
                 f"invalid {key}")
    for key in ("issue", "pr"):
        _require(type(record[key]) is int and 1 <= record[key] <= 2**31 - 1,
                 f"invalid {key}")
    branch = record["branch"]
    match = _BRANCH.fullmatch(branch) if type(branch) is str else None
    _require(match is not None and int(match.group(1)) == record["issue"],
             "invalid branch")
    for key in ("base", "head"):
        _require(type(record[key]) is str and bool(_SHA.fullmatch(record[key])),
                 f"invalid {key}")
    _require(record["base"] != record["head"], "base and head must differ")
    for key in ("body_sha256", "scope_sha256", "checks_sha256"):
        _require(type(record[key]) is str and bool(_DIGEST.fullmatch(record[key])),
                 f"invalid {key}")
    paths = record["paths"]
    _require(type(paths) is list and 1 <= len(paths) <= 100, "invalid paths")
    _require(all(_valid_path(path) for path in paths), "invalid path")
    _require(paths == sorted(set(paths)), "paths must be sorted and unique")
    checks = record["checks"]
    _require(type(checks) is list and 1 <= len(checks) <= 32, "invalid checks")
    for index, check in enumerate(checks, 1):
        _require(type(check) is dict and set(check) == {"index", "result", "log_sha256"}
                 and type(check["index"]) is int and check["index"] == index
                 and check["result"] == "passed"
                 and type(check["log_sha256"]) is str
                 and bool(_DIGEST.fullmatch(check["log_sha256"])), "invalid check")
    _require(type(record["image"]) is str
             and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", record["image"])),
             "invalid image identity")
    if record["version"] >= 2:
        _require(type(record["image_config_digest"]) is str
                 and bool(re.fullmatch(r"sha256:[0-9a-f]{64}",
                                       record["image_config_digest"])),
                 "invalid image config digest")
    unknowns = record["unknowns"]
    _require(type(unknowns) is list and all(type(item) is str for item in unknowns)
             and unknowns == sorted(set(unknowns))
             and set(unknowns) <= ({"model", "usage", "effort"} if
                                   record["version"] == 3 else {"model", "usage"}),
             "invalid unknowns")
    model = record["model"]
    _require((model is None and "model" in unknowns)
             or (type(model) is str and bool(_MODEL.fullmatch(model))
                 and "model" not in unknowns), "invalid model observation")
    if record["version"] == 3:
        _require(record["complexity"] in ("easy", "medium", "hard"),
                 "invalid approved complexity")
        _require(type(record["profile_sha256"]) is str and
                 bool(_DIGEST.fullmatch(record["profile_sha256"])),
                 "invalid complexity profile digest")
        _require((record["effort"] in ("low", "medium", "high")
                  and "effort" not in unknowns and model is not None)
                 or (record["effort"] is None and "effort" in unknowns
                     and model is None), "invalid effort observation")
    _require(type(record["attempts"]) is int and 1 <= record["attempts"] <= 10000,
             "invalid attempts")
    usage = record["usage"]
    _require(type(usage) is dict, "invalid usage")
    if "usage" in unknowns:
        _require(usage == {"observation": "unknown"}, "invalid unknown usage")
    else:
        _require(set(usage) == {"observation", *_TOKENS, "estimated_cost"}
                 and usage["observation"] == "measured", "invalid usage fields")
        _require(all(type(usage[key]) is int and 0 <= usage[key] <= 10**12
                     for key in _TOKENS), "invalid token usage")
        cost = usage["estimated_cost"]
        _require(type(cost) in (int, float) and math.isfinite(cost)
                 and 0 <= cost <= 10**9, "invalid usage cost")
    digest = record["sha256"]
    _require(type(digest) is str and bool(_DIGEST.fullmatch(digest)), "invalid digest")
    payload = {key: record[key] for key in fields}
    _require(hashlib.sha256(_canonical(payload)).hexdigest() == digest,
             "manifest digest mismatch")
    raw = _canonical(record)
    _require(len(raw) <= MAX_BYTES, "manifest too large")
    return json.loads(raw)


def make_manifest(**fields: object) -> dict:
    """Create a validated v2 or approved-complexity v3 record."""
    version = 3 if "complexity" in fields else VERSION
    _require(set(fields) == (_V3_FIELDS if version == 3 else _V2_FIELDS) - {"version"},
             "invalid manifest fields")
    payload = {"version": version, **fields}
    try:
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid manifest data") from exc
    return validate_manifest({**payload, "sha256": digest})


def dumps_manifest(record: dict) -> bytes:
    """Serialize only a validated record, as canonical UTF-8 JSON with newline."""
    return _canonical(validate_manifest(record)) + b"\n"


def loads_manifest(data: bytes) -> dict:
    """Load strict UTF-8 JSON; reject duplicate keys and noncanonical encodings."""
    _require(type(data) is bytes and len(data) <= MAX_BYTES + 1,
             "invalid manifest size")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate manifest key")
            result[key] = value
        return result

    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid manifest JSON") from exc
    checked = validate_manifest(record)
    _require(data == _canonical(checked) + b"\n", "noncanonical manifest encoding")
    return checked
