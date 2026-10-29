"""State transitions without GitHub writes or a Docker daemon."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik import state
from ermik import runtime
from ermik import review as review_module
from ermik import __main__ as cli


BODY = """## Goal
Create one file.

## Allowed paths
src/example.py

## Acceptance
The file contains a greeting.
"""
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


class FakeHub:
    issues = {}
    prs = {}
    notes = {}
    calls = []

    def __init__(self, repo):
        self.repo = repo

    def issue(self, number):
        value = self.issues[number]
        return {**value, "labels": [{"name": label} for label in value["names"]]}

    def label(self, number, *, add=None, remove=None):
        names = self.issues[number]["names"]
        if remove:
            names.discard(remove)
        if add:
            names.add(add)

    def ready_issues(self):
        return sorted(n for n, row in self.issues.items()
                      if {"queue:worker", "state:ready"}.issubset(row["names"]))

    def pr(self, number):
        return self.prs[number]

    def comments(self, number):
        return self.notes.get(number, [])

    def find_branch_pr(self, branch, base_branch=None):
        for number, pr in self.prs.items():
            if pr["headRefName"] == branch:
                return number
        return None

    def validate_pr_identity(self, pr, *, branch, base_branch, number=None,
                             head_sha=None, state=None, **_kwargs):
        if (pr["headRefName"] != branch or
                head_sha is not None and pr["headRefOid"] != head_sha or
                state is not None and pr["state"] != state):
            raise runtime.GitHubError("PR mismatch")

    def comment(self, number, body):
        self.calls.append(("comment", number, body))
        self.notes.setdefault(number, []).append({"id": len(self.notes.get(number, [])) + 1,
                                                    "body": body,
                                                    "user": {"login": "owner"}})

    def identity(self):
        return "owner"

    def command(self, args, **kwargs):
        self.calls.append(("command", args))
        raise AssertionError("merge command must not run")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),), 2, 60, (), "main")
        FakeHub.issues = {1: {"body": BODY, "title": "Example", "names":
                             {"queue:worker", "state:proposed"}}}
        FakeHub.prs = {}
        FakeHub.notes = {}
        FakeHub.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_admit_claim_review_correction_same_pr(self):
        with patch.object(runtime, "GitHub", FakeHub), patch.object(runtime, "main_sha", return_value=SHA_A):
            admitted = runtime.admit(self.cfg, 1)
            self.assertEqual(admitted["allowed_paths"], ("src/example.py",))
            self.assertIn("state:ready", FakeHub.issues[1]["names"])

            def perform(_cfg, issue, branch, spec, *, correction_note,
                        pr_number, expected_base, expected_head):
                head = SHA_B if pr_number is None else SHA_C
                FakeHub.prs[10] = {"state": "OPEN", "headRefName": branch,
                                   "headRefOid": head}
                self.assertEqual(expected_base, SHA_A)
                if pr_number is not None:
                    self.assertEqual(pr_number, 10)
                    self.assertEqual(expected_head, SHA_B)
                    self.assertIn("Please fix", correction_note)
                return {"issue": issue, "pr": 10, "branch": branch,
                        "head": head, "paths": ["src/example.py"], "ci": {"result": "passed"}}

            with patch.object(runtime, "_perform", side_effect=perform):
                first = runtime.run_once(self.cfg)
                self.assertEqual(first[0]["head"], SHA_B)
                with state.connect(self.cfg.state_dir) as conn:
                    self.assertEqual(state.job(conn, 1)["status"], "awaiting_review")

                FakeHub.notes[10] = [{"id": 99, "body":
                    f"SEMI_REWORK issue=1 head={SHA_B}\n\nPlease fix the negative case",
                    "user": {"login": "owner"}}]
                second = runtime.run_once(self.cfg)
                self.assertEqual(second[0]["head"], SHA_C)
                with state.connect(self.cfg.state_dir) as conn:
                    row = state.job(conn, 1)
                    self.assertEqual((row["pr"], row["status"], row["last_request"]),
                                     (10, "awaiting_review", 99))

    def test_blocked_job_keeps_scope_reserved(self):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(?,?,?,?,?)", (1, "work/issue-1-delivery", SHA_A,
                         "blocked", '["src/example.py"]'))
            self.assertTrue(state.conflicting(conn, ("src/example.py",)))

    def test_rebase_only_keeps_original_result_model(self):
        spec = runtime.parse_issue(BODY)
        FakeHub.issues[1]["names"] = {"queue:worker", "state:in-progress"}
        FakeHub.prs[10] = {"state": "OPEN", "headRefName": "work/issue-1-delivery",
                            "headRefOid": SHA_B}
        FakeHub.notes[1] = [{"id": 1, "body": runtime._admission_marker(1, spec),
                             "user": {"login": "owner"}}]
        FakeHub.notes[10] = [{"id": 101,
            "body": f"SEMI_REWORK issue=1 head={SHA_B}\n\nSEMI_REBASE_ONLY",
            "user": {"login": "owner"}}]
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,"
                         "allowed_json,result_model) VALUES(?,?,?,?,?,?,?,?)",
                         (1, "work/issue-1-delivery", SHA_A, SHA_B, 10,
                          "awaiting_review", '["src/example.py"]', "kimi-code/k3"))
        with patch.object(runtime, "GitHub", FakeHub), patch.object(
                runtime, "_cleanup_containers"), patch.object(
                runtime, "_reconcile_interrupted"), patch.object(
                runtime, "recover_stale_worktrees"), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "_require_pr"), patch.object(runtime, "_perform", return_value={
                    "issue": 1, "pr": 10, "head": SHA_C, "base": SHA_B,
                    "model": None, "paths": ["src/example.py"], "ci": {"result": "passed"}}):
            result = runtime.run_once(self.cfg)
        self.assertEqual(result[0]["model"], "kimi-code/k3")
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(state.job(conn, 1)["result_model"], "kimi-code/k3")

    def test_changed_issue_cannot_enter_correction(self):
        FakeHub.issues[1]["names"] = {"queue:worker", "state:in-progress"}
        FakeHub.issues[1]["body"] = BODY.replace("src/example.py", "src/other.py")
        FakeHub.prs[10] = {"state": "OPEN", "headRefName": "work/issue-1-delivery",
                            "headRefOid": SHA_B}
        FakeHub.notes[10] = [{"id": 99,
            "body": f"SEMI_REWORK issue=1 head={SHA_B}\n\nPlease fix it",
            "user": {"login": "owner"}}]
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, runtime.parse_issue(BODY).body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json) "
                         "VALUES(?,?,?,?,?,?,?)", (1, "work/issue-1-delivery", SHA_A,
                         SHA_B, 10, "awaiting_review", '["src/example.py"]'))
            FakeHub.notes[1] = [{"id": 1,
                "body": runtime._admission_marker(1, runtime.parse_issue(BODY)),
                "user": {"login": "owner"}}]
            self.assertIsNone(runtime._correction(FakeHub("owner/repo"), self.cfg, conn))
            self.assertEqual(state.job(conn, 1)["status"], "blocked")

    def test_fresh_exact_head_request_revives_blocked_published_job(self):
        spec = runtime.parse_issue(BODY)
        FakeHub.issues[1]["names"] = {"queue:worker", "state:blocked"}
        FakeHub.prs[10] = {"state": "OPEN", "headRefName": "work/issue-1-delivery",
                            "headRefOid": SHA_B}
        FakeHub.notes[1] = [{"id": 1, "body": runtime._admission_marker(1, spec),
                             "user": {"login": "owner"}}]
        FakeHub.notes[10] = [{"id": 101,
            "body": f"SEMI_REWORK issue=1 head={SHA_B}\n\nSEMI_REBASE_ONLY",
            "user": {"login": "owner"}}]
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json,last_request) "
                         "VALUES(?,?,?,?,?,?,?,?)", (1, "work/issue-1-delivery", SHA_A,
                         SHA_B, 10, "blocked", '["src/example.py"]', 100))
            conn.execute("INSERT INTO retry_state(issue,attempts,category,resume_status,"
                         "failure_id) VALUES(?,?,?,?,?)", (1, 3, "identity", "correcting", "failed"))
            with patch.object(runtime, "_route_provider", return_value="provider"):
                correction = runtime._correction(FakeHub("owner/repo"), self.cfg, conn)
            self.assertEqual(correction[2], 101)
            self.assertEqual(state.job(conn, 1)["status"], "correcting")
            self.assertIsNone(conn.execute("SELECT * FROM retry_state WHERE issue=1").fetchone())
            self.assertIn("state:in-progress", FakeHub.issues[1]["names"])
            self.assertNotIn("state:blocked", FakeHub.issues[1]["names"])

    def test_blocked_job_ignores_stale_or_wrong_head_request(self):
        spec = runtime.parse_issue(BODY)
        FakeHub.issues[1]["names"] = {"queue:worker", "state:blocked"}
        FakeHub.prs[10] = {"state": "OPEN", "headRefName": "work/issue-1-delivery",
                            "headRefOid": SHA_B}
        FakeHub.notes[1] = [{"id": 1, "body": runtime._admission_marker(1, spec),
                             "user": {"login": "owner"}}]
        FakeHub.notes[10] = [
            {"id": 100, "body": f"SEMI_REWORK issue=1 head={SHA_B}",
             "user": {"login": "owner"}},
            {"id": 101, "body": f"SEMI_REWORK issue=1 head={SHA_C}",
             "user": {"login": "owner"}},
            {"id": 102, "body": f"SEMI_REWORK issue=1 head={SHA_B}",
             "user": {"login": "other"}}]
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (1, spec.body_sha256, '["src/example.py"]'))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json,last_request) "
                         "VALUES(?,?,?,?,?,?,?,?)", (1, "work/issue-1-delivery", SHA_A,
                         SHA_B, 10, "blocked", '["src/example.py"]', 100))
            self.assertIsNone(runtime._correction(FakeHub("owner/repo"), self.cfg, conn))
            self.assertEqual(state.job(conn, 1)["status"], "blocked")
            self.assertNotIn("state:in-progress", FakeHub.issues[1]["names"])

    def test_ready_label_without_maintainer_admission_is_ignored(self):
        FakeHub.issues[1]["names"] = {"queue:worker", "state:ready"}
        with state.connect(self.cfg.state_dir) as conn:
            with patch.object(runtime, "main_sha", return_value=SHA_A):
                self.assertIsNone(runtime._claim_new(FakeHub("owner/repo"), self.cfg, conn))
            self.assertIsNone(state.job(conn, 1))

    def test_doctor_rejects_read_only_github_account(self):
        class ReadOnlyHub(FakeHub):
            def command(self, args, **kwargs):
                return '{"nameWithOwner":"owner/repo","defaultBranchRef":{"name":"main"}}'

            def api(self, endpoint):
                return {"permission": "read"}

        with patch.object(cli, "GitHub", ReadOnlyHub):
            with self.assertRaisesRegex(ValueError, "cannot publish"):
                cli.doctor(self.cfg)

    def test_maintainer_admission_reaches_separate_server_state(self):
        local_cfg = Config(self.cfg.repo, self.cfg.maintainer,
                           Path(self.tmp.name) / "maintainer-state",
                           Path(self.tmp.name) / "maintainer-work",
                           self.cfg.image, self.cfg.worker_command, self.cfg.checks,
                           1, 60, (), "main")
        with patch.object(runtime, "GitHub", FakeHub), patch.object(
                runtime, "main_sha", return_value=SHA_A):
            runtime.admit(local_cfg, 1)
            with state.connect(self.cfg.state_dir) as conn:
                claimed = runtime._claim_new(FakeHub("owner/repo"), self.cfg, conn)
                self.assertEqual(claimed[0:2], (1, "work/issue-1-delivery"))
                self.assertEqual(state.job(conn, 1)["status"], "running")

    def test_pending_rework_prevents_merge_of_old_review(self):
        FakeHub.issues[1]["names"] = {"queue:worker", "state:in-progress"}
        FakeHub.prs[10] = {"state": "OPEN", "headRefName": "work/issue-1-delivery",
                            "headRefOid": SHA_B, "baseRefOid": SHA_A,
                            "baseRefName": "main", "isDraft": True,
                            "changedFiles": 1, "files": [{"path": "src/example.py"}],
                            "body": "Closes #1"}
        FakeHub.notes[10] = [{"id": 99,
            "body": f"SEMI_REWORK issue=1 head={SHA_B}\n\nPlease fix it",
            "user": {"login": "owner"}}]
        record = {"issue": 1, "head": SHA_B, "base": SHA_A,
                  "body_sha256": runtime.parse_issue(BODY).body_sha256,
                  "paths": ["src/example.py"]}
        review_module._save(self.cfg, 10, record)
        with patch.object(review_module, "GitHub", FakeHub), patch.object(
                review_module, "main_sha", return_value=SHA_A):
            with self.assertRaisesRegex(runtime.WorkflowError, "pending exact-head"):
                review_module.merge(self.cfg, 10)
        self.assertFalse(FakeHub.calls)

    def test_real_git_worktree_commit_and_same_branch_correction(self):
        root = Path(self.tmp.name)
        remote = root / "remote.git"
        source = root / "source"

        def command(*args):
            return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()

        command("git", "init", "--bare", str(remote))
        command("git", "clone", str(remote), str(source))
        command("git", "-C", str(source), "config", "user.name", "Repository owner")
        command("git", "-C", str(source), "config", "user.email", "owner@example.invalid")
        (source / "README.md").write_text("example\n", encoding="utf-8")
        command("git", "-C", str(source), "add", "README.md")
        command("git", "-C", str(source), "commit", "-m", "Initial example")
        command("git", "-C", str(source), "branch", "-M", "main")
        command("git", "-C", str(source), "push", "-u", "origin", "main")
        base = command("git", "-C", str(source), "rev-parse", "HEAD")
        self.cfg.workspace.mkdir(parents=True)
        mirror = self.cfg.workspace / "source"
        command("git", "clone", "--no-checkout", str(remote), str(mirror))
        command("git", "-C", str(mirror), "config", "user.name", "Repository owner")
        command("git", "-C", str(mirror), "config", "user.email", "owner@example.invalid")
        branch = "work/issue-1-delivery"
        FakeHub.issues[1]["names"] = {"queue:worker", "state:in-progress"}
        FakeHub.notes[1] = [{"id": 1,
            "body": runtime._admission_marker(1, runtime.parse_issue(BODY)),
            "user": {"login": "owner"}}]

        class GitHubWithBranch(FakeHub):
            def open_pr(self, name, body, title, base_branch):
                self.asserted_name = name
                return 10

            def pr(self, number):
                head = command("git", "--git-dir", str(remote), "rev-parse",
                               f"refs/heads/{branch}")
                return {"state": "OPEN", "headRefName": branch, "headRefOid": head}

        def write_file(_cfg, checkout, _command, *, issue_file, network, timeout):
            target = checkout / "src" / "example.py"
            target.parent.mkdir(exist_ok=True)
            target.write_text("value = 1\n" if not target.exists() else "value = 2\n",
                              encoding="utf-8")

        with patch.object(runtime, "GitHub", GitHubWithBranch), patch.object(
                runtime, "mirror", return_value=mirror), patch.object(
                runtime, "_docker", side_effect=write_file), patch.object(
                runtime, "local_checks", return_value={"result": "passed"}), patch.object(
                runtime, "main_sha", return_value=base):
            spec = runtime.parse_issue(BODY)
            first = runtime._perform(self.cfg, 1, branch, spec,
                                     correction_note=None, pr_number=None,
                                     expected_base=base, expected_head=None)
            self.assertEqual(first["pr"], 10)
            second = runtime._perform(self.cfg, 1, branch, spec,
                                      correction_note="Please fix", pr_number=10,
                                      expected_base=base, expected_head=first["head"])
        self.assertNotEqual(first["head"], second["head"])
        self.assertEqual(command("git", "--git-dir", str(remote), "rev-parse",
                                 f"refs/heads/{branch}"), second["head"])


if __name__ == "__main__":
    unittest.main()
