"""Optional, bounded outbound workflow digest. Run separately from workflow commands."""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .decision_trail import read_decisions
from .github import GitHub


_KINDS = {"pr_published": "draft_pr", "blocked": "blocked",
          "merged": "completed"}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_BRANCH = re.compile(r"work/issue-(\d+)-[a-z0-9-]+\Z")
_CLOSES = re.compile(r"(?im)\bCloses\s+#(\d+)\b")
_JOURNAL = re.compile(r"pr-[1-9][0-9]{0,11}\.json\Z")
_STATE_NAME = "event-digest.json"
_MAX_SEEN = 2048
_MAX_JOURNALS = 10000
_MAX_STATE_BYTES = 1048576
_INTERVAL = 60


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _settings() -> tuple[str, str] | None:
    url, token = os.environ.get("ERMIK_DIGEST_URL"), os.environ.get("ERMIK_DIGEST_TOKEN")
    if not url and not token:
        return None
    if not url or not token or len(token) > 4096 or any(c in token for c in "\r\n"):
        raise ValueError("digest URL and token must both be set in environment")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or len(url) > 2048):
        raise ValueError("digest URL must be an HTTPS endpoint without userinfo or fragment")
    return url, token


def _read(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("digest state cannot be a symlink")
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            raise ValueError("digest state is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 2, "cursor": 0, "seen": [], "next_at": 0,
                "failures": 0, "decision_cursor": "", "decision_positions": {}}
    if type(data) is dict and data.get("version") == 1:
        data = {**data, "version": 2, "decision_cursor": "",
                "decision_positions": {}}
    if (type(data) is not dict or set(data) !=
            {"version", "cursor", "seen", "next_at", "failures",
             "decision_cursor", "decision_positions"}
            or data["version"] != 2 or type(data["cursor"]) is not int
            or data["cursor"] < 0 or type(data["next_at"]) is not int
            or data["next_at"] < 0 or type(data["failures"]) is not int
            or not 0 <= data["failures"] <= 20 or type(data["seen"]) is not list
            or len(data["seen"]) > _MAX_SEEN
            or any(type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}", key)
                   for key in data["seen"])
            or type(data["decision_cursor"]) is not str
            or (data["decision_cursor"] and not _JOURNAL.fullmatch(
                data["decision_cursor"]))
            or type(data["decision_positions"]) is not dict
            or len(data["decision_positions"]) > _MAX_JOURNALS
            or any(not _JOURNAL.fullmatch(name)
                   or type(position) is not int or not 0 <= position <= 128
                   for name, position in data["decision_positions"].items())):
        raise ValueError("digest state is malformed")
    return data


def _save(path: Path, data: dict) -> None:
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > _MAX_STATE_BYTES:
        raise ValueError("digest state is too large")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(root: Path):
    path = root / "event-digest.lock"
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _key(repo: str, kind: str, issue: int, pr: int | None, head: str | None,
         source: str | None = None) -> str:
    fields = [repo.lower(), kind, issue, pr, head]
    if source is not None:
        fields.append(source)
    value = json.dumps(fields, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _event_candidate(cfg, cursor: int) -> tuple[int, dict | None]:
    db = cfg.state_dir / "state.sqlite3"
    if not db.exists():
        return cursor, None
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT e.id,e.issue,e.kind,e.head_sha,j.pr,j.branch, "
                            "(SELECT MIN(e0.id) FROM events e0 WHERE e0.issue=e.issue "
                            "AND e0.kind='pr_published') AS first_pub "
                            "FROM events e LEFT JOIN jobs j ON j.issue=e.issue "
                            "WHERE e.id>? ORDER BY e.id LIMIT 100", (cursor,)).fetchall()
    for row in rows:
        cursor = row["id"]
        kind = _KINDS.get(row["kind"])
        if kind is None:
            continue
        if kind == "draft_pr" and row["id"] != row["first_pub"]:
            continue
        issue, pr, head = row["issue"], row["pr"], row["head_sha"]
        if type(issue) is not int or issue <= 0:
            continue
        if pr is not None and (type(pr) is not int or pr <= 0
                               or not isinstance(row["branch"], str)
                               or not _BRANCH.fullmatch(row["branch"])
                               or int(_BRANCH.fullmatch(row["branch"]).group(1)) != issue):
            continue
        if kind == "draft_pr" and (pr is None or not isinstance(head, str)
                                    or not _SHA.fullmatch(head)):
            continue
        if kind == "completed" and pr is None:
            continue
        return cursor, {"kind": kind, "issue": issue, "pr": pr,
                        "head": head if isinstance(head, str) and _SHA.fullmatch(head) else None,
                        "branch": row["branch"], "source": f"event:{row['id']}"}
    return cursor, None


