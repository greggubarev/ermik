"""A review remains valid only for its exact check policy and Docker image."""

from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ermik import review, runtime
from ermik.config import Config


BODY = "## Allowed paths\nsrc/a.py\n\n## Goal\nFix a\n\n## Acceptance\nWorks\n"
BASE = "a" * 40
HEAD = "b" * 40
MERGED = "c" * 40
IMAGE = "sha256:" + "d" * 64
OTHER_IMAGE = "sha256:" + "e" * 64
CHECKS = (("python", "-m", "unittest"), ("python", "-m", "compileall", "src"))


class Hub:
    def __init__(self):
        self.pr_state = "OPEN"
        self.draft = True
        self.head = HEAD
        self.writes = []
        self.on_ready = None

    def identity(self):
        return "maintainer"

    def issue(self, number):
        assert number == 7
        return {"body": BODY, "labels": [{"name": "queue:worker"},
                                          {"name": "state:in-progress"}]}

    def pr(self, number):
        assert number == 19
        return {"number": 19, "state": self.pr_state, "isDraft": self.draft,
                "baseRefName": "main", "baseRefOid": BASE,
                "headRefName": "work/issue-7-delivery", "headRefOid": self.head,
                "changedFiles": 1, "files": [{"path": "src/a.py"}],
                "body": "Closes #7",
                "mergeCommit": {"oid": MERGED} if self.pr_state == "MERGED" else None}

    def validate_pr_identity(self, *_args, **_kwargs):
        return None

    def comments(self, _number):
        return []

    def command(self, args, **_kwargs):
        self.writes.append(tuple(args))
        if args[:2] == ["pr", "ready"]:
            self.draft = False
            if self.on_ready:
                self.on_ready()
        elif args[:2] == ["pr", "merge"]:
            self.pr_state = "MERGED"

    def api(self, _path):
        return {"state": "closed"}


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config("owner/repo", "maintainer", self.root / "state",
                          self.root / "work", "demo:latest", ("/worker",),
                          CHECKS, 1, 60, (), "main")
        self.gh = Hub()
        self.image = IMAGE
        self.image_error = None
        self.local_calls = []
        self.checkout = self.root / "work" / "reviews" / "pr-19"
        for target, value in (
                ("GitHub", self.gh),
                ("_approved_on_github", True),
                ("main_sha", self._main_sha),
                ("_checkout_pr", self._checkout_pr),
                ("verify_changed_tree", ["src/a.py"]),
                ("local_checks", self._local_checks),
                ("mirror", self.root / "source"),
                ("git", self._git)):
            patcher = patch.object(review, target, return_value=value) if not callable(value) else patch.object(review, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        resolver = patch.object(review, "_resolve_image_id", side_effect=self._resolve_image)
        resolver.start()
        self.addCleanup(resolver.stop)

    def _resolve_image(self, _reference):
        if self.image_error:
            raise self.image_error
        return self.image

    def _main_sha(self, *_args):
        return MERGED if self.gh.pr_state == "MERGED" else BASE

    def _checkout_pr(self, *_args):
        self.checkout.mkdir(parents=True)
        return self.checkout

    def _local_checks(self, cfg, _checkout, **kwargs):
        self.local_calls.append(kwargs)
        return {"head": HEAD, "committed_diff_base": BASE,
                "checks": [{"command": list(command), "exit_code": 0,
                            "log_sha256": "f" * 64, "log": "check.log"}
                           for command in cfg.checks],
                "image_id": self.image, "result": "passed"}

    def _git(self, _path, *args, **_kwargs):
        if args[:2] == ("rev-parse", "HEAD"):
            return HEAD
        if args[:2] == ("worktree", "add"):
            Path(args[3]).mkdir(parents=True)
        if args[:2] == ("worktree", "remove"):
            shutil.rmtree(args[2])
        return ""

    def _review(self):
        return review.review(self.cfg, 19)

    def _merge(self):
        return review.merge(self.cfg, 19)

    def test_unchanged_policy_and_image_allow_merge(self):
        record = self._review()
        self.assertEqual(record["ci"]["image_id"], IMAGE)
        self.assertEqual(len(record["ci"]["checks"]), 2)
        result = self._merge()
        self.assertEqual(result["merge_commit"], MERGED)
        self.assertEqual([write[1] for write in self.gh.writes],
                         ["ready", "merge"])
        self.assertEqual(self.local_calls[-1], {"image_id": IMAGE})

    def test_added_removed_and_reordered_checks_invalidate_review(self):
        self._review()
        for changed in (CHECKS + (("python", "-V"),), CHECKS[:1],
                        CHECKS[::-1], (("python", "-m", "pytest"), CHECKS[1])):
            with self.subTest(checks=changed):
                self.cfg = replace(self.cfg, checks=changed)
                with self.assertRaises(runtime.WorkflowError):
                    self._merge()
                self.assertEqual(self.gh.writes, [])

    def test_retagged_image_invalidates_review_before_ready(self):
        self._review()
        self.image = OTHER_IMAGE
        with self.assertRaises(runtime.WorkflowError):
            self._merge()
        self.assertEqual(self.gh.writes, [])

    def test_moved_head_invalidates_review_before_ready(self):
        self._review()
        self.gh.head = "f" * 40

        with self.assertRaises(runtime.WorkflowError):
            self._merge()
        self.assertEqual(self.gh.writes, [])

    def test_missing_or_malformed_old_policy_evidence_is_rejected(self):
        self._review()
        original = review._load(self.cfg, 19)
        for key, value in (("checks_sha256", None), ("image_id", None),
                           ("checks_sha256", 7), ("image_id", "demo:latest")):
            with self.subTest(key=key, value=value):
                record = dict(original)
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
                review._save(self.cfg, 19, record)
                with self.assertRaises(runtime.WorkflowError):
                    self._merge()
                self.assertEqual(self.gh.writes, [])

    def test_unavailable_reviewed_image_blocks_merge(self):
        self._review()
        self.image_error = runtime.WorkflowError("Docker image unavailable")
        with self.assertRaises(runtime.WorkflowError):
            self._merge()
        self.assertEqual(self.gh.writes, [])

    def test_image_retagged_while_making_draft_ready_blocks_merge(self):
        self._review()
        self.gh.on_ready = lambda: setattr(self, "image", OTHER_IMAGE)
        with self.assertRaises(runtime.WorkflowError):
            self._merge()
        self.assertEqual([write[1] for write in self.gh.writes], ["ready"])

    def test_check_policy_changed_while_making_draft_ready_blocks_merge(self):
        self._review()
        self.gh.on_ready = lambda: object.__setattr__(self.cfg, "checks", CHECKS[::-1])
        with self.assertRaises(runtime.WorkflowError):
            self._merge()
        self.assertEqual([write[1] for write in self.gh.writes], ["ready"])


if __name__ == "__main__":
    unittest.main()
