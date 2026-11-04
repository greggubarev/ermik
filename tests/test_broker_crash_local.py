"""Process interruption at the draft PR response boundary.

The SQLite API double persists the remote PR independently of broker processes.
Run with ``py -3 -m unittest discover -s tests -p test_broker_crash_local.py -v``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ermik.policy import parse_issue
from ermik.pr_broker import Broker


HEAD = "a" * 40
BASE = "b" * 40
IMAGE = "sha256:" + "c" * 64
CONFIG = "sha256:" + "d" * 64
BRANCH = "work/issue-7-delivery"
BODY = "## Allowed paths\nsrc/file.py\n\n## Goal\nFix it\n\n## Acceptance\nIt works\n"
REQUEST = {"op": "create_draft", "issue": 7, "branch": BRANCH,
           "head_sha": HEAD, "base_sha": BASE}


class PersistentApi:
    """Only the GitHub reads and PR POST needed by this broker scenario."""

    def __init__(self, database: Path, marker: Path | None = None):
        self.database = database
        self.marker = marker

    def _pr(self):
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT body FROM pull_requests WHERE number = 19").fetchone()
        if row is None:
            return None
        return {"number": 19, "state": "open", "draft": True,
                "merged_at": None, "body": row[0],
                "head": {"ref": BRANCH, "sha": HEAD,
                         "repo": {"full_name": "person/fork"}},
                "base": {"ref": "main", "sha": BASE,
                         "repo": {"full_name": "org/upstream"}}}

    def call(self, method, path, payload=None):
        if method == "POST" and path == "repos/org/upstream/pulls":
            with sqlite3.connect(self.database, timeout=10) as db:
                db.execute("INSERT INTO pull_requests(number, body) VALUES (19, ?)",
                           (payload["body"],))
            if self.marker is not None:
                self.marker.write_text("post committed", encoding="ascii")
                while True:
                    time.sleep(1)
            return self._pr()
        if path == "repos/org/upstream":
            return {"id": 11, "full_name": "org/upstream"}
        if path == "repos/person/fork":
            return {"id": 22, "full_name": "person/fork", "fork": True,
                    "parent": {"id": 11, "full_name": "org/upstream"}}
        if path == "repos/org/upstream/issues/7":
            return {"state": "open", "labels": [{"name": "queue:worker"},
                                                  {"name": "state:in-progress"}],
                    "body": BODY, "title": "Small fix"}
        if path.startswith("repos/org/upstream/issues/7/comments?"):
            spec = parse_issue(BODY)
            scope = hashlib.sha256(json.dumps(spec.allowed_paths,
                                              separators=(",", ":")).encode()).hexdigest()
            return [{"user": {"login": "maintainer"},
                     "body": f"SEMI_ADMIT issue=7 body={spec.body_sha256} scope={scope}"}]
        if path == "repos/org/upstream/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"sha": BASE}}
        if path == "repos/person/fork/git/ref/heads/work%2Fissue-7-delivery":
            return {"ref": "refs/heads/" + BRANCH, "object": {"sha": HEAD}}
        if path.startswith("repos/org/upstream/compare/"):
            return {"status": "ahead", "commits": [{"sha": HEAD}],
                    "total_commits": 1, "merge_base_commit": {"sha": BASE},
                    "files": [{"filename": "src/file.py", "status": "modified"}]}
        if path == f"repos/person/fork/git/trees/{HEAD}?recursive=1":
            return {"truncated": False, "tree": [
                {"path": "src", "type": "tree", "mode": "040000"},
                {"path": "src/file.py", "type": "blob", "mode": "100644"}]}
        if path.startswith("repos/org/upstream/pulls?state=all"):
            existing = self._pr()
            return [existing] if existing else []
        if path == "repos/org/upstream/pulls/19":
            return self._pr()
        raise AssertionError((method, path))


def request_draft(root: Path, hold: bool) -> dict:
    api = PersistentApi(root / "remote.sqlite", root / "post-committed" if hold else None)
    broker = Broker(repo="org/upstream", fork="person/fork", maintainer="maintainer",
                    controller_uid=(os.getuid() if hasattr(os, "getuid") else 0),
                    evidence_dir=root / "evidence", api=api, repo_id=11, fork_id=22)
    return broker.create_draft(REQUEST)


class BrokerProcessKillTests(unittest.TestCase):
    def test_concurrent_request_and_crash_after_post_reconcile_one_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            db = sqlite3.connect(root / "remote.sqlite")
            try:
                db.execute("CREATE TABLE pull_requests(number INTEGER PRIMARY KEY, body TEXT NOT NULL)")
                db.commit()
            finally:
                db.close()
            evidence = root / "evidence" / "7"
            evidence.mkdir(parents=True)
            spec = parse_issue(BODY)
            record = {"issue": 7, "branch": BRANCH, "head": HEAD, "base": BASE,
                      "body_sha256": spec.body_sha256, "paths": ["src/file.py"],
                      "ci": {"result": "passed", "image_id": IMAGE,
                             "image_config_digest": CONFIG}}
            (evidence / f"{HEAD}.json").write_text(json.dumps(record), encoding="utf-8")
            first = subprocess.Popen([sys.executable, __file__, "--child", str(root), "hold"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not (root / "post-committed").exists() and time.monotonic() < deadline:
                    if first.poll() is not None:
                        self.fail("first broker exited before the PR POST committed")
                    time.sleep(0.02)
                self.assertTrue((root / "post-committed").exists(),
                                "first broker did not commit the PR POST")
                second = subprocess.run([sys.executable, __file__, "--child", str(root)],
                                        capture_output=True, text=True, timeout=10, check=True)
                self.assertEqual(json.loads(second.stdout), {"pr": 19, "created": False})
                self.assertIsNone(first.poll(), "first broker must still lack its response")
            finally:
                first.kill()
                first.wait(timeout=10)
            replay = subprocess.run([sys.executable, __file__, "--child", str(root)],
                                    capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(json.loads(replay.stdout), {"pr": 19, "created": False})
            db = sqlite3.connect(root / "remote.sqlite")
            try:
                self.assertEqual(db.execute("SELECT count(*) FROM pull_requests").fetchone()[0], 1)
            finally:
                db.close()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        print(json.dumps(request_draft(Path(sys.argv[2]), len(sys.argv) == 4)), flush=True)
    else:
        unittest.main()