def _decision_candidate(cfg, data: dict) -> dict | None:
    root = cfg.state_dir / "decision-trail"
    if not root.is_dir():
        data["decision_cursor"] = ""
        data["decision_positions"] = {}
        return None
    paths = sorted(path for path in root.glob("pr-*.json")
                   if _JOURNAL.fullmatch(path.name))
    if len(paths) > _MAX_JOURNALS:
        raise ValueError("digest decision journal limit reached")
    names = {path.name for path in paths}
    data["decision_positions"] = {name: position for name, position
                                  in data["decision_positions"].items() if name in names}
    if not paths:
        data["decision_cursor"] = ""
        return None
    start = next((i + 1 for i, path in enumerate(paths)
                  if path.name == data["decision_cursor"]), 0) % len(paths)
    for offset in range(min(100, len(paths))):
        index = (start + offset) % len(paths)
        path = paths[index]
        try:
            journal = read_decisions(cfg, int(path.stem[3:]))
        except (ValueError, OSError):
            data["decision_cursor"] = path.name
            continue
        position = data["decision_positions"].get(path.name, 0)
        for entry_index in range(position, len(journal["entries"])):
            entry = journal["entries"][entry_index]
            if entry["decision"] not in ("changes_requested", "merged"):
                data["decision_positions"][path.name] = entry_index + 1
                continue
            kind = ("correction_requested" if entry["decision"] == "changes_requested"
                    else "completed")
            source = f"decision:{entry['pr']}:{entry_index}"
            key = _key(cfg.repo, kind, entry["issue"], entry["pr"], entry["head"], source)
            legacy = _key(cfg.repo, kind, entry["issue"], entry["pr"], entry["head"])
            if key in data["seen"] or legacy in data["seen"]:
                data["decision_positions"][path.name] = entry_index + 1
                continue
            data["decision_cursor"] = paths[(index - 1) % len(paths)].name
            return {"kind": kind, "issue": entry["issue"], "pr": entry["pr"],
                    "head": entry["head"], "branch": None, "source": source,
                    "journal": path.name, "position": entry_index + 1}
        data["decision_positions"][path.name] = len(journal["entries"])
        data["decision_cursor"] = path.name
    return None


def _ack(data: dict, cursor: int, item: dict) -> None:
    data["cursor"] = cursor
    if "journal" in item:
        data["decision_positions"][item["journal"]] = item["position"]


def _verified(cfg, gh: GitHub, item: dict) -> bool:
    issue = gh.issue(item["issue"], allow_closed=True)
    if issue.get("number") != item["issue"]:
        return False
    if item["pr"] is None:
        return True
    pr = gh.pr(item["pr"])
    branch = pr.get("headRefName")
    match = _BRANCH.fullmatch(branch or "")
    return bool(pr.get("number") == item["pr"] and match
                and int(match.group(1)) == item["issue"]
                and pr.get("headRepository", {}).get("nameWithOwner", "").casefold()
                == (cfg.publish_repo or cfg.repo).casefold()
                and pr.get("baseRefName") == cfg.base_branch
                and _CLOSES.findall(pr.get("body") or "") == [str(item["issue"])])


def _send(url: str, token: str, payload: dict, key: str) -> int | None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")
    request = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token,
        "Idempotency-Key": key})
    try:
        with build_opener(_NoRedirect).open(request, timeout=5) as response:
            return None if 200 <= response.status < 300 else 60
    except HTTPError as exc:
        try:
            if exc.code == 429:
                try:
                    return max(60, min(3600, int(exc.headers.get("Retry-After", "60"))))
                except ValueError:
                    return 60
            return 60
        finally:
            exc.close()
    except (OSError, URLError):
        return 60


def deliver_once(cfg, *, gh: GitHub | None = None, now: int | None = None) -> dict:
    """Deliver at most one event; failures never alter workflow state."""
    settings = _settings()
    if settings is None:
        return {"status": "disabled"}
    if not cfg.state_dir.is_dir() or cfg.state_dir.is_symlink():
        return {"status": "unavailable"}
    try:
        with _lock(cfg.state_dir):
            return _deliver_locked(cfg, settings, gh, now)
    except (BlockingIOError, sqlite3.Error):
        return {"status": "unavailable"}


def _deliver_locked(cfg, settings, gh, now) -> dict:
    now = int(time.time()) if now is None else now
    path = cfg.state_dir / _STATE_NAME
    data = _read(path)
    if now < data["next_at"]:
        return {"status": "rate_limited"}
    cursor, item = _event_candidate(cfg, data["cursor"])
    if item is None:
        data["cursor"] = cursor
        item = _decision_candidate(cfg, data)
    if item is None:
        _save(path, data)
        return {"status": "empty"}
    key = _key(cfg.repo, item["kind"], item["issue"], item["pr"], item["head"],
               item["source"])
    if key in data["seen"]:
        _ack(data, cursor, item)
        _save(path, data)
        return {"status": "duplicate"}
    try:
        verified = _verified(cfg, gh or GitHub(cfg.repo, cfg.publish_repo,
                                              cfg.publish_token_env), item)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {"status": "identity_unverified"}
    if not verified:
        _ack(data, cursor, item)
        data["seen"] = (data["seen"] + [key])[-_MAX_SEEN:]
        _save(path, data)
        return {"status": "identity_rejected"}
    payload = {"version": 1, "event": item["kind"], "issue": item["issue"],
               "issue_url": f"https://github.com/{cfg.repo}/issues/{item['issue']}",
               "pr": item["pr"],
               "pr_url": (f"https://github.com/{cfg.repo}/pull/{item['pr']}"
                          if item["pr"] else None)}
    retry = _send(*settings, payload, key)
    if retry is not None:
        data["failures"] = min(20, data["failures"] + 1)
        data["next_at"] = now + max(retry, min(3600, 60 * 2 ** (data["failures"] - 1)))
        _save(path, data)
        return {"status": "retry_wait"}
    _ack(data, cursor, item)
    data["seen"] = (data["seen"] + [key])[-_MAX_SEEN:]
    data["failures"] = 0
    data["next_at"] = now + _INTERVAL
    _save(path, data)
    return {"status": "delivered", "event": item["kind"], "issue": item["issue"]}
