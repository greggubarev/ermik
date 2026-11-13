"""Maintainer decision trail across review, correction, and merge retries."""

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ermik import review
from ermik.config import Config
from ermik.decision_trail import append_decision, read_decisions
from ermik.github import GitHubError
from ermik.runtime import WorkflowError


BASE = "a" * 40
HEADS = ("b" * 40, "c" * 40, "d" * 40)
IMAGE = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "9" * 64
BODY = "## Goal\nChange a file.\n\n## Allowed paths\nsrc/a.py\n\n## Acceptance\nWorks.\n"


class Hub:
    def __init__(self):
        self.head = HEADS[0]
        self.target = BASE
        self.comments_list = []
        self.issue_comments = []
        self.blocked = False
        self.next_comment_id = 100
        self.lose_comment = False
        self.writes = []

    def identity(self):
        return "maintainer"

    def pr(self, number):
        return {"number": number, "state": "OPEN", "isDraft": False,
                "baseRefName": "main", "baseRefOid": BASE,
                "headRefName": "work/issue-7-delivery", "headRefOid": self.head,
                "changedFiles": 1, "files": [{"path": "src/a.py"}],
                "body": (f"Closes #7\n\nExecution image: `{IMAGE}`\n"
                         f"Execution image config: `{CONFIG_DIGEST}`")}

    def issue(self, _number):
        return {"body": BODY, "labels": [{"name": "queue:worker"},
                                            {"name": "state:blocked" if self.blocked
                                             else "state:in-progress"}]}

    def validate_pr_identity(self, *_args, **_kwargs):
        pass

    def comments(self, number):
        return list(self.issue_comments if number == 7 else self.comments_list)

    def comment(self, _number, body):
        self.next_comment_id += 1
        self.comments_list.append({"id": self.next_comment_id,
                                   "body": body, "user": {"login": "maintainer"}})
        self.writes.append("comment")
        if self.lose_comment:
            self.lose_comment = False
            raise GitHubError("comment response lost")


class DecisionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cfg = Config("owner/repo", "maintainer", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main")
        self.hub = Hub()
        self.log_hash = "f" * 64
        self.checkout = root / "work" / "reviews" / "pr-19"
        for name, value in (("GitHub", self.hub),
                            ("_approved_on_github", True),
                            ("_resolve_image_id", IMAGE),
                            ("_portable_image_digest", CONFIG_DIGEST),
                            ("main_sha", lambda *_args: self.hub.target),
                            ("_checkout_pr", self._checkout),
                            ("verify_changed_tree", ["src/a.py"]),
                            ("local_checks", self._checks),
                            ("git", self._git)):
            p = (patch.object(review, name, side_effect=value) if callable(value)
                 else patch.object(review, name, return_value=value))
            p.start()
            self.addCleanup(p.stop)

    def _checkout(self, *_args):
        self.checkout.mkdir(parents=True)
        return self.checkout

    def _git(self, _path, *args):
        if args[:2] == ("rev-parse", "HEAD"):
            return self.hub.head
        if args[:2] == ("worktree", "remove"):
            shutil.rmtree(args[2])
        return ""

    def _checks(self, *_args, **_kwargs):
        return {"result": "passed", "head": self.hub.head,
                "committed_diff_base": BASE, "image_id": IMAGE,
                "image_config_digest": CONFIG_DIGEST,
                "checks": [{"command": ["true"], "exit_code": 0,
                            "log_sha256": self.log_hash, "log": "private.log"}]}

    def _decisions(self):
        return read_decisions(self.cfg, 19)["entries"]

    def _note(self, value="Please correct the negative case."):
        path = self.cfg.workspace / "note.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_two_correction_cycles_keep_each_head_and_do_not_duplicate_replay(self):
        for head in HEADS[:2]:
            self.hub.head = head
            review.review(self.cfg, 19)
            note = self._note()
            self.assertEqual(review.request_changes(self.cfg, 19, note)["request"], "posted")
            self.assertEqual(review.request_changes(self.cfg, 19, note)["request"],
                             "already-present")
        entries = self._decisions()
        self.assertEqual([(e["head"], e["decision"]) for e in entries],
                         [(head, decision) for head in HEADS[:2]
                          for decision in ("reviewed", "changes_requested")])
        self.assertEqual(self.hub.writes, ["comment", "comment"])

    def test_changed_proof_digest_keeps_prior_review(self):
        review.review(self.cfg, 19)
        first = self._decisions()[0]
        self.log_hash = "1" * 64
        review.review(self.cfg, 19)
        entries = self._decisions()
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[1]["evidence_sha256"], first["evidence_sha256"])

    def test_pending_correction_blocks_new_review_on_same_head(self):
        review.review(self.cfg, 19)
        review.request_changes(self.cfg, 19, self._note())
        before = list(self._decisions())
        with self.assertRaisesRegex(review.WorkflowError, "pending exact-head correction"):
            review.review(self.cfg, 19)
        self.assertEqual(self._decisions(), before)
        self.hub.head = HEADS[1]
        self.assertEqual(review.review(self.cfg, 19)["head"], HEADS[1])

    def test_lost_comment_response_reconciles_without_duplicate(self):
        review.review(self.cfg, 19)
        note = self._note()
        self.hub.lose_comment = True
        with self.assertRaises(GitHubError):
            review.request_changes(self.cfg, 19, note)
        self.assertEqual([e["decision"] for e in self._decisions()], ["reviewed"])
        self.assertEqual(review.request_changes(self.cfg, 19, note)["request"],
                         "already-present")
        self.assertEqual(self.hub.writes, ["comment"])
        self.assertEqual([e["decision"] for e in self._decisions()],
                         ["reviewed", "changes_requested"])

    def test_blocked_consumed_request_posts_one_bound_retry(self):
        review.review(self.cfg, 19)
        note = self._note("SEMI_REBASE_ONLY\n\nPlease correct the stale base.")
        review.request_changes(self.cfg, 19, note)
        old = self.hub.comments_list[-1]["id"]
        self.hub.blocked = True
        self.hub.target = HEADS[2]
        self.hub.issue_comments.append({
            "id": old + 1,
            "body": (f"ERMIK_CORRECTION_BLOCKED issue=7 pr=19 "
                     f"head={HEADS[0]} base={BASE} target={HEADS[2]} request={old} "
                     f"nonce={'n' * 22}\n\nExecution blocked."),
            "user": {"login": "controller"}})
        self.hub.next_comment_id = old + 1
        self.hub.target = HEADS[1]
        with self.assertRaisesRegex(WorkflowError, "receipt is missing"):
            review.request_changes(self.cfg, 19, note)
        self.hub.target = HEADS[2]
        self.assertEqual(review.request_changes(self.cfg, 19, note)["request"], "posted")
        self.assertIn(f"ERMIK_RETRY_OF request={old} receipt={old + 1}",
                      self.hub.comments_list[-1]["body"])
        self.assertIn("SEMI_REBASE_ONLY", self.hub.comments_list[-1]["body"])
        self.assertEqual(review.request_changes(self.cfg, 19, note)["request"],
                         "already-present")
        self.assertEqual(self.hub.writes, ["comment", "comment"])

    def test_blocked_retry_refuses_unconsumed_duplicate_and_uncertain_post(self):
        review.review(self.cfg, 19)
        note = self._note()
        review.request_changes(self.cfg, 19, note)
        old = self.hub.comments_list[-1]["id"]
        self.hub.blocked = True
        with self.assertRaisesRegex(WorkflowError, "receipt is missing"):
            review.request_changes(self.cfg, 19, note)
        self.hub.issue_comments.append({
            "id": old + 1,
            "body": (f"ERMIK_CORRECTION_BLOCKED issue=7 pr=19 "
                     f"head={HEADS[0]} base={BASE} target={BASE} "
                     f"request={old} nonce={'n' * 22}"),
            "user": {"login": "controller"}})
        self.hub.issue_comments.append({
            "id": old + 2,
            "body": (f"ERMIK_CORRECTION_BLOCKED issue=7 pr=19 "
                     f"head={HEADS[0]} base={BASE} target={BASE} "
                     f"request={old} nonce={'f' * 22}"),
            "user": {"login": "maintainer"}})
        with self.assertRaisesRegex(WorkflowError, "ambiguous"):
            review.request_changes(self.cfg, 19, note)
        self.hub.issue_comments.pop()
        self.hub.next_comment_id = old + 1
        self.hub.lose_comment = True
        with self.assertRaises(GitHubError):
            review.request_changes(self.cfg, 19, note)
        self.assertEqual(review.request_changes(self.cfg, 19, note)["request"],
                         "already-present")
        self.hub.comments_list.append({"id": old + 3,
                                       "body": self.hub.comments_list[-1]["body"],
                                       "user": {"login": "maintainer"}})
        with self.assertRaisesRegex(WorkflowError, "publication is uncertain"):
            review.request_changes(self.cfg, 19, note)

    def test_interrupted_review_append_reconciles_from_saved_record(self):
        with patch.object(review, "append_decision", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                review.review(self.cfg, 19)
        self.assertEqual(self._decisions(), [])
        self.assertEqual(review.request_changes(self.cfg, 19, self._note())["request"],
                         "posted")
        self.assertEqual([e["decision"] for e in self._decisions()],
                         ["reviewed", "changes_requested"])

    def test_new_head_review_backfills_interrupted_prior_review(self):
        with patch.object(review, "append_decision", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                review.review(self.cfg, 19)
        self.hub.head = HEADS[1]
        review.review(self.cfg, 19)
        self.assertEqual([(e["head"], e["decision"]) for e in self._decisions()],
                         [(HEADS[0], "reviewed"), (HEADS[1], "reviewed")])

    def test_changed_retry_note_backfills_observed_correction(self):
        review.review(self.cfg, 19)
        actual_append = review.append_decision

        def interrupted(cfg, record, decision):
            if decision == "changes_requested":
                raise OSError("interrupted after comment")
            return actual_append(cfg, record, decision)

        with patch.object(review, "append_decision", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted after comment"):
                review.request_changes(self.cfg, 19, self._note())
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            review.request_changes(self.cfg, 19,
                                   self._note("Please correct another case."))
        self.assertEqual(self.hub.writes, ["comment"])
        self.assertEqual([e["decision"] for e in self._decisions()],
                         ["reviewed", "changes_requested"])

    def test_interrupted_merge_intent_append_prevents_remote_merge(self):
        review.review(self.cfg, 19)
        actual_append = review.append_decision

        def interrupted(cfg, record, decision):
            if decision == "merge_intent":
                raise OSError("interrupted intent write")
            return actual_append(cfg, record, decision)

        with patch.object(review, "append_decision", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted intent write"):
                review.merge(self.cfg, 19)
        self.assertNotIn("merge_attempt", review._load(self.cfg, 19))
        self.assertEqual([e["decision"] for e in self._decisions()], ["reviewed"])
        self.assertEqual(self.hub.writes, [])

    def test_orphan_merge_intent_blocks_correction_comment(self):
        record = review.review(self.cfg, 19)
        append_decision(self.cfg, record, "merge_intent")
        with self.assertRaisesRegex(ValueError, "conflicting decisions"):
            review.request_changes(self.cfg, 19, self._note())
        self.assertEqual(self.hub.writes, [])

    def test_conflicting_decision_blocks_draft_ready_write(self):
        record = review.review(self.cfg, 19)
        append_decision(self.cfg, record, "changes_requested")
        original_pr = self.hub.pr
        with patch.object(self.hub, "pr", side_effect=lambda number: {
                **original_pr(number), "isDraft": True}), patch.object(
                self.hub, "command", create=True) as command:
            with self.assertRaisesRegex(ValueError, "conflicting decisions"):
                review.merge(self.cfg, 19)
        command.assert_not_called()

    def test_malformed_saved_proof_blocks_comment(self):
        record = review.review(self.cfg, 19)
        record["ci"]["checks"][0]["log_sha256"] = "bad"
        review._save(self.cfg, 19, record)
        with self.assertRaises(ValueError):
            review.request_changes(self.cfg, 19, self._note())
        self.assertEqual(self.hub.writes, [])


if __name__ == "__main__":
    unittest.main()
