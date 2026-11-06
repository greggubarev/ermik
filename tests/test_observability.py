from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from ermik.observability import (QUOTA_FILE, log_window_metadata, resource_guard,
                                  status_snapshot)
from ermik.state import initialize
from ermik.__main__ import refresh_quota
from ermik.config import QuotaSource
from ermik.dashboard import public_snapshot


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_is_read_only_and_marks_unavailable_metrics(self):
        state_dir = self.root / "missing"
        value = status_snapshot(state_dir, max_parallel=2)
        self.assertFalse(state_dir.exists())
        self.assertIsNone(value["queue"]["count"])
        self.assertEqual(value["slots"]["available"], 2)
        self.assertIsNone(value["attempts"])
        self.assertEqual(value["events"], [])
        self.assertEqual(value["quota_observations"], [])
        unsupported = status_snapshot(state_dir, max_parallel=2,
                                      quota_providers=("other-provider",))
        self.assertEqual(unsupported["quota_observations"], [{
            "provider": "other-provider", "source": "unavailable",
            "observed_at": None, "status": "unknown", "unit": "percent",
            "windows": []}])

    def test_quota_observation_stale_and_malicious_fields_are_bounded(self):
        state_dir = self.root / "quota"
        state_dir.mkdir()
        row = {"provider": "opencode-go", "source": "opencode-local",
               "observed_at": "2026-11-07T10:00:00Z", "status": "ok",
               "unit": "percent", "windows": [{"name": "weekly",
               "used_percent": 31.0, "reset_at": "2026-11-14T10:00:00Z"}],
               "token": "never-display"}
        (state_dir / QUOTA_FILE).write_text(json.dumps({"version": 1,
            "providers": [row, {**row, "provider": "secret:malicious"}]}),
            encoding="utf-8")
        current = status_snapshot(state_dir, max_parallel=1,
            observed_at=datetime(2026, 11, 7, 10, 1, tzinfo=timezone.utc),
            quota_providers=("opencode-go",))
        self.assertEqual(len(current["quota_observations"]), 1)
        self.assertEqual(public_snapshot(current)["quota_observations"],
                         current["quota_observations"])
        self.assertNotIn("never-display", str(current))
        removed = status_snapshot(state_dir, max_parallel=1,
            observed_at=datetime(2026, 11, 7, 10, 1, tzinfo=timezone.utc),
            quota_providers=("opencode-go",), quota_sources={})
        self.assertEqual(removed["quota_observations"][0]["status"], "unknown")
        self.assertEqual(removed["quota_observations"][0]["source"], "unavailable")
        stale = status_snapshot(state_dir, max_parallel=1,
            observed_at=datetime(2026, 11, 7, 10, 16, tzinfo=timezone.utc),
            quota_providers=("opencode-go",))
        self.assertEqual(stale["quota_observations"][0]["status"], "stale")
        self.assertEqual(stale["quota_observations"][0]["windows"], [])
        row["windows"][0]["reset_at"] = "2026-11-07T10:02:00Z"
        (state_dir / QUOTA_FILE).write_text(json.dumps({"version": 1,
            "providers": [row]}), encoding="utf-8")
        reset = status_snapshot(state_dir, max_parallel=1,
            observed_at=datetime(2026, 11, 7, 10, 3, tzinfo=timezone.utc),
            quota_providers=("opencode-go",))
        self.assertEqual(reset["quota_observations"][0]["status"], "stale")

    def test_quota_refresh_stores_only_probe_projection(self):
        state_dir = self.root / "quota-refresh"
        state_dir.mkdir()
        cfg = SimpleNamespace(repo="owner/repo", state_dir=state_dir,
            quota_sources={"kimi-code": QuotaSource("kimi-code", 58627, "LOCAL_TOKEN")},
            models=(SimpleNamespace(provider_id="kimi-code"),))
        row = {"source": "kimi-cli-local", "observed_at": "2026-11-07T10:00:00Z",
               "status": "ok", "unit": "percent", "windows": [{
                   "name": "limit5h", "used_percent": 25.0,
                   "reset_at": "2026-11-07T15:00:00Z"}]}
        with patch.dict("os.environ", {"LOCAL_TOKEN": "test-only"}):
            with patch("ermik.__main__.probe", return_value=row) as mocked:
                result = refresh_quota(cfg)
        self.assertEqual(mocked.call_args.args[0:2], ("kimi-code", 58627))
        self.assertEqual(result["repo"], "owner/repo")
        self.assertEqual((state_dir / QUOTA_FILE).exists(), True)
        self.assertNotIn("LOCAL_TOKEN", (state_dir / QUOTA_FILE).read_text())

    def test_snapshot_redacts_error_and_counts_slots(self):
        state_dir = self.root / "state"
        db = initialize(state_dir)
        conn = sqlite3.connect(db)
        try:
            for issue, status, error in [(1, "running", "secret=abc"),
                                         (2, "correcting", None),
                                         (3, "awaiting_review", None),
                                         (4, "blocked", "token xyz")]:
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,error) "
                             "VALUES(?,?,?,?,?,?)",
                             (issue, f"private-{issue}", "a" * 40, status, "[]", error))
            conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=3",
                         (1_700_000_000,))
            conn.commit()
        finally:
            conn.close()
        value = status_snapshot(state_dir, max_parallel=2, ready_issues=[7, 5, 7],
                                observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(value["queue"], {"ready_issues": [5, 7], "count": 2})
        self.assertEqual(value["slots"], {"capacity": 2, "active": 2,
                                           "available": 0, "over_capacity": False})
        self.assertTrue(value["jobs"][0]["has_error"])
        self.assertEqual(value["jobs"][2]["next_review_at"], 1_700_000_000)
        self.assertIsNone(value["jobs"][0]["next_review_at"])
        self.assertNotIn("secret", str(value))
        self.assertNotIn("private", str(value))
        self.assertNotIn("token xyz", str(value))

    def test_waiting_and_blocked_guidance_never_recommends_an_unverified_retry(self):
        state_dir = self.root / "guidance"
        db = initialize(state_dir)
        with closing(sqlite3.connect(db)) as conn:
            for issue, status, pr, error in (
                    (1, "blocked", None, "secret token and unknown remote write"),
                    (2, "retry_wait", None, "provider response body"),
                    (3, "awaiting_review", 19, None),
                    (4, "awaiting_review", None, None),
                    (5, "retry_wait", None, "arbitrary failure")):
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,pr,status,allowed_json,error) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (issue, f"private-{issue}", "a" * 40, pr, status, "[]", error))
            conn.execute("INSERT INTO events(issue,kind) VALUES(1,'pr_published')")
            conn.execute("INSERT INTO retry_state(issue,attempts,category,resume_status,"
                         "next_retry_at,failure_id) VALUES(2,1,'provider','running',90,'f2')")
            conn.execute("INSERT INTO retry_state(issue,attempts,category,resume_status,"
                         "next_retry_at,failure_id) VALUES(5,1,'untrusted text','running',90,'f5')")
            conn.commit()
        jobs = {job["issue"]: job for job in status_snapshot(state_dir, max_parallel=2)["jobs"]}
        self.assertEqual((jobs[1]["reason_code"], jobs[1]["last_recorded_action"],
                          jobs[1]["next_action"]),
                         ("requires_inspection", "pr_published",
                          "reconcile_issue_branch_and_pr_manually"))
        self.assertEqual(jobs[2]["reason_code"], "provider_capacity_wait")
        self.assertEqual(jobs[3]["next_action"], "verify_current_pr_head_then_review")
        self.assertEqual(jobs[4]["reason_code"], "pr_identity_unverified")
        self.assertEqual(jobs[5]["reason_code"], "scheduled_retry_wait")
        self.assertNotIn("secret", str(jobs))
        self.assertNotIn("provider response", str(jobs))
        self.assertNotIn("untrusted text", str(jobs))
        self.assertNotIn("retry_now", str(jobs))

    def test_persisted_failure_reasons_override_only_matching_states(self):
        state_dir = self.root / "typed-reasons"
        db = initialize(state_dir)
        with closing(sqlite3.connect(db)) as conn:
            for issue, status, reason in ((1, "blocked", "provider_auth"),
                                          (2, "retry_wait", "provider_quota"),
                                          (3, "blocked", "local_check_failed"),
                                          (4, "awaiting_review", "provider_auth"),
                                          (5, "retry_wait", "publication_unconfirmed"),
                                          (6, "blocked", "broker_unavailable"),
                                          (7, "blocked", "publish_credential_unavailable")):
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,"
                             "reason_code,error) VALUES(?,?,?,?,?,?,?)",
                             (issue, f"work-{issue}", "a" * 40, status, "[]", reason,
                              "private token text"))
            conn.commit()
        jobs = {row["issue"]: row for row in status_snapshot(state_dir, max_parallel=2)["jobs"]}
        self.assertEqual(jobs[1]["reason_code"], "provider_auth")
        self.assertEqual(jobs[1]["next_action"], "verify_provider_credential_manually")
        self.assertEqual(jobs[2]["reason_code"], "provider_quota")
        self.assertEqual(jobs[3]["next_action"], "inspect_local_check_results")
        self.assertEqual(jobs[4]["reason_code"], "pr_identity_unverified")
        self.assertEqual(jobs[5]["next_action"], "reconcile_publication_remotely")
        self.assertEqual(jobs[6]["next_action"], "inspect_broker_manually")
        self.assertEqual(jobs[7]["next_action"], "verify_fork_credential_manually")
        self.assertNotIn("private token text", str(jobs))

    def test_resource_guard_is_non_destructive(self):
        workspace = self.root / "work"
        worktrees = workspace / "jobs"
        (worktrees / "issue-1").mkdir(parents=True)
        (worktrees / "other").mkdir()
        value = resource_guard(workspace, min_free_bytes=10, max_worktrees=1,
                               disk_free_bytes=9)
        self.assertEqual(value["reasons"], ["low_disk_space", "worktree_limit"])
        self.assertFalse(value["admit"])
        self.assertTrue((worktrees / "issue-1").exists())

    def test_resource_guard_missing_workspace(self):
        value = resource_guard(self.root / "new", min_free_bytes=10,
                               max_worktrees=2, disk_free_bytes=10)
        self.assertTrue(value["admit"])
        self.assertFalse((self.root / "new").exists())

    def test_resource_guard_refuses_low_state_filesystem(self):
        workspace = self.root / "work"
        state_dir = self.root / "state"
        value = resource_guard(workspace, state_dir=state_dir,
                               min_free_bytes=10, max_worktrees=1,
                               disk_free_bytes=100, state_disk_free_bytes=9)
        self.assertFalse(value["admit"])
        self.assertEqual(value["reasons"], ["low_state_disk_space"])
        self.assertEqual(value["disk_free_bytes"], 100)
        self.assertEqual(value["state_disk_free_bytes"], 9)
        self.assertFalse(workspace.exists())
        self.assertFalse(state_dir.exists())

    def test_unknown_issue_entry_uses_slot_even_when_not_a_directory(self):
        workspace = self.root / "work"
        jobs = workspace / "jobs"
        jobs.mkdir(parents=True)
        entry = jobs / "issue-7"
        entry.write_text("unexpected", encoding="utf-8")
        value = resource_guard(workspace, min_free_bytes=0, max_worktrees=1,
                               disk_free_bytes=100,
                               retained_issues=frozenset({7}))
        self.assertEqual(value["reasons"], ["worktree_limit"])
        entry.unlink()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            entry.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        value = resource_guard(workspace, min_free_bytes=0, max_worktrees=1,
                               disk_free_bytes=100,
                               retained_issues=frozenset({7}))
        self.assertEqual(value["reasons"], ["worktree_limit"])
        self.assertTrue(outside.exists())

    def test_log_metadata_bounds_read_without_exposing_content(self):
        path = self.root / "check.log"
        path.write_bytes(b"secret=" + b"x" * 30)
        value = log_window_metadata(path, max_bytes=8)
        self.assertEqual(value["size_bytes"], 37)
        self.assertEqual(value["window_bytes"], 8)
        self.assertTrue(value["truncated"])
        self.assertNotIn("secret", str(value))


if __name__ == "__main__":
    unittest.main()
