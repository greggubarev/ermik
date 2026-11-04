"""Best-effort delivery of exact-HEAD evidence for published jobs."""

from __future__ import annotations

from . import evidence_transport, state
from .config import Config
from .evidence_store import read_evidence, write_evidence


MAX_DELIVERIES = 32
_CURSOR_FILE = "delivery-cursor.json"


def _cursor(cfg: Config) -> int:
    try:
        record = read_evidence(cfg.state_dir / _CURSOR_FILE)
    except FileNotFoundError:
        return 0
    if (set(record) != {"issue"} or type(record["issue"]) is not int
            or record["issue"] < 0):
        raise ValueError("invalid evidence delivery cursor")
    return record["issue"]


def deliver_pending(cfg: Config, limit: int = 8) -> list[dict]:
    """Deliver bounded pending records without holding the state lock on GitHub I/O."""
    if type(limit) is not int or not 1 <= limit <= MAX_DELIVERIES:
        raise ValueError("delivery limit must be 1..32")
    if cfg.evidence_publisher is None:
        return []
    with state.exclusive(cfg.state_dir):
        with state.connect(cfg.state_dir) as conn:
            cursor = _cursor(cfg)
            query = (
                "SELECT j.issue,j.pr,j.head_sha,j.base_sha FROM jobs AS j "
                "WHERE j.status='awaiting_review' AND j.pr IS NOT NULL "
                "AND j.head_sha IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM events AS e WHERE e.issue=j.issue "
                "AND e.kind='evidence_delivered' AND e.head_sha=j.head_sha) ")
            pending = [tuple(row) for row in conn.execute(
                query + "AND j.issue>? ORDER BY j.issue LIMIT ?",
                (cursor, limit)).fetchall()]
            if len(pending) < limit:
                pending.extend(tuple(row) for row in conn.execute(
                    query + "AND j.issue<=? ORDER BY j.issue LIMIT ?",
                    (cursor, limit - len(pending))).fetchall())
            if pending:
                # Advance before network I/O so a persistent failure cannot
                # monopolize the next process's bounded delivery budget.
                write_evidence(cfg.state_dir / _CURSOR_FILE,
                               {"issue": pending[-1][0]}, max_bytes=128)
    results: list[dict] = []
    for issue, pr, head, base in pending:
        try:
            delivered = evidence_transport.publish_manifest(cfg, pr)
            if (not isinstance(delivered, dict)
                    or delivered.get("status") not in ("published", "existing")
                    or delivered.get("head") != head):
                raise ValueError("evidence delivery did not confirm the selected HEAD")
        except Exception:
            # Publication errors are retried by the next pass; they must not
            # change the job's review state or disclose remote response text.
            results.append({"issue": issue, "pr": pr, "head": head,
                            "status": "failed"})
            continue
        with state.exclusive(cfg.state_dir):
            with state.connect(cfg.state_dir) as conn:
                current = state.job(conn, issue)
                if (current is None or current["status"] != "awaiting_review"
                        or current["pr"] != pr or current["head_sha"] != head
                        or current["base_sha"] != base):
                    status = "superseded"
                else:
                    already = conn.execute(
                        "SELECT 1 FROM events WHERE issue=? AND kind='evidence_delivered' "
                        "AND head_sha=? LIMIT 1", (issue, head)).fetchone()
                    if already:
                        status = "existing"
                    else:
                        state.event(conn, issue, "evidence_delivered", head_sha=head)
                        status = "delivered"
        results.append({"issue": issue, "pr": pr, "head": head, "status": status})
    return results
