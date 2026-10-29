from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ermik.observability import log_window_metadata, resource_guard, status_snapshot
from ermik.state import initialize


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
