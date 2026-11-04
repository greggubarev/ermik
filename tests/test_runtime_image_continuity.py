"""A task keeps its recorded container image through execution and recovery."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik import runtime, state
from ermik.config import Config


BODY = """## Goal
Create one file.

## Allowed paths
src/example.py

## Acceptance
The file contains a greeting.
"""
BASE = "a" * 40
HEAD = "b" * 40
BRANCH = "work/issue-1-delivery"
IMAGE_A = "sha256:" + "c" * 64
IMAGE_B = "sha256:" + "d" * 64
CONFIG = "sha256:" + "e" * 64


class StopBeforeWorker(Exception):
    pass


class Hub:
    def issue(self, _number):
        return {"body": BODY, "title": "Example", "labels": []}

    def pr(self, _number):
        return {"number": 10, "headRefOid": HEAD, "isDraft": True,
                "changedFiles": 1, "files": [{"path": "src/example.py"}],
                "body": f"Closes #1\n\nExecution image: `{IMAGE_A}`\n"
                        f"Execution image config: `{CONFIG}`\n"}

    def find_branch_pr(self, _branch):
        return 10


class RuntimeImageContinuityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main", (), 0)
        self.checkout = self.cfg.workspace / "jobs" / "issue-1"
        self.checkout.mkdir(parents=True)
        self.spec = runtime.parse_issue(BODY)
        portable = patch.object(runtime, "_portable_image_digest", return_value=CONFIG)
        portable.start()
        self.addCleanup(portable.stop)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,'running',?)",
                         (1, BRANCH, BASE, '["src/example.py"]'))

    def _perform_until_worker(self, *, correction=False, resolver):
        launched = []

        def docker(_cfg, _checkout, _command, **kwargs):
            with state.connect(self.cfg.state_dir) as conn:
                saved = state.job(conn, 1)["image_id"]
            launched.append((kwargs["image_id"], saved))
            raise StopBeforeWorker

        def git(_checkout, *args, **_kwargs):
            if args[:2] == ("rev-parse", "HEAD"):
                return HEAD
            if args[:2] == ("rev-parse", "origin/main"):
                return BASE
            return ""

        with patch.object(runtime, "_github", return_value=Hub()), patch.object(
                runtime, "_checkout", return_value=self.checkout), patch.object(
                runtime, "_remove_checkout"), patch.object(
                runtime, "_resolve_image_id", side_effect=resolver) as resolve, patch.object(
                runtime, "_docker", side_effect=docker), patch.object(
                runtime, "main_sha", return_value=BASE), patch.object(
                runtime, "_require_pr"), patch.object(runtime, "git", side_effect=git):
            with self.assertRaises(StopBeforeWorker):
                runtime._perform(self.cfg, 1, BRANCH, self.spec,
                                 correction_note="Apply requested change" if correction else None,
                                 pr_number=10 if correction else None,
                                 expected_base=BASE,
                                 expected_head=HEAD if correction else None)
        return launched, [call.args[0] for call in resolve.call_args_list]

    def test_first_image_binding_is_committed_before_worker_launch(self):
        launches, resolutions = self._perform_until_worker(
            resolver=lambda _reference: IMAGE_A)
        self.assertEqual(launches, [(IMAGE_A, IMAGE_A)])
        self.assertEqual(resolutions, [self.cfg.image, IMAGE_A])

    def test_retry_and_correction_use_saved_image_after_tag_moves(self):
        with state.connect(self.cfg.state_dir) as conn:
            state.bind_image(conn, 1, IMAGE_A)

        def resolver(reference):
            return IMAGE_B if reference == self.cfg.image else IMAGE_A

        for correction in (False, True):
            with self.subTest(correction=correction):
                launches, resolutions = self._perform_until_worker(
                    correction=correction, resolver=resolver)
                self.assertEqual(launches, [(IMAGE_A, IMAGE_A)])
                self.assertNotIn(self.cfg.image, resolutions)

    def _rebuild(self, image_id):
        row = {"issue": 1, "branch": BRANCH, "allowed_json": json.dumps(["src/example.py"]),
               "last_model": None, "image_id": image_id}
        pr = {"number": 10, "isDraft": True, "changedFiles": 1,
              "files": [{"path": "src/example.py"}],
              "body": f"Closes #1\n\nExecution image: `{IMAGE_A}`\n"
                      f"Execution image config: `{CONFIG}`\n",
              "headRefOid": HEAD}
        source = self.cfg.workspace / "source"
        source.mkdir(parents=True)

        def git(_checkout, *args, **_kwargs):
            if args[:2] == ("rev-parse", f"refs/heads/{BRANCH}"):
                return HEAD
            if args[:1] == ("merge-base",):
                return BASE
            if args[:2] == ("diff", "--name-only"):
                return "src/example.py"
            return ""

        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "git", side_effect=git), patch.object(
                runtime, "local_checks", return_value={"result": "passed",
                                                      "image_id": IMAGE_A,
                                                      "image_config_digest": CONFIG}) as checks:
            if image_id is None:
                with self.assertRaises(runtime.WorkflowError):
                    runtime._rebuild_published_evidence(self.cfg, row, pr)
                checks.assert_not_called()
                return None
            result = runtime._rebuild_published_evidence(self.cfg, row, pr)
            checks.assert_called_once_with(self.cfg,
                                           self.cfg.workspace / "recovery" / "issue-1",
                                           image_id=IMAGE_A)
            return result

    def test_crash_recovery_checks_exact_head_with_saved_image(self):
        result = self._rebuild(IMAGE_A)
        self.assertEqual(result["head"], HEAD)
        self.assertEqual(result["ci"]["image_id"], IMAGE_A)
        self.assertEqual(result["ci"]["image_config_digest"], CONFIG)

    def test_crash_recovery_rejects_missing_image_binding(self):
        self._rebuild(None)

    def test_lost_push_response_reuses_binding_and_rejects_missing_binding(self):
        source = self.cfg.workspace / "source"
        source.mkdir(parents=True)
        row = {"issue": 1, "branch": BRANCH, "status": "running", "pr": None,
               "base_sha": BASE, "last_model": None, "image_id": IMAGE_A}
        hub = Hub()

        def git(_checkout, *args, **_kwargs):
            if args[:2] == ("rev-parse", f"refs/heads/{BRANCH}"):
                return HEAD
            if args[:1] == ("merge-base",):
                return BASE
            if args[:2] == ("diff", "--name-only"):
                return "src/example.py"
            return ""

        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "git", side_effect=git), patch.object(
                runtime, "main_sha", return_value=BASE), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "_require_initial_draft"), patch.object(
                runtime, "local_checks", return_value={"result": "passed",
                                                      "image_id": IMAGE_A,
                                                      "image_config_digest": CONFIG}) as checks:
            result = runtime._recover_unacknowledged_push(
                hub, self.cfg, row, hub.issue(1), self.spec,
                f"{HEAD} refs/heads/{BRANCH}")
            self.assertEqual(result["ci"]["image_id"], IMAGE_A)
            checks.assert_called_once_with(self.cfg,
                                           self.cfg.workspace / "recovery" / "issue-1",
                                           image_id=IMAGE_A)
            checks.reset_mock()
            row["image_id"] = None
            with self.assertRaises(runtime.WorkflowError):
                runtime._recover_unacknowledged_push(
                    hub, self.cfg, row, hub.issue(1), self.spec,
                    f"{HEAD} refs/heads/{BRANCH}")
            checks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
