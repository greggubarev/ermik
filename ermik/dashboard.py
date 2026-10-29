"""Read-only local dashboard over the bounded controller status snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import re
from typing import Callable

from .observability import status_snapshot


MAX_JOBS = 200
MAX_EVENTS = 40
MAX_QUEUE = 100
MAX_RESPONSE_BYTES = 128 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
_TIME = re.compile(r"[0-9T: .+Z-]{1,40}\Z")
_STATUSES = {"running", "correcting", "retry_wait", "awaiting_review", "blocked", "merged", "unknown"}
_EVENTS = {"admitted", "claimed", "retry_scheduled", "retry_started", "model_selected",
           "quota_paused", "checks_passed", "pr_published", "awaiting_review",
           "blocked", "correction_started", "merged"}


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _safe_time(value: object) -> str | None:
    if type(value) is int and 0 <= value <= 4102444800:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return value if isinstance(value, str) and _TIME.fullmatch(value) else None


def public_snapshot(source: dict) -> dict:
    """Project only approved scalar fields; cap every user-visible collection."""
    queue = source.get("queue") or {}
    slots = source.get("slots") or {}
    raw_events = source.get("events") or []
    events = []
    model_by_issue: dict[int, str] = {}
    for row in raw_events[:MAX_EVENTS]:
        if not isinstance(row, dict):
            continue
        issue = _positive_int(row.get("issue"))
        kind = row.get("kind")
        if issue is None or not isinstance(kind, str) or kind not in _EVENTS:
            continue
        model = _safe_id(row.get("model"))
        if kind == "model_selected" and model and issue not in model_by_issue:
            model_by_issue[issue] = model
        events.append({"issue": issue, "kind": kind, "model": model,
                       "at": _safe_time(row.get("created_at"))})
    jobs = []
    for row in (source.get("jobs") or [])[:MAX_JOBS]:
        if not isinstance(row, dict):
            continue
        issue = _positive_int(row.get("issue"))
        if issue is None:
            continue
        status = row.get("status")
        last_model = _safe_id(row.get("last_model")) or model_by_issue.get(issue)
        result_model = _safe_id(row.get("result_model"))
        jobs.append({"issue": issue,
                     "pr": _positive_int(row.get("pr")),
                     "status": status if isinstance(status, str) and status in _STATUSES else "unknown",
                     "model": last_model,
                     "result_model": result_model,
                     "attempts": _positive_int(row.get("attempts")),
                     "next_retry_at": _safe_time(row.get("next_retry_at")),
                     "next_review_at": _safe_time(row.get("next_review_at"))})
    cooldowns = []
    for row in (source.get("provider_cooldown") or [])[:32]:
        if isinstance(row, dict):
            provider = _safe_id(row.get("provider"))
            if provider:
                cooldowns.append({"provider": provider,
                                  "retry_at": _safe_time(row.get("retry_at"))})
    raw_ready = queue.get("ready_issues")
    ready = None if raw_ready is None else [n for n in raw_ready[:MAX_QUEUE]
                                             if _positive_int(n) is not None]
    counts = source.get("counts") or {}
    usage = []
    for row in (source.get("usage") or [])[:32]:
        if not isinstance(row, dict):
            continue
        provider, model = _safe_id(row.get("provider")), _safe_id(row.get("model"))
        if not provider or not model:
            continue
        metrics = {key: value if type(value) is int and value >= 0 else None
                   for key, value in ((key, row.get(key)) for key in
                                      ("attempts", "measured_attempts", "unknown_attempts",
                                       "input_tokens", "output_tokens", "reasoning_tokens",
                                       "cache_read_tokens", "cache_write_tokens", "total_tokens"))}
        cost = row.get("estimated_cost")
        metrics["estimated_cost"] = (round(cost, 4) if type(cost) in (int, float)
                                      and math.isfinite(cost) and cost >= 0 else None)
        usage.append({"provider": provider, "model": model, **metrics})
    return {
        "observed_at": _safe_time(source.get("observed_at")),
        "queue": {"count": queue.get("count") if type(queue.get("count")) is int else None,
                  "ready_issues": ready,
                  "truncated": raw_ready is not None and len(raw_ready) > MAX_QUEUE},
        "slots": {key: value if type(value) is int and value >= 0 else None
                  for key, value in ((key, slots.get(key)) for key in
                                     ("capacity", "active", "available"))},
        "counts": {key: value for key, value in counts.items()
                   if key in _STATUSES and type(value) is int and value >= 0},
        "jobs": jobs, "jobs_truncated": bool(source.get("jobs_truncated")) or
                len(source.get("jobs") or []) > MAX_JOBS,
        "provider_cooldown": cooldowns, "events": events, "usage": usage,
    }


def render_html(snapshot: dict) -> bytes:
    """Render escaped static HTML with no script, external assets, or raw logs."""
    e = lambda value: escape(str(value if value is not None else "—"), quote=True)
    queue = snapshot["queue"]
    slots = snapshot["slots"]
    job_rows = "".join(
        f"<tr><td>#{e(row['issue'])}</td><td>{e(row['status'])}</td>"
        f"<td>{e(row['model'])}</td><td>{e(row['result_model'])}</td><td>{e(row['attempts'])}</td>"
        f"<td>{e(row['next_retry_at'])}</td><td>{e(row['next_review_at'])}</td>"
        f"<td>{e(row['pr'])}</td></tr>"
        for row in snapshot["jobs"])
    event_rows = "".join(
        f"<tr><td>{e(row['at'])}</td><td>#{e(row['issue'])}</td>"
        f"<td>{e(row['kind'])}</td><td>{e(row['model'])}</td></tr>"
        for row in snapshot["events"])
    cooldown_rows = "".join(
        f"<li>{e(row['provider'])}: {e(row['retry_at'])}</li>"
        for row in snapshot["provider_cooldown"])
    usage_rows = "".join(
        f"<tr><td>{e(row['provider'])}/{e(row['model'])}</td><td>{e(row['attempts'])}</td>"
        f"<td>{e(row['measured_attempts'])}</td><td>{e(row['unknown_attempts'])}</td>"
        f"<td>{e(row['input_tokens'])}</td><td>{e(row['output_tokens'])}</td>"
        f"<td>{e(row['cache_read_tokens'])}</td><td>{e(row['total_tokens'])}</td>"
        f"<td>{e(row['estimated_cost'])}</td></tr>" for row in snapshot["usage"])
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Task status</title><style>
body{{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#172433;background:#f5f7fa}}
h1,h2{{margin:.7rem 0}}section{{background:white;padding:1rem;margin:1rem 0;border:1px solid #d8e0e8;border-radius:8px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:.5rem;border-bottom:1px solid #e3e8ee}}
small{{color:#526374}}.scroll{{overflow:auto}}
</style></head><body><h1>Task status</h1><small>Updated: {e(snapshot['observed_at'])}</small>
<section><h2>Queue and slots</h2><p>Ready: {e(queue['count'])} · Active: {e(slots['active'])} / {e(slots['capacity'])} · Available: {e(slots['available'])}</p>
<p>Queued issues: {e(', '.join('#' + str(n) for n in (queue['ready_issues'] or [])) if queue['ready_issues'] is not None else None)}{' …' if queue['truncated'] else ''}</p></section>
<section><h2>Jobs</h2><div class="scroll"><table><thead><tr><th>Issue</th><th>Status</th><th>Last model</th><th>PR model</th><th>Attempts</th><th>Next retry</th><th>Next PR check</th><th>PR</th></tr></thead><tbody>{job_rows}</tbody></table></div>{'<small>List truncated.</small>' if snapshot['jobs_truncated'] else ''}</section>
<section><h2>Provider cooldowns</h2><ul>{cooldown_rows or '<li>None</li>'}</ul></section>
<section><h2>Usage by model</h2><p><small>Cost is an estimate, not a subscription balance. Attempts without usage data are counted separately.</small></p><div class="scroll"><table><thead><tr><th>Provider/model</th><th>Attempts</th><th>Measured</th><th>Unknown</th><th>Input</th><th>Output</th><th>Cache read</th><th>Total</th><th>Est. $</th></tr></thead><tbody>{usage_rows}</tbody></table></div></section>
<section><h2>Recent events</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Issue</th><th>Event</th><th>Model</th></tr></thead><tbody>{event_rows}</tbody></table></div></section>
</body></html>"""
    data = html.encode("utf-8")
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("dashboard response exceeds limit")
    return data


def create_server(state_dir: Path, *, max_parallel: int, port: int = 8765,
                  ready_issues: Callable[[], list[int]] | None = None) -> HTTPServer:
    """Build a read-only HTTP server bound strictly to IPv4 loopback."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_error(405, "read-only endpoint")

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def do_GET(self) -> None:
            if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d+)?", self.headers.get("Host", "")):
                self.send_error(400, "invalid host")
                return
            if self.path not in ("/", "/api/status"):
                self.send_error(404)
                return
            try:
                ready = ready_issues() if ready_issues is not None else None
                snapshot = public_snapshot(status_snapshot(
                    state_dir, max_parallel=max_parallel, ready_issues=ready))
                if self.path == "/":
                    payload = render_html(snapshot)
                    content_type = "text/html; charset=utf-8"
                else:
                    payload = json.dumps(snapshot, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise ValueError("dashboard response exceeds limit")
                    content_type = "application/json; charset=utf-8"
            except (OSError, ValueError, TypeError, KeyError):
                self.send_error(503, "status unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            # Avoid logging request paths, which may contain untrusted text.
            pass

    return HTTPServer(("127.0.0.1", port), Handler)
