"""Recovery of abandoned Git worktrees at the resource limit."""

from pathlib import Path
import os
import sqlite3
import subprocess
import tempfile
import unittest

from ermik.observability import resource_guard
from ermik.resources import recover_stale_worktrees
from ermik.state import initialize


class WorkspaceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.source = self.workspace / "source"
        self.jobs = self.workspace / "jobs"
        self.jobs.mkdir(parents=True)
        self.git("init", str(self.source), cwd=self.root)
        self.git("config", "user.name", "Test", cwd=self.source)
        self.git("config", "user.email", "test@example.invalid", cwd=self.source)
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.source)
        self.git("commit", "-m", "base", cwd=self.source)
        self.conn = sqlite3.connect(initialize(self.root / "state"))
        self.addCleanup(self.conn.close)

    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout

    def worktree(self, number):
        path = self.jobs / f"issue-{number}"
        self.git("worktree", "add", "-b", f"work/issue-{number}-delivery",
                 str(path), "HEAD", cwd=self.source)
        return path

    def job(self, number, status, *, head=None, pr=None):
        self.conn.execute(
            "INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,head_sha,pr) "
            "VALUES(?,?,?,?,?,?,?)",
            (number, f"work/issue-{number}-delivery", "a" * 40, status,
             "[]", head, pr),
        )
        self.conn.commit()

    def test_verified_published_tree_releases_guard_capacity(self):
        path = self.worktree(1)
        self.job(1, "awaiting_review", head=self.git("rev-parse", "HEAD", cwd=path).strip(),
                 pr=101)
        self.assertFalse(resource_guard(self.workspace, min_free_bytes=0,
                                        max_worktrees=1)["admit"])
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result, {"removed": [1], "preserved": [],
                                  "requires_inspection": []})
        self.assertFalse(path.exists())
        self.assertTrue(resource_guard(self.workspace, min_free_bytes=0,
                                       max_worktrees=1)["admit"])

    def test_missing_workspace_is_a_noop(self):
        missing = self.root / "new-workspace"
        self.assertEqual(recover_stale_worktrees(missing, self.conn),
                         {"removed": [], "preserved": [],
                          "requires_inspection": []})
        self.assertFalse(missing.exists())

    def test_empty_jobs_without_source_is_a_noop(self):
        other = self.root / "other-workspace"
        (other / "jobs").mkdir(parents=True)
        self.assertEqual(recover_stale_worktrees(other, self.conn),
                         {"removed": [], "preserved": [],
                          "requires_inspection": []})

    def test_active_jobs_and_dirty_stale_tree_are_preserved(self):
        active = self.worktree(1)
        correcting = self.worktree(2)
        dirty = self.worktree(3)
        self.job(1, "running")
        self.job(2, "correcting")
        self.job(3, "awaiting_review", head=self.git("rev-parse", "HEAD", cwd=dirty).strip(),
                 pr=103)
        (dirty / "uncommitted.txt").write_text("keep", encoding="utf-8")
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result, {"removed": [], "preserved": [1, 2],
                                  "requires_inspection": [3]})
        self.assertTrue(active.exists())
        self.assertTrue(correcting.exists())
        self.assertEqual((dirty / "uncommitted.txt").read_text(encoding="utf-8"), "keep")

    def test_unregistered_issue_directory_is_not_deleted(self):
        rogue = self.jobs / "issue-4"
        rogue.mkdir()
        (rogue / "evidence.txt").write_text("keep", encoding="utf-8")
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [4])
        self.assertTrue((rogue / "evidence.txt").exists())

    def test_orphaned_registered_clean_tree_is_preserved(self):
        orphan = self.worktree(6)
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [6])
        self.assertTrue(orphan.exists())

    def test_clean_checkout_with_unique_unpushed_commit_is_preserved(self):
        checkout = self.worktree(8)
        self.job(8, "blocked")
        (checkout / "tracked.txt").write_text("unique commit\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=checkout)
        self.git("commit", "-m", "unique", cwd=checkout)
        unique_head = self.git("rev-parse", "HEAD", cwd=checkout).strip()
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [8])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=checkout).strip(), unique_head)

    def test_retry_wait_tree_is_preserved_even_when_clean(self):
        checkout = self.worktree(9)
        self.job(9, "retry_wait")
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [9])
        self.assertTrue(checkout.exists())

    def test_published_ledger_head_mismatch_preserves_unpushed_commit(self):
        checkout = self.worktree(10)
        published_head = self.git("rev-parse", "HEAD", cwd=checkout).strip()
        self.job(10, "awaiting_review", head=published_head, pr=110)
        (checkout / "tracked.txt").write_text("later commit\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=checkout)
        self.git("commit", "-m", "later", cwd=checkout)
        local_head = self.git("rev-parse", "HEAD", cwd=checkout).strip()
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [10])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=checkout).strip(), local_head)

    def test_published_status_without_pr_is_not_sufficient(self):
        checkout = self.worktree(11)
        self.job(11, "awaiting_review", head=self.git("rev-parse", "HEAD", cwd=checkout).strip())
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [11])
        self.assertTrue(checkout.exists())

    def test_unknown_job_status_is_preserved(self):
        path = self.worktree(7)
        self.job(7, "unexpected")
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [7])
        self.assertTrue(path.exists())

    def test_symlink_issue_directory_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "evidence.txt").write_text("keep", encoding="utf-8")
        try:
            os.symlink(outside, self.jobs / "issue-5", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        result = recover_stale_worktrees(self.workspace, self.conn)
        self.assertEqual(result["requires_inspection"], [5])
        self.assertTrue((outside / "evidence.txt").exists())


if __name__ == "__main__":
    unittest.main()
