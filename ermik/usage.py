"""Strict projection of disposable worker usage summaries."""

from __future__ import annotations

import json
import math


FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")
MAX_TOKENS = 10**12


def parse_worker_usage(output: bytes) -> dict | None:
    """Read only the final, bounded usage marker; ignore arbitrary worker text."""
    found = None
    for line in output.splitlines():
        if not line.startswith(b"WORKER_USAGE ") or len(line) > 1024:
            continue
        try:
            value = json.loads(line[len(b"WORKER_USAGE "):])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or type(value.get("complete")) is not bool:
            continue
        if not value["complete"]:
            found = {"complete": False, **{field: None for field in FIELDS},
                     "estimated_cost": None}
            continue
        numbers = {field: value.get(field) for field in FIELDS}
        cost = value.get("estimated_cost")
        if (any(type(number) is not int or not 0 <= number <= MAX_TOKENS
                for number in numbers.values())
                or type(cost) not in (int, float) or not math.isfinite(cost)
                or not 0 <= cost <= 10**9):
            continue
        found = {"complete": True, **numbers, "estimated_cost": float(cost)}
    return found
