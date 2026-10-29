"""Crash recovery and independent queue progress at the controller boundary."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ermik import runtime, state
from ermik.config import Config


BODY = """## Goal
Create one file.

## Allowed paths
src/second.py

## Acceptance
The file contains a greeting.
"""
FIRST_BODY = BODY.replace("src/second.py", "src/first.py")
HEAD = "b" * 40
BASE = "a" * 40


class CrashMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main", (), 0)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json) "
                         "VALUES(1,'work/issue-1-delivery',?,?,?,?,?)",
                         (BASE, HEAD, 10, "awaiting_review", '["src/first.py"]'))
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, runtime.parse_issue(FIRST_BODY).body_sha256,
                          '["src/first.py"]'))
        self.gh = Mock()
        self.gh.ready_issues.return_value = [2]
        self.gh.issue.side_effect = lambda number: {
            "body": FIRST_BODY if number == 1 else BODY,
            "title": "First" if number == 1 else "Second",
            "labels": [{"name": "queue:worker"},
                       {"name": "state:in-progress" if number == 1 else "state:ready"}]}
        self.gh.pr.return_value = {"state": "OPEN", "headRefOid": HEAD}
        self.gh.find_branch_pr.return_value = None
        self.gh.comments.return_value = []

    def tearDown(self):
        self.temp.cleanup()

    def _cycle(self):
        result = {"issue": 2, "pr": 20, "branch": "work/issue-2-delivery",
                  "head": "c" * 40, "base": BASE, "ci": {"result": "passed"}}
        with patch.object(runtime, "GitHub", return_value=self.gh), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "main_sha", return_value=BASE), patch.object(
                runtime, "resource_guard", return_value={"admit": True}), patch.object(
                runtime, "checked", return_value=""), patch.object(
                runtime, "_perform", return_value=result) as perform:
            self.assertEqual(runtime.run_once(self.cfg), [result])
        perform.assert_called_once()
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 2)["status"], "awaiting_review")
            return state.job(conn, 1)["status"]

    def test_changed_pr_identity_blocks_only_its_job(self):
        self.gh.validate_pr_identity.side_effect = runtime.GitHubError("mismatch")
        self.assertEqual(self._cycle(), "blocked")
        self.gh.label.assert_any_call(1, add="state:blocked", remove="state:in-progress")

    def test_pr_read_outage_leaves_job_pending_and_does_not_starve_queue(self):
        self.gh.pr.side_effect = runtime.GitHubError("transport unavailable")
        self.assertEqual(self._cycle(), "awaiting_review")

    def test_comment_read_outage_leaves_job_pending_and_does_not_starve_queue(self):
        self.gh.comments.side_effect = runtime.GitHubError("transport unavailable")
        self.assertEqual(self._cycle(), "awaiting_review")

    def test_crashed_unpublished_job_retries_while_queue_advances(self):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET status='running',head_sha=NULL,pr=NULL WHERE issue=1")
        self.assertEqual(self._cycle(), "retry_wait")

    def test_valid_prior_pr_remains_pending_without_rework_request(self):
        self.assertEqual(self._cycle(), "awaiting_review")

    def _claim_after_first_read_fails(self, *, approval_read: bool):
        self.gh.ready_issues.return_value = [2, 3]
        def issue(number):
            if number == 2 and not approval_read:
                raise runtime.GitHubError("transport unavailable")
            return {"body": BODY.replace("src/second.py", f"src/{number}.py"),
                    "labels": [{"name": "queue:worker"}, {"name": "state:ready"}]}
        self.gh.issue.side_effect = issue
        def approval(_gh, _cfg, number, _spec):
            if number == 2 and approval_read:
                raise runtime.GitHubError("transport unavailable")
            return True
        with state.connect(self.cfg.state_dir) as conn, patch.object(
                runtime, "resource_guard", return_value={"admit": True}), patch.object(
                runtime, "main_sha", return_value=BASE), patch.object(
                runtime, "_approved_on_github", side_effect=approval):
            claimed = runtime._claim_new(self.gh, self.cfg, conn)
            self.assertEqual(claimed[0], 3)
            self.assertIsNone(state.job(conn, 2))

    def test_ready_issue_read_outage_does_not_starve_next_issue(self):
        self._claim_after_first_read_fails(approval_read=False)

    def test_ready_approval_read_outage_does_not_starve_next_issue(self):
        self._claim_after_first_read_fails(approval_read=True)


if __name__ == "__main__":
    unittest.main()
