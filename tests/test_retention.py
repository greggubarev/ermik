"""Owned artifact inventory and bounded retention cleanup."""

from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest

from ermik.observability import resource_guard
from ermik.evidence_store import write_evidence
from ermik.retention import cleanup, inventory
from ermik.state import initialize
from tests.test_state_migrations import create_preview


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=40)
RECENT = NOW - timedelta(days=2)


class MergedHub:
    def __init__(self, repo="owner/project"):
        self.repo = repo
        self.rows = {}
        self.calls = []

    def add(self, issue, pr, branch, head, *, merged_at=OLD,
            state="MERGED", issue_state="closed", head_repo=None):
        self.rows[pr] = {
            "number": pr, "state": state, "headRefOid": head,
            "headRefName": branch, "headRepository": {
                "nameWithOwner": head_repo or self.repo},
            "baseRefName": "main", "body": f"Closes #{issue}",
            "mergedAt": merged_at.isoformat() if merged_at else None,
            "issue": issue, "issue_state": issue_state,
        }

    def pr(self, number):
        self.calls.append(("pr", number))
        return self.rows[number]

    def issue(self, number, *, allow_closed=False):
        self.calls.append(("issue", number, allow_closed))
        row = next(row for row in self.rows.values() if row["issue"] == number)
        return {"state": row["issue_state"]}

    def validate_pr_identity(self, pr, *, branch, base_branch, number=None,
                             head_sha=None, state=None, **_):
        if (pr["headRefName"] != branch or pr["baseRefName"] != base_branch
                or pr["number"] != number or pr["headRefOid"] != head_sha
                or pr["state"] != state
                or pr["headRepository"]["nameWithOwner"] != self.repo):
            raise ValueError("PR identity changed")


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "project-a" / "workspace"
        self.state_dir = self.root / "project-a" / "state"
        self.source = self.workspace / "source"
        self.jobs = self.workspace / "jobs"
        self.jobs.mkdir(parents=True)
        self.git("init", str(self.source), cwd=self.root)
        self.git("config", "user.name", "Test", cwd=self.source)
        self.git("config", "user.email", "test@example.invalid", cwd=self.source)
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.source)
        self.git("commit", "-m", "base", cwd=self.source)
        self.conn = sqlite3.connect(initialize(self.state_dir))
        self.addCleanup(self.conn.close)
        self.cfg = SimpleNamespace(repo="owner/project", publish_repo=None,
                                   base_branch="main", workspace=self.workspace,
                                   state_dir=self.state_dir, retention_days=30,
                                   artifact_warn_mb=1,
                                   draft_pr_evidence_dir=None)
        self.hub = MergedHub()

    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def age(self, path, when=OLD):
        stamp = when.timestamp()
        if path.is_dir() and not path.is_symlink():
            for child in path.rglob("*"):
                if not child.is_symlink():
                    os.utime(child, (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def job(self, issue, status="merged", *, pr=None, head=None,
            branch=None, log=False, evidence=False, age=OLD):
        branch = branch or f"work/issue-{issue}-delivery"
        checkout = self.jobs / f"issue-{issue}"
        self.git("worktree", "add", "-b", branch, str(checkout), "HEAD",
                 cwd=self.source)
        head = head or self.git("rev-parse", "HEAD", cwd=checkout)
        self.conn.execute(
            "INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,head_sha,pr) "
            "VALUES(?,?,?,?,?,?,?)",
            (issue, branch, "a" * 40, status, "[]", head, pr),
        )
        self.conn.commit()
        if pr is not None:
            self.hub.add(issue, pr, branch, head)
        artifacts = {"worktree": checkout}
        if log:
            path = self.state_dir / "ci-logs" / head / "check-1.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"check output\n")
            self.age(path, age)
            self.age(path.parent, age)
            artifacts["log"] = path
        if evidence:
            path = self.state_dir / "evidence" / str(issue) / f"{head}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"issue": issue, "pr": pr,
                                        "head": head, "branch": branch,
                                        "base": "a" * 40}), encoding="utf-8")
            self.age(path, age)
            self.age(path.parent, age)
            artifacts["evidence"] = path
        self.age(checkout, age)
        return artifacts

    def test_inventory_reports_owned_bytes_and_age_without_mutating(self):
        paths = self.job(1, pr=101, log=True, evidence=True)
        before = {kind: path.stat().st_size if path.is_file() else path.exists()
                  for kind, path in paths.items()}
        result = inventory(self.cfg, now=NOW)
        self.assertGreater(result["total_bytes"], 0)
        for kind in ("worktrees", "logs", "evidence"):
            self.assertGreater(result["categories"][kind]["bytes"], 0)
            self.assertGreater(result["categories"][kind]["oldest_age_seconds"], 0)
        self.assertEqual(before, {kind: path.stat().st_size if path.is_file()
                                  else path.exists() for kind, path in paths.items()})

    def test_inventory_warning_at_configured_byte_threshold(self):
        paths = self.job(1, pr=101, log=True)
        paths["log"].write_bytes(b"x" * 1024**2)
        self.age(paths["log"])
        result = inventory(self.cfg, now=NOW)
        self.assertTrue(result["warning"])
        self.assertGreaterEqual(result["categories"]["logs"]["bytes"], 1024**2)

    def test_dry_run_and_bounded_cleanup_then_repeat(self):
        paths = self.job(1, pr=101, log=True, evidence=True)
        preview = cleanup(self.cfg, gh=self.hub, dry_run=True, max_items=1,
                          max_bytes=1 << 30, now=NOW)
        self.assertTrue(preview["dry_run"])
        self.assertLessEqual(len(preview["selected"]), 1)
        self.assertTrue(all(path.exists() for path in paths.values()))
        first = cleanup(self.cfg, gh=self.hub, dry_run=False, max_items=1,
                        max_bytes=1 << 30, now=NOW)
        self.assertLessEqual(len(first["removed"]), 1)
        cleanup(self.cfg, gh=self.hub, dry_run=False, max_items=32,
                max_bytes=1 << 30, now=NOW)
        again = cleanup(self.cfg, gh=self.hub, dry_run=False, max_items=32,
                        max_bytes=1 << 30, now=NOW)
        self.assertEqual(again["removed"], [])
        self.assertFalse(paths["worktree"].exists())
        self.assertFalse(paths["log"].exists())
        self.assertFalse(paths["evidence"].exists())

    def test_disk_pressure_refusal_does_not_trigger_cleanup(self):
        paths = self.job(1, pr=101)
        signal = resource_guard(self.workspace, min_free_bytes=10,
                                max_worktrees=1, disk_free_bytes=9)
        self.assertFalse(signal["admit"])
        self.assertEqual(signal["reasons"], ["low_disk_space", "worktree_limit"])
        self.assertTrue(paths["worktree"].exists())

    def test_active_blocked_and_unverified_work_remain(self):
        for issue, status in ((1, "running"), (2, "correcting"),
                              (3, "blocked"), (4, "retry_wait")):
            self.job(issue, status, pr=100 + issue, log=True, evidence=True)
        dirty = self.job(5, pr=105, log=True, evidence=True)
        (dirty["worktree"] / "untracked.txt").write_text("keep", encoding="utf-8")
        result = cleanup(self.cfg, gh=self.hub, dry_run=False,
                         max_bytes=1 << 30, now=NOW)
        self.assertEqual(result["removed"], [])
        for issue in range(1, 6):
            self.assertTrue((self.jobs / f"issue-{issue}").exists())
        self.assertTrue((dirty["worktree"] / "untracked.txt").exists())

    def test_unpushed_head_and_wrong_remote_identity_remain(self):
        unique = self.job(1, pr=101, log=True, evidence=True)
        (unique["worktree"] / "tracked.txt").write_text("unique\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=unique["worktree"])
        self.git("commit", "-m", "unique", cwd=unique["worktree"])
        wrong = self.job(2, pr=102, log=True, evidence=True)
        self.hub.rows[102]["headRefOid"] = "f" * 40
        wrong_pr = self.job(3, pr=103, log=True, evidence=True)
        self.hub.rows[103]["number"] = 999
        result = cleanup(self.cfg, gh=self.hub, dry_run=False,
                         max_bytes=1 << 30, now=NOW)
        self.assertEqual(result["removed"], [])
        self.assertTrue(unique["worktree"].exists())
        self.assertTrue(wrong["worktree"].exists())
        self.assertTrue(wrong_pr["worktree"].exists())

    def test_recent_or_unmerged_or_open_issue_remains(self):
        recent = self.job(1, pr=101, log=True, evidence=True, age=RECENT)
        unmerged = self.job(2, pr=102, log=True, evidence=True)
        self.hub.rows[102]["state"] = "OPEN"
        open_issue = self.job(3, pr=103, log=True, evidence=True)
        self.hub.rows[103]["issue_state"] = "open"
        result = cleanup(self.cfg, gh=self.hub, dry_run=False,
                         max_bytes=1 << 30, now=NOW)
        self.assertEqual(result["removed"], [])
        for paths in (recent, unmerged, open_issue):
            self.assertTrue(paths["worktree"].exists())
            self.assertTrue(paths["log"].exists())
            self.assertTrue(paths["evidence"].exists())

    def test_symlink_and_hardlink_are_rejected_without_following(self):
        paths = self.job(1, pr=101, log=True, evidence=True)
        outside = self.root / "outside.txt"
        outside.write_text("preserve", encoding="utf-8")
        paths["evidence"].unlink()
        try:
            paths["evidence"].symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks unavailable")
        with self.assertRaises(ValueError):
            cleanup(self.cfg, gh=self.hub, dry_run=False,
                    max_bytes=1 << 30, now=NOW)
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
        self.assertTrue(paths["evidence"].is_symlink())

        paths["evidence"].unlink()
        os.link(outside, paths["evidence"])
        with self.assertRaises(ValueError):
            cleanup(self.cfg, gh=self.hub, dry_run=False,
                    max_bytes=1 << 30, now=NOW)
        self.assertTrue(paths["evidence"].exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_symlinked_root_and_unexpected_path_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_text("preserve", encoding="utf-8")
        self.jobs.rmdir()
        try:
            self.jobs.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaises(ValueError):
            inventory(self.cfg, now=NOW)
        with self.assertRaises(ValueError):
            cleanup(self.cfg, gh=self.hub, dry_run=False, now=NOW)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_unknown_issue_directory_is_never_selected(self):
        unknown = self.jobs / "issue-999"
        unknown.mkdir()
        marker = unknown / "marker.txt"
        marker.write_text("preserve", encoding="utf-8")
        self.age(unknown)
        result = cleanup(self.cfg, gh=self.hub, dry_run=False, now=NOW)
        self.assertEqual(result["removed"], [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_unexpected_symlink_child_in_artifact_root_fails_closed(self):
        paths = self.job(1, pr=101, log=True, evidence=True)
        outside = self.root / "outside.txt"
        outside.write_text("preserve", encoding="utf-8")
        surprise = paths["log"].parent / "unexpected.txt"
        try:
            surprise.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks unavailable")
        with self.assertRaises(ValueError):
            inventory(self.cfg, now=NOW)
        with self.assertRaises(ValueError):
            cleanup(self.cfg, gh=self.hub, dry_run=False, now=NOW)
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_malformed_and_mismatched_evidence_remains(self):
        malformed = self.job(1, pr=101, evidence=True)
        mismatch = self.job(2, pr=102, evidence=True)
        malformed["evidence"].write_text("{", encoding="utf-8")
        mismatch["evidence"].write_text(json.dumps({
            "issue": 2, "pr": 999, "head": self.git("rev-parse", "HEAD", cwd=self.source),
            "branch": "work/issue-2-delivery", "base": "a" * 40,
        }), encoding="utf-8")
        self.age(malformed["evidence"])
        self.age(mismatch["evidence"])
        result = cleanup(self.cfg, gh=self.hub, dry_run=False,
                         max_bytes=1 << 30, now=NOW)
        self.assertTrue(malformed["evidence"].exists())
        self.assertTrue(mismatch["evidence"].exists())
        self.assertFalse(any(item["kind"] == "evidence" for item in result["removed"]))

    def test_incomplete_or_uncertain_local_merge_preserves_artifacts(self):
        paths = self.job(1, pr=101, log=True, evidence=True)
        head = self.git("rev-parse", "HEAD", cwd=paths["worktree"])
        record_path = self.state_dir / "reviews" / "pr-101.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        identity = {"repo": self.cfg.repo, "pr": 101, "issue": 1,
                    "head": head, "base": "a" * 40,
                    "branch": "work/issue-1-delivery"}
        for unfinished in (
            {"merged": {"status": "incomplete", "reason": "post-merge checks failed"}},
            {"merge_attempt": {"head": head, "base": "a" * 40,
                               "branch": "work/issue-1-delivery"}},
            {"merge_pending": {"status": "incomplete",
                               "reason": "merge outcome is unconfirmed"}},
        ):
            with self.subTest(unfinished=next(iter(unfinished))):
                write_evidence(record_path, {**identity, **unfinished})
                result = cleanup(self.cfg, gh=self.hub, dry_run=False,
                                 max_bytes=1 << 30, now=NOW)
                self.assertEqual(result["removed"], [])
                self.assertTrue(all(path.exists() for path in paths.values()))

        # Successful merge records keep the historical attempt marker.
        write_evidence(record_path, {**identity,
                                     "merge_attempt": {"head": head,
                                                       "base": "a" * 40,
                                                       "branch": "work/issue-1-delivery"},
                                     "merge_commit": "b" * 40,
                                     "merged": {"status": "complete", "pr": 101,
                                                "issue": 1,
                                                "merge_commit": "b" * 40}})
        complete = cleanup(self.cfg, gh=self.hub, dry_run=False,
                           max_bytes=1 << 30, now=NOW)
        self.assertTrue(complete["removed"])
        self.assertFalse(any(path.exists() for path in paths.values()))

    def test_dry_run_without_state_does_not_create_directories(self):
        missing = SimpleNamespace(**vars(self.cfg))
        missing.workspace = self.root / "absent" / "workspace"
        missing.state_dir = self.root / "absent" / "state"
        result = cleanup(missing, gh=self.hub, dry_run=True, now=NOW)
        self.assertEqual(result["selected"], [])
        self.assertFalse(missing.state_dir.exists())
        self.assertFalse(missing.workspace.exists())

    def test_dry_run_refuses_preview_db_without_migrating(self):
        legacy = self.root / "legacy-state"
        db = create_preview(legacy)
        before = db.read_bytes()
        cfg = SimpleNamespace(**vars(self.cfg))
        cfg.state_dir = legacy
        with self.assertRaisesRegex(ValueError, "current state schema"):
            cleanup(cfg, gh=self.hub, dry_run=True, now=NOW)
        self.assertEqual(db.read_bytes(), before)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("image_id", columns)

    def test_shared_sha_log_waits_for_every_referencing_issue(self):
        first = self.job(1, pr=101, log=True, evidence=True)
        self.job(2, status="blocked", pr=102, log=True, evidence=True)
        shared_log = first["log"]
        result = cleanup(self.cfg, gh=self.hub, dry_run=False,
                         max_bytes=1 << 30, now=NOW)
        self.assertTrue(shared_log.exists())
        self.assertFalse(any(item["kind"] == "logs" for item in result["removed"]))

    def test_interruption_after_unlink_recovers_on_retry(self):
        paths = self.job(1, pr=101, log=True, evidence=True)
        original_unlink = Path.unlink
        interrupted = False

        def unlink_then_interrupt(path, *args, **kwargs):
            nonlocal interrupted
            original_unlink(path, *args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt("simulated process stop")

        with patch("ermik.retention.Path.unlink", autospec=True,
                   side_effect=unlink_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                cleanup(self.cfg, gh=self.hub, dry_run=False,
                        max_bytes=1 << 30, now=NOW)
        self.assertTrue(interrupted)
        cleanup(self.cfg, gh=self.hub, dry_run=False,
                max_bytes=1 << 30, now=NOW)
        final = cleanup(self.cfg, gh=self.hub, dry_run=False,
                        max_bytes=1 << 30, now=NOW)
        self.assertEqual(final["removed"], [])
        self.assertFalse(any(path.exists() for path in paths.values()))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid"),
                         "POSIX ownership only")
    def test_wrong_owner_refuses_cleanup(self):
        paths = self.job(1, pr=101, evidence=True)
        with patch("ermik.retention.os.geteuid", return_value=-1):
            with self.assertRaises(ValueError):
                cleanup(self.cfg, gh=self.hub, dry_run=False, now=NOW)
        self.assertTrue(paths["evidence"].exists())

    def test_second_project_sibling_is_untouched(self):
        self.job(1, pr=101, log=True, evidence=True)
        other = self.root / "project-b"
        (other / "workspace" / "jobs" / "issue-1").mkdir(parents=True)
        marker = other / "workspace" / "jobs" / "issue-1" / "marker.txt"
        marker.write_text("other project", encoding="utf-8")
        (other / "state" / "evidence" / "1").mkdir(parents=True)
        other_proof = other / "state" / "evidence" / "1" / "proof.json"
        other_proof.write_text("other proof", encoding="utf-8")
        cleanup(self.cfg, gh=self.hub, dry_run=False,
                max_bytes=1 << 30, now=NOW)
        self.assertEqual(marker.read_text(encoding="utf-8"), "other project")
        self.assertEqual(other_proof.read_text(encoding="utf-8"), "other proof")

    def test_invalid_cleanup_limits_rejected(self):
        for count, size in ((0, 1), (-1, 1), (1, 0), (1, -1)):
            with self.subTest(count=count, size=size):
                with self.assertRaises(ValueError):
                    cleanup(self.cfg, gh=self.hub, dry_run=False,
                            max_items=count, max_bytes=size, now=NOW)


if __name__ == "__main__":
    unittest.main()
