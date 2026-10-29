"""Deterministic response-loss checks at external workflow boundaries."""

from pathlib import Path
from dataclasses import replace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ermik import review, runtime, state
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


class FaultHub:
    def __init__(self, _repo):
        self.names = {"queue:worker", "state:proposed"}
        self.notes = {}
        self.prs = {}
        self.remote_head = None
        self.label_loss = False
        self.comment_loss = False
        self.pr_loss = False
        self.open_calls = 0
        self.label_calls = 0

    def identity(self):
        return "owner"

    def issue(self, _number):
        return {"body": BODY, "title": "Example",
                "labels": [{"name": name} for name in self.names]}

    def comments(self, number):
        return list(self.notes.get(number, []))

    def comment(self, number, body):
        rows = self.notes.setdefault(number, [])
        rows.append({"id": len(rows) + 1, "body": body,
                     "user": {"login": "owner"}})
        if self.comment_loss:
            self.comment_loss = False
            raise runtime.GitHubError("comment response lost")

    def label(self, _number, *, add=None, remove=None):
        self.label_calls += 1
        if remove:
            self.names.discard(remove)
        if add:
            self.names.add(add)
        if self.label_loss:
            self.label_loss = False
            raise runtime.GitHubError("label response lost")

    def ready_issues(self):
        return [1] if {"queue:worker", "state:ready"}.issubset(self.names) else []

    def find_branch_pr(self, branch, _base=None):
        return next((number for number, pr in self.prs.items()
                     if pr["headRefName"] == branch), None)

    def pr(self, number):
        return self.prs[number]

    def validate_pr_identity(self, pr, *, branch, base_branch, number=None,
                             head_sha=None, state=None, **_kwargs):
        if (pr["headRefName"] != branch or
                head_sha is not None and pr["headRefOid"] != head_sha or
                state is not None and pr["state"] != state):
            raise runtime.GitHubError("PR identity changed")

    def open_pr(self, branch, body, title, base):
        self.open_calls += 1
        self.prs[10] = {"number": 10, "state": "OPEN", "headRefName": branch,
                        "headRefOid": self.remote_head, "baseRefName": base,
                        "isDraft": True, "changedFiles": 1,
                        "files": [{"path": "src/example.py"}], "body": body}
        if self.pr_loss:
            self.pr_loss = False
            raise runtime.GitHubError("PR response lost")
        return 10


class FaultWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),),
                          1, 60, (), "main", (), 0)
        self.gh = FaultHub(self.cfg.repo)
        self.spec = runtime.parse_issue(BODY)

    def tearDown(self):
        self.temp.cleanup()

    def _approved(self):
        self.gh.notes[1] = [{"id": 1, "body": runtime._admission_marker(1, self.spec),
                             "user": {"login": "owner"}}]

    def _claim(self):
        with state.connect(self.cfg.state_dir) as conn, patch.object(
                runtime, "resource_guard", return_value={"admit": True}), patch.object(
                runtime, "main_sha", return_value=BASE):
            return runtime._claim_new(self.gh, self.cfg, conn)

    def _published_leftover(self):
        """Leave a real registered worktree as a terminated process would."""
        source = self.cfg.workspace / "source"
        source.parent.mkdir(parents=True)

        def command(*args):
            return subprocess.check_output(["git", *args], text=True,
                                           stderr=subprocess.DEVNULL).strip()

        command("init", str(source))
        command("-C", str(source), "config", "user.name", "Repository owner")
        command("-C", str(source), "config", "user.email", "owner@example.invalid")
        command("-C", str(source), "checkout", "-b", "main")
        (source / "src").mkdir()
        (source / "src" / "example.py").write_text("before\n", encoding="utf-8")
        command("-C", str(source), "add", "-A")
        command("-C", str(source), "commit", "-m", "Base")
        base = command("-C", str(source), "rev-parse", "HEAD")
        command("-C", str(source), "update-ref", "refs/remotes/origin/main", base)
        checkout = self.cfg.workspace / "jobs" / "issue-1"
        checkout.parent.mkdir()
        command("-C", str(source), "worktree", "add", "-b", BRANCH,
                str(checkout), base)
        (checkout / "src" / "example.py").write_text("after\n", encoding="utf-8")
        command("-C", str(checkout), "commit", "-am", "Deliver")
        head = command("-C", str(checkout), "rev-parse", "HEAD")
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        self.gh.remote_head = head
        self.gh.open_pr(BRANCH, "Closes #1", "Example", "main")
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, self.spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,'running',?)",
                         (1, BRANCH, base, '["src/example.py"]'))
        return source, checkout, base, head

    def test_admission_comment_response_loss_is_idempotent_on_retry(self):
        self.gh.comment_loss = True
        with patch.object(runtime, "GitHub", return_value=self.gh):
            with self.assertRaises(runtime.GitHubError):
                runtime.admit(self.cfg, 1)
            self.assertEqual(len(self.gh.notes[1]), 1)
            runtime.admit(self.cfg, 1)
        self.assertEqual(len(self.gh.notes[1]), 1)
        self.assertIn("state:ready", self.gh.names)

    def test_admission_label_response_loss_leaves_ready_once(self):
        self.gh.label_loss = True
        with patch.object(runtime, "GitHub", return_value=self.gh):
            with self.assertRaises(runtime.GitHubError):
                runtime.admit(self.cfg, 1)
            self.assertIn("state:ready", self.gh.names)
            runtime.admit(self.cfg, 1)
        self.assertEqual(len(self.gh.notes[1]), 1)
        self.assertEqual(self.gh.label_calls, 1)

    def test_claim_label_response_loss_recovers_one_job(self):
        self._approved()
        self.gh.names = {"queue:worker", "state:ready"}
        self.gh.label_loss = True
        with self.assertRaises(runtime.GitHubError):
            self._claim()
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["status"], "running")
        self.assertIn("state:in-progress", self.gh.names)
        with patch.object(runtime, "checked", return_value=""):
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "retry_wait")
        self.assertIsNone(self._claim())

    def _perform_with_fake_git(self, *, lose_push=False, lose_pr=False,
                               push_exit_error=False):
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        self.gh.pr_loss = lose_pr
        checkout = self.cfg.workspace / "jobs" / "issue-1"
        checkout.mkdir(parents=True)

        def git(_path, *args, **_kwargs):
            if args[:2] == ("merge-base", "HEAD"):
                return BASE
            if args[:4] == ("diff", "--cached", "--name-only", "-z"):
                return "src/example.py\x00"
            if args[:2] == ("rev-parse", "HEAD"):
                return HEAD
            if args and args[0] == "push":
                self.gh.remote_head = HEAD
                if lose_push:
                    raise runtime.TransientFailure("push response lost")
                if push_exit_error:
                    raise runtime.WorkflowError("git push command failed")
            return ""

        with patch.object(runtime, "GitHub", return_value=self.gh), patch.object(
                runtime, "_checkout", return_value=checkout), patch.object(
                runtime, "_remove_checkout"), patch.object(
                runtime, "_docker"), patch.object(
                runtime, "_check_scope", return_value=["src/example.py"]), patch.object(
                runtime, "git", side_effect=git), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}), patch.object(
                runtime, "main_sha", return_value=BASE):
            return runtime._perform(self.cfg, 1, BRANCH, self.spec,
                                    correction_note=None, pr_number=None,
                                    expected_base=BASE, expected_head=None)

    def test_pr_response_loss_finds_existing_pr_without_second_write(self):
        result = self._perform_with_fake_git(lose_pr=True)
        self.assertEqual((result["pr"], result["head"]), (10, HEAD))
        self.assertEqual(self.gh.open_calls, 1)
        evidence = self.cfg.state_dir / "evidence" / "1" / f"{HEAD}.json"
        self.assertTrue(evidence.is_file())

    def test_push_response_loss_does_not_open_pr(self):
        with self.assertRaises(runtime.TransientFailure):
            self._perform_with_fake_git(lose_push=True)
        self.assertEqual(self.gh.remote_head, HEAD)
        self.assertEqual(self.gh.open_calls, 0)
        self.assertFalse((self.cfg.state_dir / "evidence" / "1" / f"{HEAD}.json").exists())

    def test_nonzero_push_exit_is_reconciled_as_ambiguous(self):
        with self.assertRaises(runtime.TransientFailure):
            self._perform_with_fake_git(push_exit_error=True)
        self.assertEqual(self.gh.remote_head, HEAD)
        self.assertEqual(self.gh.open_calls, 0)

    def test_unacknowledged_push_rebuilds_proof_and_opens_one_pr(self):
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        self.gh.remote_head = HEAD
        row = {"issue": 1, "branch": BRANCH, "status": "running",
               "pr": None, "base_sha": BASE, "last_model": "kimi"}

        def fake_git(_path, *args, **_kwargs):
            if args[:2] == ("rev-parse", f"refs/heads/{BRANCH}"):
                return HEAD
            if args[:2] == ("merge-base", "HEAD"):
                return BASE
            if args[:2] == ("diff", "--name-only"):
                return "src/example.py"
            if args[:2] == ("worktree", "add"):
                Path(args[3]).mkdir(parents=True)
            return ""

        remote = f"{HEAD}\trefs/heads/{BRANCH}"
        with patch.object(runtime, "mirror", return_value=self.cfg.workspace), patch.object(
                runtime, "git", side_effect=fake_git), patch.object(
                runtime, "_remove_checkout"), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}), patch.object(
                runtime, "main_sha", return_value=BASE):
            result = runtime._recover_unacknowledged_push(
                self.gh, self.cfg, row, self.gh.issue(1), self.spec, remote)
        self.assertEqual((result["pr"], result["head"]), (10, HEAD))
        self.assertEqual(self.gh.open_calls, 1)

    def test_unacknowledged_push_rejects_remote_head_mismatch(self):
        row = {"issue": 1, "branch": BRANCH, "status": "running",
               "pr": None, "base_sha": BASE, "last_model": "kimi"}
        with patch.object(runtime, "mirror", return_value=self.cfg.workspace), patch.object(
                runtime, "git", return_value=HEAD), patch.object(
                runtime, "main_sha", return_value=BASE):
            with self.assertRaisesRegex(runtime.WorkflowError, "head or base changed"):
                runtime._recover_unacknowledged_push(
                    self.gh, self.cfg, row, self.gh.issue(1), self.spec,
                    f"{'c' * 40}\trefs/heads/{BRANCH}")
        self.assertEqual(self.gh.open_calls, 0)

    def test_retry_wait_push_loss_reconciles_without_second_execution(self):
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, self.spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,'retry_wait',?)",
                         (1, BRANCH, BASE, '["src/example.py"]'))
        proof = {"issue": 1, "pr": 10, "branch": BRANCH, "head": HEAD,
                 "base": BASE, "paths": ["src/example.py"],
                 "ci": {"result": "passed"}, "model": "kimi"}
        (self.cfg.workspace / "source").mkdir(parents=True)
        with patch.object(self.gh, "find_branch_pr", return_value=None), patch.object(
                runtime, "checked", return_value=f"{HEAD}\trefs/heads/{BRANCH}"), patch.object(
                runtime, "git", return_value=HEAD), patch.object(
                runtime, "_recover_unacknowledged_push", return_value=proof):
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                job = state.job(conn, 1)
                self.assertEqual((job["status"], job["pr"], job["head_sha"]),
                                 ("awaiting_review", 10, HEAD))
        evidence = self.cfg.state_dir / "evidence" / "1" / f"{HEAD}.json"
        self.assertTrue(evidence.is_file())

    def test_lost_pr_response_replays_broker_before_adopting_pr(self):
        self.cfg = replace(self.cfg, draft_pr_socket=Path(self.temp.name) / "broker.sock")
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        self.gh.remote_head = HEAD
        self.gh.open_pr(BRANCH, "Closes #1", "Example", "main")
        proof = {"issue": 1, "pr": 10, "branch": BRANCH, "head": HEAD,
                 "base": BASE, "paths": ["src/example.py"],
                 "ci": {"result": "passed"}, "model": "kimi"}
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, self.spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,'running',?)",
                         (1, BRANCH, BASE, '["src/example.py"]'))
            with patch.object(runtime, "_rebuild_published_evidence", return_value=proof), \
                    patch.object(runtime, "_draft_pr", side_effect=runtime.GitHubError(
                        "broker refused")) as broker:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "running")
                broker.assert_called_once()
            with patch.object(runtime, "_draft_pr", return_value=11):
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "blocked")

    def test_lost_pr_response_adopts_only_matching_broker_pr(self):
        self.cfg = replace(self.cfg, draft_pr_socket=Path(self.temp.name) / "broker.sock")
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        self.gh.remote_head = HEAD
        self.gh.open_pr(BRANCH, "Closes #1", "Example", "main")
        proof = {"issue": 1, "pr": 10, "branch": BRANCH, "head": HEAD,
                 "base": BASE, "paths": ["src/example.py"],
                 "ci": {"result": "passed"}, "model": "kimi"}
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, self.spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,'running',?)",
                         (1, BRANCH, BASE, '["src/example.py"]'))
            with patch.object(runtime, "_rebuild_published_evidence", return_value=proof), \
                    patch.object(runtime, "_draft_pr", return_value=10) as broker:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual((state.job(conn, 1)["status"], state.job(conn, 1)["pr"]),
                                 ("awaiting_review", 10))
                self.assertEqual(broker.call_args.kwargs["body_sha256"],
                                 self.spec.body_sha256)

    def test_crash_leftover_clean_exact_head_recovers_with_fresh_checks(self):
        source, checkout, base, head = self._published_leftover()
        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}) as checks:
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["pr"], row["head_sha"], row["base_sha"]),
                                 ("awaiting_review", 10, head, base))
        checks.assert_called_once()
        self.assertFalse(checkout.exists())

    def test_crash_after_push_recovers_leftover_and_opens_one_pr(self):
        source, checkout, base, head = self._published_leftover()
        self.gh.prs.clear()
        actual_checked = runtime.checked

        def checked(args, **kwargs):
            if args[:3] == ["git", "ls-remote", "--heads"]:
                return f"{head}\trefs/heads/{BRANCH}"
            return actual_checked(args, **kwargs)

        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "checked", side_effect=checked), patch.object(
                runtime, "main_sha", return_value=base), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}) as checks:
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["pr"], row["head_sha"]),
                                 ("awaiting_review", 10, head))
        checks.assert_called_once()
        self.assertEqual(self.gh.open_calls, 2)  # Fixture PR, then recovered PR.
        self.assertFalse(checkout.exists())

    def test_crash_leftover_dirty_checkout_blocks_and_preserves_files(self):
        source, checkout, _base, _head = self._published_leftover()
        extra = checkout / "untracked.txt"
        extra.write_text("preserve me\n", encoding="utf-8")
        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "local_checks") as checks:
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "blocked")
        checks.assert_not_called()
        self.assertEqual(extra.read_text(encoding="utf-8"), "preserve me\n")

    def test_crash_leftover_mismatched_remote_head_blocks(self):
        source, checkout, _base, _head = self._published_leftover()
        self.gh.prs[10]["headRefOid"] = "c" * 40
        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "local_checks") as checks:
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "blocked")
        checks.assert_not_called()
        self.assertTrue(checkout.exists())

    def test_crash_leftover_unregistered_directory_blocks(self):
        source, checkout, _base, _head = self._published_leftover()
        subprocess.check_call(["git", "-C", str(source), "worktree", "remove",
                               str(checkout)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        checkout.mkdir()
        (checkout / "note.txt").write_text("unknown checkout\n", encoding="utf-8")
        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "local_checks") as checks:
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "blocked")
        checks.assert_not_called()
        self.assertTrue((checkout / "note.txt").exists())

    def test_crash_leftover_broker_refusal_keeps_checkout_for_retry(self):
        source, checkout, _base, _head = self._published_leftover()
        self.cfg = replace(self.cfg, draft_pr_socket=Path(self.temp.name) / "broker.sock")
        with patch.object(runtime, "mirror", return_value=source), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}) as checks, \
                patch.object(runtime, "_draft_pr", side_effect=runtime.GitHubError(
                    "broker refused")) as broker:
            with state.connect(self.cfg.state_dir) as conn:
                runtime._reconcile_interrupted(self.gh, self.cfg, conn)
                self.assertEqual(state.job(conn, 1)["status"], "running")
        checks.assert_called_once()
        broker.assert_called_once()
        self.assertTrue(checkout.exists())

    def test_review_comment_read_loss_preserves_exact_head_request(self):
        self._approved()
        self.gh.names = {"queue:worker", "state:in-progress"}
        self.gh.remote_head = HEAD
        self.gh.open_pr(BRANCH, "Closes #1", "Example", "main")
        self.gh.notes[10] = [{"id": 7,
                              "body": f"SEMI_REWORK issue=1 head={HEAD}\n\nFix the case",
                              "user": {"login": "owner"}}]
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, self.spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json) "
                         "VALUES(?,?,?,?,?,'awaiting_review',?)",
                         (1, BRANCH, BASE, HEAD, 10, '["src/example.py"]'))
            original = self.gh.comments
            with patch.object(self.gh, "comments", side_effect=runtime.GitHubError("read lost")):
                self.assertIsNone(runtime._correction(self.gh, self.cfg, conn))
            self.assertEqual(state.job(conn, 1)["status"], "awaiting_review")
            conn.execute("UPDATE jobs SET review_next_check_at=0 WHERE issue=1")
            self.gh.comments = original
            correction = runtime._correction(self.gh, self.cfg, conn)
            self.assertEqual((correction[0]["issue"], correction[2]), (1, 7))
            self.assertEqual(state.job(conn, 1)["status"], "correcting")

    def test_review_comment_response_loss_reuses_exact_head_request(self):
        note_file = Path(self.temp.name) / "review.txt"
        note_file.write_text("Please fix the negative case.", encoding="utf-8")
        pr = {"headRefOid": HEAD, "baseRefOid": BASE}
        record = {"head": HEAD, "base": BASE}
        self.gh.comment_loss = True
        with patch.object(review, "GitHub", return_value=self.gh), patch.object(
                review, "_identity", return_value=(pr, 1, self.gh.issue(1))), patch.object(
                review, "_load", return_value=record):
            with self.assertRaises(runtime.GitHubError):
                review.request_changes(self.cfg, 10, note_file)
            result = review.request_changes(self.cfg, 10, note_file)
        self.assertEqual(result["request"], "already-present")
        self.assertEqual(len(self.gh.notes[10]), 1)
        self.assertIn(f"SEMI_REWORK issue=1 head={HEAD}", self.gh.notes[10][0]["body"])


if __name__ == "__main__":
    unittest.main()
