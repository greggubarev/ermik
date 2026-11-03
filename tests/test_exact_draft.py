"""Direct publication reconciles only the exact open draft PR."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik import runtime
from ermik.config import Config


BASE = "a" * 40
HEAD = "b" * 40
BRANCH = "work/issue-7-delivery"
BODY = "## Allowed paths\nsrc/a.py\n\n## Goal\nFix a\n\n## Acceptance\nWorks\n"


class Hub:
    def __init__(self, pr=None):
        self.existing = pr
        self.created = 0
        self.lose_response = False
        self.created_changes = {}

    def issue(self, _number):
        return {"body": BODY, "title": "Fix a", "labels": [
            {"name": "queue:worker"}, {"name": "state:in-progress"}]}

    def find_branch_pr(self, branch, _base=None):
        assert branch == BRANCH
        return 19 if self.existing else None

    def pr(self, _number):
        return self.existing

    def validate_pr_identity(self, pr, *, branch, base_branch, number=None,
                             head_sha=None, state=None, **_kwargs):
        if (pr["headRefName"] != branch or pr["baseRefName"] != base_branch
                or number != pr["number"] or head_sha != pr["headRefOid"]
                or pr["state"] != state):
            raise runtime.GitHubError("identity mismatch")

    def open_pr(self, branch, body, _title, base):
        self.created += 1
        self.existing = draft(**self.created_changes)
        self.existing["body"] = body
        if self.lose_response:
            raise runtime.GitHubError("PR response lost")
        return 19


def draft(**changes):
    return {"number": 19, "state": "OPEN", "isDraft": True,
            "headRefName": BRANCH, "headRefOid": HEAD,
            "baseRefName": "main", "baseRefOid": BASE,
            "body": "Closes #7\n\nLocal checks passed.",
            "changedFiles": 1, "files": [{"path": "src/a.py"}], **changes}


class ExactDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),), 1, 60, (), "main")
        self.spec = runtime.parse_issue(BODY)
        self.checkout = root / "work" / "jobs" / "issue-7"
        self.checkout.mkdir(parents=True)

    def perform(self, hub):
        def git(_path, *args, **_kwargs):
            if args[:2] == ("merge-base", "HEAD"):
                return BASE
            if args[:4] == ("diff", "--cached", "--name-only", "-z"):
                return "src/a.py\x00"
            if args[:2] == ("rev-parse", "HEAD"):
                return HEAD
            return ""

        with patch.object(runtime, "_github", return_value=hub), patch.object(
                runtime, "_checkout", return_value=self.checkout), patch.object(
                runtime, "_remove_checkout"), patch.object(
                runtime, "_docker"), patch.object(
                runtime, "_check_scope", return_value=["src/a.py"]), patch.object(
                runtime, "git", side_effect=git), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}), patch.object(
                runtime, "main_sha", return_value=BASE), patch.object(
                runtime, "_approved_on_github", return_value=True):
            return runtime._perform(self.cfg, 7, BRANCH, self.spec,
                                    correction_note=None, pr_number=None,
                                    expected_base=BASE, expected_head=None)

    def test_existing_exact_draft_is_reused_without_second_create(self):
        hub = Hub(draft())
        result = self.perform(hub)
        self.assertEqual(result["pr"], 19)
        self.assertEqual(hub.created, 0)

    def test_no_existing_pr_creates_one_draft(self):
        hub = Hub()
        result = self.perform(hub)
        self.assertEqual(result["pr"], 19)
        self.assertEqual(hub.created, 1)

    def test_lost_create_response_reuses_exact_draft_once(self):
        hub = Hub()
        hub.lose_response = True
        result = self.perform(hub)
        self.assertEqual(result["pr"], 19)
        self.assertEqual(hub.created, 1)

    def test_lost_create_response_refuses_ready_pr(self):
        hub = Hub()
        hub.lose_response = True
        hub.created_changes = {"isDraft": False}
        with self.assertRaisesRegex(runtime.WorkflowError, "exact open draft"):
            self.perform(hub)
        self.assertEqual(hub.created, 1)

    def test_ready_closed_and_modified_prs_are_not_reconciled(self):
        variants = ({"isDraft": False}, {"state": "CLOSED"},
                    {"baseRefOid": "c" * 40}, {"body": "Closes #8"},
                    {"headRefOid": "d" * 40})
        for change in variants:
            with self.subTest(change=change):
                hub = Hub(draft(**change))
                with self.assertRaises(runtime.WorkflowError):
                    self.perform(hub)
                self.assertEqual(hub.created, 0)


if __name__ == "__main__":
    unittest.main()
