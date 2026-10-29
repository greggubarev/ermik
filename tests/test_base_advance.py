"""Corrections remain bound to their PR when the base branch advances."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ermik import runtime, state
from ermik.config import Config
from ermik.github import GitHub


BODY = """## Goal
Update the example.

## Allowed paths
src/example.py

## Acceptance
The example is updated.
"""
BRANCH = "work/issue-1-delivery"


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


class BaseAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.mirror = root / "work" / "source"
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main", (), 0)
        command("git", "init", "--bare", str(self.remote))
        command("git", "clone", str(self.remote), str(self.seed))
        command("git", "-C", str(self.seed), "config", "user.name", "Repository owner")
        command("git", "-C", str(self.seed), "config", "user.email", "owner@example.invalid")
        (self.seed / "src").mkdir()
        (self.seed / "src" / "example.py").write_text("value = 0\n", encoding="utf-8")
        command("git", "-C", str(self.seed), "add", "-A")
        command("git", "-C", str(self.seed), "commit", "-m", "Initial")
        command("git", "-C", str(self.seed), "branch", "-M", "main")
        command("git", "-C", str(self.seed), "push", "origin", "main")
        self.base = command("git", "-C", str(self.seed), "rev-parse", "HEAD")
        command("git", "-C", str(self.seed), "checkout", "-b", BRANCH)
        (self.seed / "src" / "example.py").write_text("value = 1\n", encoding="utf-8")
        command("git", "-C", str(self.seed), "commit", "-am", "First delivery")
        command("git", "-C", str(self.seed), "push", "origin", BRANCH)
        self.head = command("git", "-C", str(self.seed), "rev-parse", "HEAD")
        command("git", "-C", str(self.seed), "checkout", "main")
        command("git", "clone", "--no-checkout", str(self.remote), str(self.mirror))
        command("git", "-C", str(self.mirror), "config", "user.name", "Repository owner")
        command("git", "-C", str(self.mirror), "config", "user.email", "owner@example.invalid")

    def tearDown(self):
        self.temp.cleanup()

    def advance_main(self, *, conflict=False):
        path = self.seed / ("src/example.py" if conflict else "README.md")
        path.write_text("value = 2\n" if conflict else "another PR\n", encoding="utf-8")
        command("git", "-C", str(self.seed), "add", "-A")
        command("git", "-C", str(self.seed), "commit", "-m", "Another PR")
        command("git", "-C", str(self.seed), "push", "origin", "main")
        return command("git", "-C", str(self.seed), "rev-parse", "HEAD")

    def perform(self, *, mutate_remote=False, rebase_only=False, bad_files=False,
                no_edit=False):
        outer = self

        class Hub:
            def __init__(self, repo):
                self.repo = repo

            def issue(self, number):
                return {"body": BODY, "title": "Example",
                        "labels": [{"name": "queue:worker"}, {"name": "state:in-progress"}]}

            def pr(self, number):
                return {"number": number, "state": "OPEN", "headRefName": BRANCH,
                        "headRefOid": command("git", "--git-dir", str(outer.remote),
                                              "rev-parse", f"refs/heads/{BRANCH}"),
                        "baseRefName": "main",
                        "changedFiles": 1,
                        "files": [{"path": "src/other.py" if bad_files else "src/example.py"}],
                        "headRepository": {"nameWithOwner": "owner/repo"}}

            validate_pr_identity = GitHub.validate_pr_identity

        def worker(_cfg, checkout, _command, **_kwargs):
            if not rebase_only and not no_edit:
                (checkout / "src" / "example.py").write_text("value = 3\n", encoding="utf-8")
            if mutate_remote:
                command("git", "-C", str(self.seed), "checkout", BRANCH)
                (self.seed / "src" / "example.py").write_text("value = 4\n", encoding="utf-8")
                command("git", "-C", str(self.seed), "commit", "-am", "External update")
                command("git", "-C", str(self.seed), "push", "origin", BRANCH)

        def fetch(_cfg):
            command("git", "-C", str(self.mirror), "fetch", "origin", "main", BRANCH)
            return self.mirror

        with patch.object(runtime, "GitHub", Hub), patch.object(runtime, "mirror", fetch), patch.object(
                runtime, "main_sha", side_effect=lambda gh, cfg: command(
                    "git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/main")), patch.object(
                runtime, "_docker", side_effect=worker) as docker_run, patch.object(
                runtime, "local_checks", return_value={"result": "passed"}) as checks, patch.object(
                runtime, "_approved_on_github", return_value=True):
            result = runtime._perform(self.cfg, 1, BRANCH, runtime.parse_issue(BODY),
                                      correction_note=("SEMI_REBASE_ONLY" if rebase_only else "Improve it"),
                                      pr_number=10, expected_base=self.base,
                                      expected_head=self.head)
            checks.assert_called_once()
            if rebase_only:
                docker_run.assert_not_called()
            return result

    def test_correction_rebases_onto_advanced_main_and_updates_same_pr(self):
        new_base = self.advance_main()
        result = self.perform()
        self.assertEqual(result["pr"], 10)
        self.assertEqual(result["base"], new_base)
        self.assertEqual(command("git", "--git-dir", str(self.remote), "rev-parse",
                                 f"refs/heads/{BRANCH}"), result["head"])
        self.assertEqual(command("git", "--git-dir", str(self.remote), "merge-base",
                                 new_base, result["head"]), new_base)

    def test_rebase_only_updates_same_pr_without_new_file_edit(self):
        new_base = self.advance_main()
        result = self.perform(rebase_only=True)
        self.assertEqual(result["paths"], ["src/example.py"])
        self.assertEqual(result["base"], new_base)
        self.assertNotEqual(result["head"], self.head)
        self.assertEqual(command("git", "--git-dir", str(self.remote), "rev-parse",
                                 f"refs/heads/{BRANCH}"), result["head"])

    def test_rebase_only_requires_matching_published_file_set(self):
        self.advance_main()
        with self.assertRaisesRegex(runtime.WorkflowError, "published PR paths differ"):
            self.perform(rebase_only=True, bad_files=True)
        self.assertEqual(command("git", "--git-dir", str(self.remote), "rev-parse",
                                 f"refs/heads/{BRANCH}"), self.head)

    def test_no_edit_without_explicit_rebase_only_request_blocks(self):
        self.advance_main()
        with self.assertRaisesRegex(runtime.WorkflowError, "no changed files"):
            self.perform(no_edit=True)

    def test_conflict_blocks_without_changing_remote_head(self):
        self.advance_main(conflict=True)
        with self.assertRaisesRegex(runtime.WorkflowError, "conflicts with advanced base"):
            self.perform()
        self.assertEqual(command("git", "--git-dir", str(self.remote), "rev-parse",
                                 f"refs/heads/{BRANCH}"), self.head)

    def test_changed_remote_head_blocks_publication(self):
        self.advance_main()
        with self.assertRaisesRegex(runtime.WorkflowError, "PR identity changed"):
            self.perform(mutate_remote=True)
        self.assertNotEqual(command("git", "--git-dir", str(self.remote), "rev-parse",
                                    f"refs/heads/{BRANCH}"), self.head)

    def test_due_retry_accepts_advanced_base_and_records_it(self):
        new_base = self.advance_main()
        spec = runtime.parse_issue(BODY)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,?,?)", (1, "work/issue-1-retry", self.base,
                                                "running", '["src/example.py"]'))
            runtime.record_failure(conn, 1, "transient", failure_id="first",
                                   resume_status="running", now=int(time.time()) - 120)
        hub = Mock()
        hub.issue.return_value = {"body": BODY, "labels": [
            {"name": "queue:worker"}, {"name": "state:in-progress"}]}
        hub.find_branch_pr.return_value = None
        hub.ready_issues.return_value = []
        result = {"issue": 1, "pr": 10, "branch": "work/issue-1-retry",
                  "base": new_base, "head": "c" * 40,
                  "ci": {"result": "passed"}}
        with patch.object(runtime, "GitHub", return_value=hub), patch.object(
                runtime, "_reconcile_interrupted"), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "_perform", return_value=result) as perform, patch.object(
                runtime, "resource_guard", return_value={"admit": True}):
            self.assertEqual(runtime.run_once(self.cfg), [result])
        self.assertEqual(perform.call_args.kwargs["expected_base"], self.base)
        with state.connect(self.cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["base_sha"]),
                             ("awaiting_review", new_base))

    def test_malformed_ready_issue_does_not_starve_next_issue(self):
        hub = Mock()
        hub.ready_issues.return_value = [1, 2]
        hub.issue.side_effect = lambda number: {
            "body": "not a valid task" if number == 1 else BODY,
            "labels": [{"name": "queue:worker"}, {"name": "state:ready"}]}
        with state.connect(self.cfg.state_dir) as conn, patch.object(
                runtime, "resource_guard", return_value={"admit": True}), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "main_sha", return_value=self.base):
            claimed = runtime._claim_new(hub, self.cfg, conn)
            self.assertEqual(claimed[0], 2)
            self.assertIsNone(state.job(conn, 1))

    def test_poll_continues_after_redacted_workflow_error(self):
        output = io.StringIO()
        with patch.object(runtime, "run_once", side_effect=[
                runtime.WorkflowError("private details"), []]), patch.object(
                runtime.time, "sleep", side_effect=[None, KeyboardInterrupt]), redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                runtime.poll(self.cfg)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines, [{"status": "error", "error": "WorkflowError"}, []])
        self.assertNotIn("private details", output.getvalue())


if __name__ == "__main__":
    unittest.main()
