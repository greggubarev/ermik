"""A backlog of draft PRs is checked fairly without a request storm."""

from pathlib import Path
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ermik import runtime, state
from ermik.config import Config
from ermik.github import GitHubError


BODY = """## Goal
Update the example.

## Allowed paths
src/example.py

## Acceptance
The example is updated.
"""


class ReviewPollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("true",), (("true",),), 2, 60,
                          (), "main", (), 0)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, count):
        with state.connect(self.cfg.state_dir) as conn:
            for issue in range(1, count + 1):
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json) "
                             "VALUES(?,?,?,?,?,'awaiting_review',?)",
                             (issue, f"work/issue-{issue}-delivery", "a" * 40,
                              "b" * 40, issue + 100, '["src/example.py"]'))

    def test_backlog_progresses_in_bounded_batches(self):
        self.seed(25)
        gh = Mock()
        gh.pr.side_effect = GitHubError("temporary read failure")
        with patch.object(runtime.time, "time", return_value=1000):
            with state.connect(self.cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(gh, self.cfg, conn, [10]))
                self.assertEqual(gh.pr.call_count, 10)
                self.assertIsNone(runtime._correction(gh, self.cfg, conn, [10]))
                self.assertEqual(gh.pr.call_count, 20)
                self.assertIsNone(runtime._correction(gh, self.cfg, conn, [10]))
                self.assertEqual(gh.pr.call_count, 25)
                self.assertIsNone(runtime._correction(gh, self.cfg, conn, [10]))
                self.assertEqual(gh.pr.call_count, 25)
                next_checks = conn.execute("SELECT DISTINCT review_next_check_at FROM jobs").fetchall()
                self.assertEqual([row[0] for row in next_checks], [1060])

    def test_unmodified_pr_is_rechecked_after_interval(self):
        self.seed(1)
        gh = Mock()
        gh.pr.return_value = {"state": "OPEN", "headRefOid": "b" * 40}
        gh.issue.return_value = {"body": BODY, "labels": [
            {"name": "queue:worker"}, {"name": "state:in-progress"}]}
        gh.comments.return_value = []
        with patch.object(runtime, "_require_pr"), patch.object(
                runtime, "_approved_on_github", return_value=True):
            with state.connect(self.cfg.state_dir) as conn:
                spec = runtime.parse_issue(BODY)
                conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                             (1, spec.body_sha256, '["src/example.py"]'))
                with patch.object(runtime.time, "time", return_value=1000):
                    self.assertIsNone(runtime._correction(gh, self.cfg, conn))
                with patch.object(runtime.time, "time", return_value=1599):
                    self.assertIsNone(runtime._correction(gh, self.cfg, conn))
                self.assertEqual(gh.pr.call_count, 1)
                with patch.object(runtime.time, "time", return_value=1600):
                    self.assertIsNone(runtime._correction(gh, self.cfg, conn))
                self.assertEqual(gh.pr.call_count, 2)

    def test_correction_waits_for_old_head_delivery(self):
        self.seed(1)
        cfg = replace(self.cfg, evidence_publisher="publisher", evidence_publisher_id=42)
        gh = Mock()
        gh.pr.return_value = {"state": "OPEN", "headRefOid": "b" * 40}
        gh.issue.return_value = {"body": BODY, "labels": [
            {"name": "queue:worker"}, {"name": "state:in-progress"}]}
        note = f"SEMI_REWORK issue=1 head={'b' * 40}\nSEMI_REBASE_ONLY"
        gh.comments.return_value = [{"id": 1, "body": note,
                                     "user": {"login": "owner"}}]
        with patch.object(runtime, "_require_pr"), patch.object(
                runtime, "_approved_on_github", return_value=True):
            with state.connect(cfg.state_dir) as conn:
                spec = runtime.parse_issue(BODY)
                conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                             (1, spec.body_sha256, '["src/example.py"]'))
                with patch.object(runtime.time, "time", return_value=1000):
                    self.assertIsNone(runtime._correction(gh, cfg, conn))
                self.assertEqual(state.job(conn, 1)["status"], "awaiting_review")
                state.event(conn, 1, "evidence_delivered", head_sha="b" * 40)
                with patch.object(runtime.time, "time", return_value=1060):
                    selected = runtime._correction(gh, cfg, conn)
                self.assertEqual(selected[0]["issue"], 1)
                self.assertEqual(state.job(conn, 1)["status"], "correcting")


if __name__ == "__main__":
    unittest.main()
