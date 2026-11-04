"""Maintainer actions require the current, exact issue admission."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik import review, runtime


BODY = "## Allowed paths\nsrc/a.py\n\n## Goal\nFix a\n\n## Acceptance\nWorks\n"
HEAD = "a" * 40
BASE = "b" * 40
IMAGE = "sha256:" + "c" * 64


class Hub:
    def __init__(self):
        self.body = BODY
        self.author = "maintainer"
        self.marker = runtime._admission_marker(7, runtime.parse_issue(BODY))
        self.writes = []
        self.requests = []

    def identity(self):
        return "maintainer"

    def issue(self, number):
        assert number == 7
        return {"body": self.body, "labels": [{"name": "queue:worker"},
                                               {"name": "state:in-progress"}]}

    def comments(self, number):
        if number == 7:
            return [{"body": self.marker, "user": {"login": self.author}}]
        return self.requests if number == 19 else []

    def pr(self, number):
        assert number == 19
        return {"number": 19, "state": "OPEN", "baseRefName": "main",
                "baseRefOid": BASE, "headRefName": "work/issue-7-delivery",
                "headRefOid": HEAD, "changedFiles": 1,
                "files": [{"path": "src/a.py"}],
                "body": f"Closes #7\n\nExecution image: `{IMAGE}`",
                "isDraft": True}

    def validate_pr_identity(self, *_args, **_kwargs):
        return None

    def comment(self, number, body):
        self.writes.append((number, body))
        if number == 19:
            self.requests.append({"body": body, "user": {"login": "maintainer"}})

    def command(self, args, **_kwargs):
        self.writes.append(tuple(args))


class ReviewAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "maintainer", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),), 1, 60, (), "main")
        self.gh = Hub()

    def test_exact_approval_allows_review_identity(self):
        pr, issue, _ = review._identity(self.cfg, self.gh, 19)
        self.assertEqual((pr["headRefOid"], issue), (HEAD, 7))

    def test_changed_body_or_wrong_author_refuses_review(self):
        self.gh.body += "\nChanged\n"
        with self.assertRaisesRegex(runtime.WorkflowError, "admission"):
            review._identity(self.cfg, self.gh, 19)
        self.gh.body = BODY
        self.gh.author = "other-user"
        with self.assertRaisesRegex(runtime.WorkflowError, "admission"):
            review._identity(self.cfg, self.gh, 19)

    def test_request_changes_requires_current_approval_and_reviewed_body(self):
        record = {"pr": 19, "issue": 7, "repo": self.cfg.repo,
                  "head": HEAD, "base": BASE, "image_id": IMAGE,
                  "body_sha256": runtime.parse_issue(BODY).body_sha256,
                  "paths": ["src/a.py"],
                  "ci": {"head": HEAD, "committed_diff_base": BASE,
                         "image_id": IMAGE, "result": "passed",
                         "checks": [{"command": ["true"], "exit_code": 0,
                                     "log_sha256": "d" * 64}]}}
        note = Path(self.temp.name) / "note.txt"
        note.write_text("Please fix the edge case.", encoding="utf-8")
        with patch.object(review, "GitHub", return_value=self.gh), patch.object(
                review, "_load", return_value=record):
            self.assertEqual(review.request_changes(self.cfg, 19, note)["request"], "posted")
            self.gh.body += "\nChanged\n"
            with self.assertRaisesRegex(runtime.WorkflowError, "admission"):
                review.request_changes(self.cfg, 19, note)
        self.assertEqual(len(self.gh.writes), 1)

    def test_merge_refuses_unadmitted_pr_before_github_write(self):
        self.gh.marker = "SEMI_ADMIT issue=7 body=stale scope=stale"
        record = {"pr": 19, "issue": 7, "repo": self.cfg.repo,
                  "head_repository": self.cfg.repo,
                  "branch": "work/issue-7-delivery", "head": HEAD, "base": BASE,
                  "body_sha256": runtime.parse_issue(BODY).body_sha256,
                  "paths": ["src/a.py"], "image_id": IMAGE}
        with patch.object(review, "GitHub", return_value=self.gh), patch.object(
                review, "_load", return_value=record):
            with self.assertRaisesRegex(runtime.WorkflowError, "admission"):
                review.merge(self.cfg, 19)
        self.assertEqual(self.gh.writes, [])


if __name__ == "__main__":
    unittest.main()
