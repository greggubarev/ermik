"""Bounded read-only maintainer projection over remote queue and review packets."""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re

from .github import GitHub
from .review_packet import build_review_packet


MAX_ITEMS = 100
MAX_BYTES = 128 * 1024
_BRANCH = re.compile(r"work/issue-([1-9][0-9]*)-[a-z0-9-]+\Z")
_ROUTE = re.compile(r"route:[a-z][a-z0-9_-]{0,39}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_STATES = {"current", "missing", "stale", "duplicate", "incomplete",
           "unsupported_version"}


def _action(packet: dict) -> str:
    if packet.get("head_verified") is not True:
        return "reconcile_remote_identity_manually"
    if packet.get("status") != "current":
        return "reconcile_evidence_then_review"
    if packet.get("review", {}).get("status") != "current":
        return "review_current_head"
    return "decide_on_reviewed_head_manually"


def queue_snapshot(cfg) -> dict:
    """Read live GitHub state; no controller runtime or remote check is inferred."""
    gh = GitHub(cfg.repo)
    ready = gh.ready_issues()
    pulls = gh._pages("pulls?state=open")
    if len(ready) + len(pulls) > MAX_ITEMS:
        raise ValueError("maintainer queue exceeds display limit")
    rows = []
    represented = set()
    for pr in pulls:
        number = pr.get("number")
        branch = (pr.get("head") or {}).get("ref")
        match = _BRANCH.fullmatch(branch) if isinstance(branch, str) else None
        if type(number) is not int or number <= 0 or not match:
            continue
        issue = int(match.group(1))
        packet = build_review_packet(cfg, number)
        # A redacted or changed identity must never inherit a route or action
        # from the unverified PR branch.
        verified = packet.get("head_verified") is True and packet.get("issue") == issue
        if verified:
            represented.add(issue)
        remote_state = "unknown"
        route = "unknown"
        if verified:
            issue_record = gh.issue(issue)
            labels = {row.get("name") for row in issue_record.get("labels", [])
                      if isinstance(row, dict)}
            if "state:in-progress" in labels:
                remote_state = "in_progress"
            elif "state:blocked" in labels:
                remote_state = "blocked"
            routes = {label for label in labels if isinstance(label, str)
                      and _ROUTE.fullmatch(label)}
            if len(routes) == 1:
                route = next(iter(routes))
        evidence = packet.get("evidence") or {}
        review = packet.get("review") or {}
        rows.append({"issue": issue if verified else None, "pr": number,
                     "head": packet.get("head") if verified and _SHA.fullmatch(
                         packet.get("head") or "") else None,
                     "remote_label_state": remote_state,
                     "controller_runtime": "unverified",
                     "model_route_label": route,
                     "publisher_model_claim": evidence.get("model_ref") if verified and
                     packet.get("status") == "current" and
                     isinstance(evidence.get("model_ref"), str) and
                     re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}",
                                  evidence["model_ref"]) else None,
                     "evidence": evidence.get("status") if verified and
                     evidence.get("status") in _STATES else "unknown",
                     "checks": "publisher_claim_passed" if verified and
                     packet.get("status") == "current" and
                     evidence.get("observation") == "publisher_claim" and
                     evidence.get("checks") and all(
                         check.get("result") == "passed" for check in evidence["checks"])
                     else "unverified",
                     "review": review.get("status") if verified and
                     review.get("status") in {"current", "missing", "stale", "invalid"}
                     else "unknown",
                     "next_action": _action(packet) if verified else
                     "reconcile_remote_identity_manually"})
    for issue in ready:
        if issue not in represented:
            rows.append({"issue": issue, "pr": None, "head": None,
                         "remote_label_state": "ready", "controller_runtime": "unverified",
                         "model_route_label": "unknown", "evidence": "missing",
                         "publisher_model_claim": None,
                         "checks": "unverified", "review": "missing",
                         "next_action": "wait_for_controller_or_reconcile_manually"})
    return {"repo": cfg.repo, "items": sorted(rows, key=lambda row:
            (row["issue"] or 0, row["pr"] or 0))}


def render_html(snapshot: dict) -> bytes:
    def cell(value: object) -> str:
        return escape(str(value if value is not None else "—"), quote=True)
    columns = ("issue", "pr", "head", "remote_label_state", "controller_runtime",
               "model_route_label", "publisher_model_claim", "evidence", "checks",
               "review", "next_action")
    rows = "".join("<tr>" + "".join(f"<td>{cell(row[key])}</td>" for key in columns)
                   + "</tr>" for row in snapshot["items"])
    heads = "".join(f"<th>{cell(key.replace('_', ' ').title())}</th>" for key in columns)
    data = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"refresh\" content=\"30\"><title>Maintainer queue</title>"
            "<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse}"
            "td,th{padding:.5rem;border:1px solid #bbb;text-align:left}"
            ".scroll{overflow:auto}</style></head><body><h1>Maintainer queue</h1>"
            "<p>Controller runtime is unverified on this host. Checks are publisher claims "
            "until independently reviewed.</p><div class=\"scroll\"><table><thead><tr>"
            + heads + "</tr></thead><tbody>" + rows + "</tbody></table></div></body></html>")
    payload = data.encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ValueError("maintainer view exceeds response limit")
    return payload


def create_server(cfg, port: int = 8766) -> HTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_error(405, "read-only endpoint")

        do_PUT = do_PATCH = do_DELETE = do_POST

        def do_GET(self):
            if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d+)?",
                                self.headers.get("Host", "")):
                self.send_error(400, "invalid host")
                return
            if self.path not in ("/", "/api/queue"):
                self.send_error(404)
                return
            try:
                snapshot = queue_snapshot(cfg)
                payload = render_html(snapshot) if self.path == "/" else json.dumps(
                    snapshot, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                if len(payload) > MAX_BYTES:
                    raise ValueError("maintainer view exceeds response limit")
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                self.send_error(503, "queue unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/"
                             else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    return HTTPServer(("127.0.0.1", port), Handler)
