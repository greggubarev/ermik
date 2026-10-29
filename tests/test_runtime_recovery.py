"""Controller retry integration with a local ledger and simulated GitHub."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik import recovery, runtime, state
from ermik.config import Config


BODY = """## Goal
Create one file.

## Allowed paths
src/example.py

## Acceptance
The file contains a greeting.
"""
BASE = "a" * 40
HEAD = "b" * 40


class Hub:
    issues = {}
    comments_by_number = {}
    prs = {}
    opened = []

    def __init__(self, repo):
        self.repo = repo

    def identity(self):
        return "owner"

    def issue(self, number):
        issue = self.issues[number]
        return {**issue, "labels": [{"name": name} for name in issue["names"]]}

    def label(self, number, *, add=None, remove=None):
        names = self.issues[number]["names"]
        if remove:
            names.discard(remove)
        if add:
            names.add(add)

    def comments(self, number):
        return self.comments_by_number.get(number, [])

    def comment(self, number, body):
        rows = self.comments_by_number.setdefault(number, [])
        rows.append({"id": len(rows) + 1, "body": body,
                     "user": {"login": "owner"}})

    def ready_issues(self):
        return [number for number, issue in self.issues.items()
                if {"queue:worker", "state:ready"}.issubset(issue["names"])]

    def find_branch_pr(self, branch, base_branch=None):
        return next((number for number, pr in self.prs.items()
                     if pr["headRefName"] == branch), None)

    def pr(self, number):
        return self.prs[number]

    def validate_pr_identity(self, pr, *, branch, base_branch, number=None,
                             head_sha=None, state=None, **_kwargs):
        if (pr["headRefName"] != branch or
                head_sha is not None and pr["headRefOid"] != head_sha or
                state is not None and pr["state"] != state):
            raise runtime.GitHubError("PR mismatch")

    def open_pr(self, branch, body, title, base):
        self.opened.append(branch)
        number = 10 + len(self.opened)
        self.prs[number] = {"state": "OPEN", "headRefName": branch,
                            "headRefOid": HEAD}
        return number


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main", (), 0)
        Hub.issues = {1: {"body": BODY, "title": "Example",
                          "names": {"queue:worker", "state:proposed"}}}
        Hub.comments_by_number = {}
        Hub.prs = {}
        Hub.opened = []
        self.patches = [patch.object(runtime, "GitHub", Hub),
                        patch.object(runtime, "main_sha", return_value=BASE),
                        patch.object(runtime, "resource_guard",
                                     return_value={"admit": True})]
        for item in self.patches:
            item.start()
        runtime.admit(self.cfg, 1)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _first_failure(self):
        with patch.object(runtime, "_perform", side_effect=runtime.WorkerFailure(1)):
            result = runtime.run_once(self.cfg)
        self.assertEqual(result[0]["status"], "retry_wait")
        with state.connect(self.cfg.state_dir) as conn:
            row = state.job(conn, 1)
            due = conn.execute("SELECT next_retry_at FROM retry_state WHERE issue=1").fetchone()[0]
            self.assertEqual(row["status"], "retry_wait")
            self.assertEqual(row["branch"], "work/issue-1-delivery")
        self.assertIn("state:in-progress", Hub.issues[1]["names"])
        return due

    def _run_due(self, due, perform):
        actual_claim = recovery.claim_due
        with patch.object(runtime, "claim_due", side_effect=lambda conn, limit:
                          actual_claim(conn, limit=limit, now=due)):
            with patch.object(runtime, "_perform", side_effect=perform):
                return runtime.run_once(self.cfg)

    def test_worker_failure_retries_same_issue_and_branch_without_duplicate_pr(self):
        due = self._first_failure()
        calls = []

        def perform(_cfg, issue, branch, spec, *, correction_note,
                    pr_number, expected_base, expected_head):
            calls.append((issue, branch, pr_number, expected_base, expected_head))
            number = Hub(self.cfg.repo).open_pr(branch, "proof", "Example", "main")
            return {"issue": issue, "pr": number, "branch": branch,
                    "head": HEAD, "paths": ["src/example.py"],
                    "ci": {"result": "passed"}}

        result = self._run_due(due, perform)
        self.assertEqual(result[0]["pr"], 11)
        self.assertEqual(calls, [(1, "work/issue-1-delivery", None, BASE, None)])
        self.assertEqual(Hub.opened, ["work/issue-1-delivery"])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["status"], "awaiting_review")
            self.assertIsNone(conn.execute("SELECT 1 FROM retry_state WHERE issue=1").fetchone())
        self.assertEqual(runtime.run_once(self.cfg), [])
        self.assertEqual(len(Hub.opened), 1)

    def test_changed_issue_identity_blocks_due_retry(self):
        due = self._first_failure()
        Hub.issues[1]["body"] = BODY.replace("src/example.py", "src/other.py")
        result = self._run_due(due, AssertionError("worker must not run"))
        self.assertEqual(result, [])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["status"], "blocked")
        self.assertEqual(Hub.opened, [])

    def test_preexisting_branch_pr_blocks_retry_without_second_pr(self):
        due = self._first_failure()
        Hub.prs[10] = {"state": "OPEN", "headRefName": "work/issue-1-delivery",
                       "headRefOid": HEAD}
        result = self._run_due(due, AssertionError("worker must not run"))
        self.assertEqual(result, [])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["status"], "blocked")
        self.assertEqual(Hub.opened, [])

    def test_existing_retry_checkout_is_preserved_for_inspection(self):
        due = self._first_failure()
        checkout = self.cfg.workspace / "jobs" / "issue-1"
        checkout.mkdir(parents=True)
        evidence = checkout / "uncommitted.txt"
        evidence.write_text("inspect this", encoding="utf-8")
        result = self._run_due(due, AssertionError("worker must not run"))
        self.assertEqual(result, [])
        self.assertEqual(evidence.read_text(encoding="utf-8"), "inspect this")
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["status"], "blocked")

    def test_interrupted_claim_without_remote_branch_schedules_same_job(self):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,?,?)", (1, "work/issue-1-delivery", BASE,
                         "running", '["src/example.py"]'))
        with patch.object(runtime, "checked", return_value=""):
            self.assertEqual(runtime.run_once(self.cfg), [])
        with state.connect(self.cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual(row["status"], "retry_wait")
            self.assertEqual(row["branch"], "work/issue-1-delivery")
        self.assertIn("state:in-progress", Hub.issues[1]["names"])

    def test_interrupted_claim_with_unverified_remote_branch_blocks(self):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,?,?)", (1, "work/issue-1-delivery", BASE,
                         "running", '["src/example.py"]'))
        with patch.object(runtime, "checked", return_value=HEAD + "\trefs/heads/work/issue-1-delivery"):
            self.assertEqual(runtime.run_once(self.cfg), [])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
