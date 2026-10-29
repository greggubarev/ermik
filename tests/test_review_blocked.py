"""A maintainer can issue a fresh correction after a published job blocks."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ermik import review
from ermik.runtime import WorkflowError

BODY = """## Goal
Create one file.

## Allowed paths
src/example.py

## Acceptance
The file contains a greeting.
"""


class BlockedReviewRequestTests(unittest.TestCase):
    def test_only_correction_identity_accepts_blocked_issue(self):
        cfg = SimpleNamespace(maintainer="owner", base_branch="main")
        gh = Mock()
        gh.identity.return_value = "owner"
        gh.pr.return_value = {
            "state": "OPEN", "baseRefName": "main", "changedFiles": 1,
            "files": [{"path": "src/example.py"}],
            "headRefName": "work/issue-1-delivery", "body": "Closes #1",
        }
        gh.issue.return_value = {
            "body": BODY,
            "labels": [{"name": "queue:worker"}, {"name": "state:blocked"}],
        }
        with self.assertRaisesRegex(WorkflowError, "issue queue or state changed"):
            review._identity(cfg, gh, 10)
        pr, issue, _ = review._identity(cfg, gh, 10, allow_blocked=True)
        self.assertEqual((pr, issue), (gh.pr.return_value, 1))


if __name__ == "__main__":
    unittest.main()
