"""Explicit merge recovery at ambiguous GitHub and local persistence boundaries."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from ermik import review, runtime
from ermik.config import Config
from ermik.github import GitHub, GitHubError
from ermik.policy import parse_issue


BODY = "## Goal\nUpdate one file.\n\n## Allowed paths\nsrc/a.py\n\n## Acceptance\nWorks.\n"
BASE = "a" * 40
HEAD = "b" * 40
MERGED = "c" * 40
IMAGE = "sha256:" + "d" * 64
BRANCH = "work/issue-7-delivery"
PR = 19


class MergeHub(GitHub):
    def __init__(self, repo):
        super().__init__(repo)
        self.state = "OPEN"
        self.head = HEAD
        self.head_repo = repo
        self.branch = BRANCH
        self.base = BASE
        self.main = BASE
        self.merge_parent = BASE
        self.merge_calls = 0
        self.ready_calls = 0
        self.lose_merge_response = False
        self.ambiguous_open_response = False
        self.merge_entered = None
        self.release_merge = None
        self.drift_after_merge = False
        self.close_on_merge = True

    def identity(self):
        return "maintainer"

    def pr(self, number):
        assert number == PR
        return {"number": number, "state": self.state, "isDraft": False,
                "baseRefName": "main", "baseRefOid": self.base,
                "headRefName": self.branch, "headRefOid": self.head,
                "headRepository": {"nameWithOwner": self.head_repo},
                "changedFiles": 1, "files": [{"path": "src/a.py"}],
                "body": "Closes #7",
                "mergeCommit": {"oid": MERGED} if self.state == "MERGED" else None}

    def comments(self, number):
        assert number == PR
        return []

    def command(self, args, **kwargs):
        if args[:2] == ["pr", "merge"]:
            self.merge_calls += 1
            assert args[-1] == HEAD
            if self.merge_entered is not None:
                self.merge_entered.set()
                if not self.release_merge.wait(5):
                    raise AssertionError("merge test did not release first call")
            if self.ambiguous_open_response:
                self.ambiguous_open_response = False
                raise GitHubError("merge response lost before remote outcome")
            self.state = "MERGED"
            self.main = "f" * 40 if self.drift_after_merge else MERGED
            self.base = MERGED  # GitHub may show the updated target ref after merge.
            if self.lose_merge_response:
                self.lose_merge_response = False
                raise GitHubError("merge response lost")
            return ""
        if args[:2] == ["pr", "ready"]:
            self.ready_calls += 1
            return ""
        raise AssertionError(args)

    def api(self, path):
        if path == "issues/7":
            return {"state": "closed" if self.state == "MERGED" and self.close_on_merge else "open",
                    "body": BODY, "labels": [{"name": "queue:worker"},
                                              {"name": "state:in-progress"}]}
        if path == f"git/commits/{MERGED}":
            return {"sha": MERGED, "parents": [{"sha": self.merge_parent}]}
        raise AssertionError(path)


class MergeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cfg = Config("owner/repo", "maintainer", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main")
        self.hub = MergeHub(self.cfg.repo)
        self.check_calls = 0
        self.fail_check_once = False
        self.record = {"pr": PR, "issue": 7, "repo": self.cfg.repo,
                       "head_repository": self.cfg.repo, "branch": BRANCH,
                       "base": BASE, "head": HEAD,
                       "body_sha256": parse_issue(BODY).body_sha256,
                       "paths": ["src/a.py"], "decision": "reviewed",
                       "checks_sha256": review._checks_sha256(self.cfg),
                       "image_id": IMAGE}
        review._save(self.cfg, PR, self.record)
        for target, value in (("GitHub", self.hub),
                              ("_approved_on_github", True),
                              ("_resolve_image_id", IMAGE),
                              ("main_sha", self._main_sha),
                              ("mirror", root / "source"),
                              ("git", self._git),
                              ("local_checks", self._local_checks)):
            patcher = (patch.object(review, target, side_effect=value)
                       if callable(value) else patch.object(review, target, return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _main_sha(self, _gh, _cfg):
        return self.hub.main

    def _git(self, _source, *args):
        if args[:2] == ("worktree", "add"):
            Path(args[3]).mkdir(parents=True)
        elif args[:2] == ("worktree", "remove"):
            shutil.rmtree(args[2])
        else:
            raise AssertionError(args)
        return ""

    def _local_checks(self, _cfg, path, *, image_id):
        self.assertEqual(path, self.cfg.workspace / "reviews" / f"main-after-{PR}")
        self.assertEqual(image_id, IMAGE)
        self.check_calls += 1
        if self.fail_check_once:
            self.fail_check_once = False
            raise runtime.WorkflowError("post-merge checks failed")
        return {"result": "passed", "image_id": image_id}

    def _merge(self):
        return review.merge(self.cfg, PR)

    def _assert_saved_once(self):
        result = self._merge()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["merge_commit"], MERGED)
        self.assertEqual(result["main_ci"]["result"], "passed")
        self.assertTrue(result["issue_closed"])
        self.assertEqual(review._load(self.cfg, PR)["merged"], result)
        self.assertEqual(self.hub.merge_calls, 1)
        return result

    def test_lost_response_after_github_merge_reconciles_and_retry_is_read_only(self):
        self.hub.lose_merge_response = True
        first = self._merge()
        self.assertEqual(first, self._assert_saved_once())
        self.assertEqual(self.check_calls, 1)

    def test_ambiguous_merge_response_with_open_pr_never_reissues_merge(self):
        self.hub.ambiguous_open_response = True
        first = self._merge()
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["reason"], "merge outcome is unconfirmed")
        stored = review._load(self.cfg, PR)
        self.assertEqual(stored["merge_attempt"],
                         {"head": HEAD, "base": BASE, "branch": BRANCH})
        self.assertEqual(self.hub.merge_calls, 1)
        self.assertEqual(self._merge(), first)
        self.assertEqual(self.hub.merge_calls, 1)
        self.assertEqual(self.check_calls, 0)

    def test_crash_after_durable_attempt_before_request_does_not_issue_merge_on_retry(self):
        actual_save = review._save

        def crash_after_attempt(cfg, number, value):
            actual_save(cfg, number, value)
            if "merge_attempt" in value and "merge_commit" not in value:
                raise SystemExit("interrupted after durable attempt")

        with patch.object(review, "_save", side_effect=crash_after_attempt):
            with self.assertRaises(SystemExit):
                self._merge()
        stored = review._load(self.cfg, PR)
        self.assertEqual(stored["merge_attempt"],
                         {"head": HEAD, "base": BASE, "branch": BRANCH})
        self.assertEqual(self.hub.merge_calls, 0)
        self.assertEqual(self._merge()["reason"], "merge outcome is unconfirmed")
        self.assertEqual(self.hub.merge_calls, 0)

    def test_review_cannot_overwrite_ambiguous_merge_attempt(self):
        self.hub.ambiguous_open_response = True
        self._merge()
        prior = review._load(self.cfg, PR)
        with patch.object(review, "_checkout_pr", side_effect=AssertionError(
                "review checkout must not start after a merge attempt")):
            with self.assertRaisesRegex(runtime.WorkflowError, "merge attempt"):
                review.review(self.cfg, PR)
        self.assertEqual(review._load(self.cfg, PR), prior)
        self.assertEqual(self.hub.merge_calls, 1)

    def test_concurrent_merge_commands_do_not_both_write(self):
        self.hub.merge_entered = Event()
        self.hub.release_merge = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self._merge)
            self.assertTrue(self.hub.merge_entered.wait(5))
            second = pool.submit(self._merge)
            try:
                with self.assertRaises(OSError):
                    second.result(timeout=5)
            finally:
                self.hub.release_merge.set()
            self.assertEqual(first.result(timeout=5)["status"], "complete")
        self.assertEqual(self.hub.merge_calls, 1)

    def test_interruption_before_merge_request_retries_exact_review(self):
        original = self.hub.comments
        with patch.object(self.hub, "comments", side_effect=GitHubError("read lost")):
            with self.assertRaises(GitHubError):
                self._merge()
        self.assertEqual(self.hub.merge_calls, 0)
        self.hub.comments = original
        self._assert_saved_once()

    def test_interruption_before_post_merge_checks_resumes_without_second_merge(self):
        with patch.object(review, "mirror", side_effect=RuntimeError("interrupted")):
            first = self._merge()
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["reason"], "post-merge checkout failed")
        self.assertEqual(review._load(self.cfg, PR)["merged"], first)
        self.assertEqual(self.hub.state, "MERGED")
        self.assertEqual(self.check_calls, 0)
        self._assert_saved_once()
        self.assertEqual(self.check_calls, 1)

    def test_interruption_before_saving_result_resumes_without_second_merge(self):
        actual_save = review._save
        def save_except_complete(cfg, number, value):
            if value.get("merged", {}).get("status") == "complete":
                raise OSError("interrupted")
            return actual_save(cfg, number, value)

        with patch.object(review, "_save", side_effect=save_except_complete):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self._merge()
        self.assertNotIn("merged", review._load(self.cfg, PR))
        self._assert_saved_once()
        self.assertEqual(self.check_calls, 2)

    def test_changed_head_after_interruption_rejects_without_new_merge(self):
        with patch.object(self.hub, "comments", side_effect=GitHubError("read lost")):
            with self.assertRaises(GitHubError):
                self._merge()
        self.hub.head = "e" * 40
        with self.assertRaisesRegex(runtime.WorkflowError, "stale"):
            self._merge()
        self.assertEqual(self.hub.merge_calls, 0)

    def test_closed_unmerged_pr_rejects_without_merge_or_checks(self):
        self.hub.state = "CLOSED"
        with self.assertRaises((GitHubError, runtime.WorkflowError)):
            self._merge()
        self.assertEqual(self.hub.merge_calls, 0)
        self.assertEqual(self.check_calls, 0)

    def test_failed_post_merge_check_retries_without_second_merge(self):
        self.fail_check_once = True
        first = self._merge()
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["reason"], "post-merge checks failed")
        self.assertEqual(review._load(self.cfg, PR)["merged"], first)
        self.assertEqual(self.hub.state, "MERGED")
        incomplete = review._load(self.cfg, PR)["merged"]
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertEqual(incomplete["merge_commit"], MERGED)
        self._assert_saved_once()
        self.assertEqual(self.check_calls, 2)

    def test_main_drift_after_merge_has_distinct_incomplete_reason(self):
        self.hub.drift_after_merge = True
        first = self._merge()
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["reason"], "base branch moved after merge")
        self.assertEqual(review._load(self.cfg, PR)["merged"], first)
        self.assertEqual(self.check_calls, 0)
        self.assertEqual(self.hub.merge_calls, 1)

    def test_open_issue_after_merge_has_distinct_incomplete_reason(self):
        self.hub.close_on_merge = False
        first = self._merge()
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["reason"], "linked issue is not closed")
        self.assertEqual(review._load(self.cfg, PR)["merged"], first)
        self.assertEqual(self.check_calls, 1)
        self.assertEqual(self.hub.merge_calls, 1)

    def test_merged_pr_from_another_repository_is_not_adopted(self):
        self.hub.state = "MERGED"
        self.hub.main = MERGED
        self.hub.head_repo = "other/repo"
        with self.assertRaises(GitHubError):
            self._merge()
        self.assertEqual(self.check_calls, 0)

    def test_merged_pr_with_another_branch_is_not_adopted(self):
        self.hub.state = "MERGED"
        self.hub.main = MERGED
        self.hub.branch = "work/issue-8-delivery"
        with self.assertRaises((GitHubError, runtime.WorkflowError)):
            self._merge()
        self.assertEqual(self.check_calls, 0)

    def test_merged_commit_with_different_parent_is_not_adopted(self):
        self.hub.state = "MERGED"
        self.hub.main = MERGED
        self.hub.merge_parent = "f" * 40
        with self.assertRaises((GitHubError, runtime.WorkflowError)):
            self._merge()
        self.assertEqual(self.hub.merge_calls, 0)
        self.assertEqual(self.check_calls, 0)


if __name__ == "__main__":
    unittest.main()
