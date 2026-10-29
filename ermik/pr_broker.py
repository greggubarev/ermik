"""A narrow, separately credentialed draft PR service for a trusted controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .policy import parse_issue, validate_changed_paths


_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_MESSAGE = 4096


class BrokerError(RuntimeError):
    """A refusal whose detailed cause stays within the broker process."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BrokerError(message)


def _private_file(path: Path, owners: set[int]) -> bytes:
    """Open without following a symlink and check the opened inode."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536
                 and (os.name != "posix" or
                      (info.st_uid in owners and not info.st_mode & 0o022)),
                 "untrusted private file")
        return os.read(fd, 65537)
    finally:
        os.close(fd)


class Api:
    def __init__(self, token: str, opener=urlopen):
        self.token = token
        self.opener = opener

    def call(self, method: str, path: str, payload: dict | None = None):
        request = Request("https://api.github.com/" + path,
                          data=(json.dumps(payload).encode("utf-8") if payload is not None
                                else None), method=method,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28",
                                   "User-Agent": "ermik-pr-broker",
                                   "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=20) as response:
                return json.load(response)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise BrokerError("GitHub request failed") from exc


class Broker:
    def __init__(self, *, repo: str, fork: str, maintainer: str, controller_uid: int,
                 evidence_dir: Path, api: Api, repo_id: int, fork_id: int,
                 base_branch: str = "main"):
        _require(bool(_REPO.fullmatch(repo)) and bool(_REPO.fullmatch(fork))
                 and repo.casefold() != fork.casefold(), "invalid repository configuration")
        _require(base_branch == "main", "broker requires main as base")
        _require(controller_uid >= 0 and bool(maintainer)
                 and type(repo_id) is int and repo_id > 0
                 and type(fork_id) is int and fork_id > 0 and repo_id != fork_id,
                 "invalid trust configuration")
        self.repo, self.fork, self.maintainer = repo, fork, maintainer
        self.controller_uid, self.evidence_dir, self.api = controller_uid, evidence_dir, api
        self.repo_id, self.fork_id = repo_id, fork_id

    def _pages(self, path: str) -> list[dict]:
        rows = []
        for page in range(1, 11):
            separator = "&" if "?" in path else "?"
            batch = self.api.call("GET", path + separator + urlencode({"per_page": 100,
                                                                        "page": page}))
            _require(isinstance(batch, list), "invalid GitHub listing")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise BrokerError("GitHub listing too large")

    def _ref(self, repo: str, branch: str) -> str:
        row = self.api.call("GET", f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}")
        obj = row.get("object") if isinstance(row, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        _require(row.get("ref") == f"refs/heads/{branch}" and isinstance(sha, str)
                 and bool(_SHA.fullmatch(sha)), "branch reference mismatch")
        return sha

    def _existing(self, branch: str) -> dict | None:
        owner = self.fork.split("/", 1)[0]
        path = f"repos/{self.repo}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}"
        matches = [row for row in self._pages(path)
                   if (row.get("head") or {}).get("ref") == branch
                   and str(((row.get("head") or {}).get("repo") or {}).get("full_name", "")).casefold()
                   == self.fork.casefold()]
        _require(len(matches) <= 1, "multiple PRs for branch")
        return matches[0] if matches else None

    def _validate_pr(self, row: dict, branch: str, head: str, base: str) -> int:
        full = self.api.call("GET", f"repos/{self.repo}/pulls/{row['number']}")
        source, target = full.get("head") or {}, full.get("base") or {}
        _require(full.get("state") == "open" and full.get("draft") is True
                 and full.get("merged_at") is None
                 and source.get("ref") == branch and source.get("sha") == head
                 and str((source.get("repo") or {}).get("full_name", "")).casefold()
                 == self.fork.casefold()
                 and target.get("ref") == "main" and target.get("sha") == base
                 and str((target.get("repo") or {}).get("full_name", "")).casefold()
                 == self.repo.casefold(), "existing PR identity mismatch")
        return full["number"]

    def _evidence(self, issue: int, branch: str, head: str, base: str,
                  body_hash: str, paths: list[str]) -> None:
        directory = self.evidence_dir.resolve(strict=True)
        _require(directory == self.evidence_dir and directory.is_dir(),
                 "evidence directory is not canonical")
        # Controller is trusted to report its local checks. The broker independently
        # checks remote identity, scope, and exact refs; evidence cannot override them.
        path = directory / str(issue) / f"{head}.json"
        _require(not path.parent.is_symlink(), "untrusted evidence directory")
        parent_info = path.parent.stat()
        _require(os.name != "posix" or
                 (parent_info.st_uid in {0, self.controller_uid}
                  and not parent_info.st_mode & 0o022),
                 "untrusted evidence directory")
        evidence = json.loads(_private_file(path, {0, self.controller_uid}))
        _require(isinstance(evidence, dict) and evidence.get("issue") == issue
                 and evidence.get("branch") == branch
                 and evidence.get("head") == head and evidence.get("base") == base
                 and evidence.get("body_sha256") == body_hash
                 and evidence.get("paths") == paths
                 and (evidence.get("ci") or {}).get("result") == "passed",
                 "CI evidence mismatch")

    def create_draft(self, request: dict) -> dict:
        _require(isinstance(request, dict)
                 and set(request) == {"op", "issue", "branch", "head_sha", "base_sha"}
                 and request.get("op") == "create_draft", "invalid request")
        issue, branch = request["issue"], request["branch"]
        head, base = request["head_sha"], request["base_sha"]
        _require(type(issue) is int and issue > 0 and issue <= 2**31 - 1
                 and branch == f"work/issue-{issue}-delivery"
                 and isinstance(head, str) and bool(_SHA.fullmatch(head))
                 and isinstance(base, str) and bool(_SHA.fullmatch(base)),
                 "invalid request identity")
        upstream = self.api.call("GET", f"repos/{self.repo}")
        fork = self.api.call("GET", f"repos/{self.fork}")
        _require(str(upstream.get("full_name", "")).casefold() == self.repo.casefold()
                 and upstream.get("id") == self.repo_id
                 and str(fork.get("full_name", "")).casefold() == self.fork.casefold()
                 and fork.get("id") == self.fork_id
                 and fork.get("fork") is True
                 and str((fork.get("parent") or {}).get("full_name", "")).casefold()
                 == self.repo.casefold()
                 and (fork.get("parent") or {}).get("id") == self.repo_id,
                 "repository identity mismatch")
        row = self.api.call("GET", f"repos/{self.repo}/issues/{issue}")
        labels = {label.get("name") for label in row.get("labels", [])}
        _require(row.get("state") == "open" and "pull_request" not in row
                 and {"queue:worker", "state:in-progress"} <= labels,
                 "issue state mismatch")
        spec = parse_issue(row.get("body") or "")
        marker_scope = hashlib.sha256(json.dumps(spec.allowed_paths, separators=(",", ":"),
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()
        marker = (f"SEMI_ADMIT issue={issue} body={spec.body_sha256} "
                  f"scope={marker_scope}")
        comments = self._pages(f"repos/{self.repo}/issues/{issue}/comments")
        _require(any((comment.get("body") or "").splitlines()[:1] == [marker]
                     and (comment.get("user") or {}).get("login") == self.maintainer
                     for comment in comments), "issue admission mismatch")
        _require(self._ref(self.repo, "main") == base
                 and self._ref(self.fork, branch) == head, "branch moved")
        compare = self.api.call("GET", f"repos/{self.repo}/compare/"
                               f"{base}...{quote(self.fork.split('/')[0] + ':' + branch, safe=':')}")
        files = compare.get("files")
        commits = compare.get("commits")
        _require(compare.get("status") == "ahead"
                 and isinstance(commits, list) and len(commits) == 1
                 and compare.get("total_commits") == 1
                 and commits[0].get("sha") == head
                 and (compare.get("merge_base_commit") or {}).get("sha") == base
                 and isinstance(files, list) and 0 < len(files) <= 100,
                 "diff identity mismatch")
        paths = []
        for file in files:
            _require(isinstance(file, dict) and file.get("status") in
                     {"added", "modified", "removed", "renamed", "copied", "changed"},
                     "invalid diff file")
            paths.append(file.get("filename"))
            if file.get("previous_filename"):
                paths.append(file["previous_filename"])
        _require(all(isinstance(path, str) for path in paths), "invalid diff paths")
        validate_changed_paths(paths, spec.allowed_paths)
        paths = sorted(set(paths))
        tree = self.api.call("GET", f"repos/{self.fork}/git/trees/{head}?recursive=1")
        _require(tree.get("truncated") is False and isinstance(tree.get("tree"), list),
                 "incomplete commit tree")
        entries = {entry.get("path"): entry for entry in tree["tree"]}
        for file in files:
            if file["status"] == "removed":
                continue
            path = file["filename"]
            entry = entries.get(path) or {}
            _require(entry.get("type") == "blob" and entry.get("mode") in
                     {"100644", "100755"}, "nonregular changed file")
            parts = path.split("/")
            for index in range(1, len(parts)):
                _require((entries.get("/".join(parts[:index])) or {}).get("type") == "tree",
                         "nonregular parent path")
        self._evidence(issue, branch, head, base, spec.body_sha256, paths)
        existing = self._existing(branch)
        if existing is not None:
            return {"pr": self._validate_pr(existing, branch, head, base), "created": False}
        _require(self._ref(self.repo, "main") == base
                 and self._ref(self.fork, branch) == head, "branch moved before PR")
        title = f"Issue #{issue}: {str(row.get('title') or '').strip()}"[:240]
        body = (f"Closes #{issue}\n\nLocal checks passed on `{head}`.\n"
                "Review the full diff and acceptance criteria.")
        try:
            created = self.api.call("POST", f"repos/{self.repo}/pulls",
                                    {"title": title, "body": body,
                                     "head": f"{self.fork.split('/')[0]}:{branch}",
                                     "base": "main", "draft": True})
        except BrokerError:
            existing = self._existing(branch)
            if existing is None:
                raise
            return {"pr": self._validate_pr(existing, branch, head, base),
                    "created": False}
        return {"pr": self._validate_pr(created, branch, head, base), "created": True}


def peer_uid(connection: socket.socket) -> int:
    _require(hasattr(socket, "SO_PEERCRED"), "peer credentials unavailable")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", raw)
    return uid


def handle_connection(connection: socket.socket, broker: Broker) -> None:
    connection.settimeout(30)
    try:
        _require(peer_uid(connection) == broker.controller_uid, "unauthorized peer")
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(min(1024, _MAX_MESSAGE + 1 - len(data)))
            _require(bool(chunk) and len(data) + len(chunk) <= _MAX_MESSAGE,
                     "invalid message length")
            data.extend(chunk)
        _require(data.endswith(b"\n") and data.count(b"\n") == 1,
                 "invalid message frame")
        result = broker.create_draft(json.loads(data))
        reply = {"ok": True, **result}
    except Exception:
        # Raw exceptions, HTTP bodies and tokens never cross this boundary.
        reply = {"ok": False, "error": "request refused"}
    try:
        connection.sendall((json.dumps(reply, separators=(",", ":")) + "\n").encode())
    except OSError:
        # The peer may disconnect while GitHub is processing the request.
        # A replay will reconcile any PR created before the response was lost.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve one draft PR operation")
    parser.add_argument("serve", choices=["serve"])
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--fork", required=True)
    parser.add_argument("--repo-id", type=int, required=True)
    parser.add_argument("--fork-id", type=int, required=True)
    parser.add_argument("--maintainer", required=True)
    parser.add_argument("--controller-uid", type=int, required=True)
    parser.add_argument("--socket-gid", type=int, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    token = parser.add_mutually_exclusive_group(required=True)
    token.add_argument("--token-file", type=Path)
    token.add_argument("--token-env")
    args = parser.parse_args()
    secret = (_private_file(args.token_file, {0, os.geteuid()}).decode().strip()
              if args.token_file else os.environ.get(args.token_env, ""))
    _require(bool(secret) and "\n" not in secret, "publication credential unavailable")
    broker = Broker(repo=args.repo, fork=args.fork, maintainer=args.maintainer,
                    controller_uid=args.controller_uid, evidence_dir=args.evidence_dir,
                    api=Api(secret), repo_id=args.repo_id, fork_id=args.fork_id)
    _require(args.socket.is_absolute() and not args.socket.exists(),
             "socket path must be absent and absolute")
    _require(args.socket_gid >= 0, "invalid socket group")
    old_umask = os.umask(0o117)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(args.socket))
        os.chown(args.socket, -1, args.socket_gid)
        os.chmod(args.socket, 0o660)
        server.listen(8)
        while True:
            connection, _ = server.accept()
            with connection:
                handle_connection(connection, broker)
    finally:
        server.close()
        os.umask(old_umask)
        args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
