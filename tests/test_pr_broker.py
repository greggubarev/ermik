"""The draft PR boundary rejects stale identities and reconciles POST loss."""

import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from ermik.policy import parse_issue
from ermik.pr_broker import Broker, BrokerError, handle_connection


HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "work/issue-7-delivery"
BODY = "## Allowed paths\nsrc/file.py\n\n## Goal\nFix it\n\n## Acceptance\nIt works\n"


class FakeApi:
    def __init__(self):
        self.calls = []
        self.existing = None
        self.fail_post = False
        self.branch_sha = HEAD
        self.issue_labels = [{"name": "queue:worker"}, {"name": "state:in-progress"}]

    def pr(self):
        return {"number": 19, "state": "open", "draft": True, "merged_at": None,
                "head": {"ref": BRANCH, "sha": HEAD,
                         "repo": {"full_name": "person/fork"}},
                "base": {"ref": "main", "sha": BASE,
                         "repo": {"full_name": "org/upstream"}}}

    def call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST":
            self.existing = self.pr()
            if self.fail_post:
                raise BrokerError("ambiguous response")
            return self.existing
        if path == "repos/org/upstream":
            return {"id": 11, "full_name": "org/upstream"}
        if path == "repos/person/fork":
            return {"id": 22, "full_name": "person/fork", "fork": True,
                    "parent": {"id": 11, "full_name": "org/upstream"}}
        if path == "repos/org/upstream/issues/7":
            return {"state": "open", "labels": self.issue_labels,
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
            return {"ref": "refs/heads/" + BRANCH,
                    "object": {"sha": self.branch_sha}}
        if path.startswith("repos/org/upstream/compare/"):
            return {"status": "ahead", "commits": [{"sha": HEAD}],
                    "total_commits": 1,
                    "merge_base_commit": {"sha": BASE},
                    "files": [{"filename": "src/file.py", "status": "modified"}]}
        if path == f"repos/person/fork/git/trees/{HEAD}?recursive=1":
            return {"truncated": False, "tree": [
                {"path": "src", "type": "tree", "mode": "040000"},
                {"path": "src/file.py", "type": "blob", "mode": "100644"}]}
        if path.startswith("repos/org/upstream/pulls?state=all"):
            return [self.existing] if self.existing else []
        if path == "repos/org/upstream/pulls/19":
            return self.pr()
        raise AssertionError((method, path))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        evidence_dir = root / "evidence"
        evidence_dir.mkdir(mode=0o700)
        issue_dir = evidence_dir / "7"
        issue_dir.mkdir(mode=0o700)
        self.evidence = issue_dir / (HEAD + ".json")
        self.record = {"issue": 7, "branch": BRANCH, "head": HEAD, "base": BASE,
                       "body_sha256": parse_issue(BODY).body_sha256,
                       "paths": ["src/file.py"], "ci": {"result": "passed"}}
        self.write_evidence()
        self.api = FakeApi()
        self.broker = Broker(repo="org/upstream", fork="person/fork",
                             repo_id=11, fork_id=22, maintainer="maintainer",
                             controller_uid=self.evidence.stat().st_uid,
                             evidence_dir=evidence_dir,
                             api=self.api)
        self.request = {"op": "create_draft", "issue": 7, "branch": BRANCH,
                        "head_sha": HEAD, "base_sha": BASE}

    def write_evidence(self):
        self.evidence.write_text(json.dumps(self.record), encoding="utf-8")
        self.evidence.chmod(0o600)

    def test_create_draft_and_idempotent_replay(self):
        self.assertEqual(self.broker.create_draft(self.request),
                         {"pr": 19, "created": True})
        self.assertEqual(self.broker.create_draft(self.request),
                         {"pr": 19, "created": False})
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 1)
        post = next(payload for method, _, payload in self.api.calls if method == "POST")
        self.assertTrue(post["draft"])
        self.assertEqual(post["head"], "person:" + BRANCH)

    def test_uncertain_post_reconciles_same_pr(self):
        self.api.fail_post = True
        self.assertEqual(self.broker.create_draft(self.request),
                         {"pr": 19, "created": False})
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 1)

    def test_moved_fork_head_refuses_before_post(self):
        self.api.branch_sha = "c" * 40
        with self.assertRaises(BrokerError):
            self.broker.create_draft(self.request)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_evidence_mismatch_refuses_before_post(self):
        self.record["ci"]["result"] = "failed"
        self.write_evidence()
        with self.assertRaises(BrokerError):
            self.broker.create_draft(self.request)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_out_of_scope_compare_refuses_before_post(self):
        original = self.api.call

        def out_of_scope(method, path, payload=None):
            result = original(method, path, payload)
            if path.startswith("repos/org/upstream/compare/"):
                result["files"] = [{"filename": "other/file.py", "status": "modified"}]
            return result

        self.api.call = out_of_scope
        with self.assertRaises(ValueError):
            self.broker.create_draft(self.request)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_repository_id_mismatch_refuses_before_post(self):
        self.broker.fork_id = 999
        with self.assertRaises(BrokerError):
            self.broker.create_draft(self.request)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_closed_duplicate_refuses(self):
        self.api.existing = self.api.pr()
        original = self.api.call

        def closed(method, path, payload=None):
            result = original(method, path, payload)
            if path == "repos/org/upstream/pulls/19":
                result["state"] = "closed"
            return result

        self.api.call = closed
        with self.assertRaises(BrokerError):
            self.broker.create_draft(self.request)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_peer_auth_refuses_without_invoking_broker(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall((json.dumps(self.request) + "\n").encode())
        with patch("ermik.pr_broker.peer_uid",
                   return_value=self.broker.controller_uid + 1):
            handle_connection(left, self.broker)
        self.assertEqual(json.loads(right.recv(1024)),
                         {"ok": False, "error": "request refused"})
        self.assertEqual(self.api.calls, [])

    def test_disconnected_client_does_not_prevent_next_request(self):
        class DisconnectedClient:
            def settimeout(self, _seconds):
                pass

            def recv(self, _size):
                return (json.dumps(self_request) + "\n").encode()

            def sendall(self, _data):
                raise BrokenPipeError("client disconnected")

        self_request = self.request
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall((json.dumps(self.request) + "\n").encode())
        with patch("ermik.pr_broker.peer_uid",
                   return_value=self.broker.controller_uid):
            handle_connection(DisconnectedClient(), self.broker)
            handle_connection(left, self.broker)
        self.assertEqual(json.loads(right.recv(1024)),
                         {"ok": True, "pr": 19, "created": False})
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 1)


if __name__ == "__main__":
    unittest.main()
