"""Approved complexity is an execution boundary, not a task hint."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ermik import runtime, state
from ermik.config import Config
from ermik.policy import parse_issue
from ermik.providers import ModelCandidate, opencode_command


KIMI = ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY",
                      supported_efforts=("low", "medium", "high"))
GO = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY",
                    supported_efforts=("low", "medium", "high", "max"))
BODY = "## Goal\nChange a file.\n\n## Allowed paths\nfile.txt\n\n## Acceptance\nChanged.\n"
SPEC = parse_issue(BODY)
IMAGE = "sha256:" + "a" * 64


class Hub:
    route = "route:auto"
    complexity = "complexity:easy"
    marker = ""

    def __init__(self, repo):
        self.repo = repo

    def issue(self, number):
        return {"body": BODY, "title": "Task",
                "labels": [{"name": self.route}, {"name": self.complexity},
                           {"name": "queue:worker"}, {"name": "state:in-progress"}]}

    def comments(self, number):
        return [{"body": self.marker, "user": {"login": "owner"}}]

    def pr(self, number):
        return {"number": number, "state": "OPEN", "headRefOid": "b" * 40,
                "body": f"Closes #1\n\nExecution image: `{IMAGE}`\n"
                        f"Execution image config: `{IMAGE}`"}


class StopAttempt(RuntimeError):
    pass


class AdmitHub:
    names = {"queue:worker", "state:proposed"}
    notes = []

    def __init__(self, repo):
        self.repo = repo

    def identity(self):
        return "owner"

    def issue(self, number):
        return {"body": BODY, "title": "Task",
                "labels": [{"name": name} for name in self.names]}

    def label(self, number, *, add, remove):
        if remove:
            self.names.discard(remove)
        if add:
            self.names.add(add)

    def comment(self, number, body):
        self.notes.append({"body": body, "user": {"login": "owner"}})

    def comments(self, number):
        return self.notes


class ReadmitHub:
    def __init__(self, cfg, *, approval_id=20, request_id=21):
        self.cfg = cfg
        self.body = BODY
        self.names = {"queue:worker", "state:blocked",
                      "route:go", "complexity:medium"}
        self.approval_id = approval_id
        self.request_id = request_id
        self.notes = []
        self.pr_reads = 0
        self.advance_head = False
        self.cancel_on_request = False

    def identity(self):
        return "owner"

    def issue(self, number):
        return {"body": self.body, "title": "Task",
                "labels": [{"name": name} for name in self.names]}

    def comments(self, number):
        if number == 1:
            return self.notes
        if self.cancel_on_request:
            self.names.discard("queue:worker")
        return [{"id": self.request_id,
                 "body": f"SEMI_REWORK issue=1 head={'b' * 40}",
                 "user": {"login": "owner"}}]

    def find_branch_pr(self, branch):
        return 2

    def pr(self, number):
        self.pr_reads += 1
        return {"number": 2, "state": "OPEN", "isDraft": True,
                "createdAt": "2026-01-01T00:00:00Z",
                "headRefOid": ("c" if self.advance_head and self.pr_reads > 1
                               else "b") * 40, "body": "Closes #1"}

    def label(self, number, *, add, remove):
        if remove:
            self.names.discard(remove)
        if add:
            self.names.add(add)

    def comment(self, number, body):
        self.notes.append({"id": self.approval_id, "body": body,
                           "created_at": "2026-01-02T00:00:00Z",
                           "user": {"login": "owner"}})


class ComplexityProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "worker:test", ("worker",), (("true",),), 2, 60, (),
                          "main", (KIMI, GO),
                          complexity_profiles={
                              "easy": (replace(KIMI, effort="low"),),
                              "medium": (replace(GO, effort="medium"),),
                              "hard": (replace(GO, effort="max"),)})
        state.initialize(self.cfg.state_dir)
        Hub.route = "route:auto"
        Hub.complexity = "complexity:easy"
        Hub.marker = runtime._admission_marker(
            1, SPEC, self.cfg, Hub("owner/repo").issue(1))
        self.checkout = root / "work" / "checkout"
        self.checkout.mkdir(parents=True)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute(
                "INSERT INTO jobs(issue,branch,base_sha,status,allowed_json,image_id,"
                "route_key,complexity,profile_sha) VALUES(?,?,?,?,?,?,?,?,?)",
                (1, "work/issue-1", "a" * 40, "running", '["file.txt"]',
                 IMAGE, "route:auto", "easy", runtime._profile_sha(self.cfg, "easy")))

    def perform(self, *, correction=False, quota_once=False):
        selected = []

        def docker(cfg, path, command, *, selected_model, **kwargs):
            selected.append((selected_model.ref, selected_model.effort))
            if quota_once and len(selected) == 1:
                raise runtime.WorkerFailure(75)
            raise StopAttempt()

        with patch.object(runtime, "GitHub", Hub), patch.object(
                runtime, "_resolve_image_id", return_value=IMAGE), patch.object(
                runtime, "_portable_image_digest", return_value=IMAGE), patch.object(
                runtime, "_checkout", return_value=self.checkout) as checkout, patch.object(
                runtime, "_docker", side_effect=docker), patch.object(
                runtime, "_require_pr"), patch.object(
                runtime, "git", side_effect=lambda _path, *args, **kwargs:
                    "b" * 40 if correction and args == ("rev-parse", "HEAD")
                    else "a" * 40), patch.object(
                runtime, "main_sha", return_value="a" * 40):
            with self.assertRaises((StopAttempt, runtime.WorkflowError)) as failure:
                runtime._perform(self.cfg, 1, "work/issue-1", SPEC,
                                 correction_note="fix" if correction else None,
                                 pr_number=2 if correction else None,
                                 expected_base="a" * 40,
                                 expected_head="b" * 40 if correction else None)
        return selected, failure.exception, checkout.call_count

    def test_approved_tier_selects_only_its_effort(self):
        selected, error, _ = self.perform()
        self.assertEqual(selected, [(KIMI.ref, "low")], str(error))
        self.assertIsInstance(error, StopAttempt)
        self.assertIn("--variant", opencode_command(replace(KIMI, effort="low"), BODY))
        with state.connect(self.cfg.state_dir) as conn:
            row = conn.execute("SELECT model,effort,complexity FROM events "
                               "WHERE issue=1 AND kind='route_decision'").fetchone()
        self.assertEqual(tuple(row), (KIMI.ref, "low", "easy"))

    def test_go_max_variant_is_bound_to_hard_profile(self):
        from ermik.providers import model_spec
        import json
        chosen = self.cfg.complexity_profiles["hard"][0]
        self.assertEqual(json.loads(model_spec(chosen))["effort"], "max")
        self.assertIn("--variant", opencode_command(chosen, BODY))

    def test_admit_requires_explicit_tier_and_binds_route_and_profile(self):
        AdmitHub.names = {"queue:worker", "state:proposed", "route:auto"}
        AdmitHub.notes = []
        with patch.object(runtime, "GitHub", AdmitHub):
            with self.assertRaisesRegex(runtime.WorkflowError, "--complexity"):
                runtime.admit(self.cfg, 2)
            self.assertEqual(AdmitHub.notes, [])
            runtime.admit(self.cfg, 2, "easy")
        marker = AdmitHub.notes[0]["body"].splitlines()[0]
        self.assertIn("tier=easy route=route:auto profile=", marker)
        self.assertIn("complexity:easy", AdmitHub.names)

    def test_correction_keeps_approved_tier(self):
        selected, error, _ = self.perform(correction=True)
        self.assertEqual(selected, [(KIMI.ref, "low")], str(error))
        self.assertIsInstance(error, StopAttempt)

    def test_quota_fallback_stays_in_tier(self):
        self.cfg = replace(self.cfg, complexity_profiles={
            **self.cfg.complexity_profiles,
            "easy": (replace(KIMI, effort="low"), replace(GO, effort="low"))})
        Hub.marker = runtime._admission_marker(
            1, SPEC, self.cfg, Hub("owner/repo").issue(1))
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET profile_sha=? WHERE issue=1",
                         (runtime._profile_sha(self.cfg, "easy"),))
        selected, error, _ = self.perform(quota_once=True)
        self.assertEqual(selected, [(KIMI.ref, "low"), (GO.ref, "low")])
        self.assertIsInstance(error, StopAttempt)

    def test_changed_route_or_profile_blocks_before_checkout(self):
        Hub.route = "route:other"
        selected, error, checkout = self.perform()
        self.assertEqual((selected, checkout), ([], 0))
        self.assertIn("approved complexity changed", str(error))
        Hub.route = "route:auto"
        self.cfg = replace(self.cfg, complexity_profiles={
            **self.cfg.complexity_profiles,
            "easy": (replace(GO, effort="low"),)})
        selected, error, checkout = self.perform()
        self.assertEqual((selected, checkout), ([], 0))
        self.assertIn("approved complexity changed", str(error))

    def test_changed_tier_and_missing_approval_block(self):
        Hub.complexity = "complexity:hard"
        selected, _, checkout = self.perform()
        self.assertEqual((selected, checkout), ([], 0))
        Hub.complexity = "complexity:easy"
        Hub.marker = "untrusted approval"
        selected, _, checkout = self.perform()
        self.assertEqual((selected, checkout), ([], 0))

    def test_exhausted_tier_never_falls_back_to_hard(self):
        with state.connect(self.cfg.state_dir) as conn:
            from ermik.providers import record_exhaustion
            import time
            record_exhaustion(conn, KIMI, int(time.time()) + 600)
        selected, error, _ = self.perform()
        self.assertEqual(selected, [])
        self.assertIsInstance(error, runtime.ProviderCapacityUnavailable)

    def readmission_hub(self, *, approval_id=20, request_id=21):
        cfg = replace(self.cfg, routes={"route:go": "opencode-go"})
        hub = ReadmitHub(cfg, approval_id=approval_id, request_id=request_id)
        hub.comment(1, runtime._admission_marker(1, SPEC, cfg, hub.issue(1)))
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET status='blocked',pr=2,head_sha=?,"
                         "review_next_check_at=0 WHERE issue=1", ("b" * 40,))
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) "
                         "VALUES(?,?,?)", (1, SPEC.body_sha256, '["file.txt"]'))
        return cfg, hub

    def disposable_git(self, cfg):
        root = cfg.workspace.parent
        source = cfg.workspace / "source"
        remote = root / "remote.git"
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(source)], check=True,
                       capture_output=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (source / "file.txt").write_text("base\n", encoding="utf-8")
        git("add", "file.txt")
        git("commit", "-m", "base")
        git("remote", "add", "origin", str(remote))
        git("push", "origin", "main")
        base = git("rev-parse", "HEAD")
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET base_sha=? WHERE issue=1", (base,))
        return source, base, git

    def test_blocked_pr_readmission_requires_new_exact_head_request(self):
        cfg, hub = self.readmission_hub()
        with patch.object(runtime, "GitHub", return_value=hub), patch.object(
                runtime, "_require_pr"):
            result = runtime.admit(cfg, 1, "medium")
            self.assertEqual(result["complexity"], "medium")
            with state.connect(cfg.state_dir) as conn:
                pending = runtime._correction(hub, cfg, conn)
                self.assertIsNotNone(pending)
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["complexity"], row["route_key"],
                                  row["head_sha"], row["pr"]),
                                 ("correcting", "medium", "route:go", "b" * 40, 2))
                self.assertEqual(row["profile_sha"],
                                 runtime._profile_sha(cfg, "medium", "route:go"))

    def test_blocked_pr_cannot_change_frozen_task_set(self):
        cfg, hub = self.readmission_hub()
        hub.names.add("task-set:replacement")
        prior_notes = list(hub.notes)
        with patch.object(runtime, "GitHub", return_value=hub), patch.object(
                runtime, "_require_pr"):
            with self.assertRaisesRegex(runtime.WorkflowError,
                                        "blocked job task-set cannot change"):
                runtime.admit(cfg, 1, "medium", task_set="replacement")
        self.assertEqual(hub.notes, prior_notes)
        with state.connect(cfg.state_dir) as conn:
            self.assertIsNone(state.job(conn, 1)["task_set"])

    def test_old_request_cannot_readmit_blocked_pr(self):
        cfg, hub = self.readmission_hub(request_id=19)
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(hub, cfg, conn))
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["complexity"], row["route_key"]),
                                 ("blocked", "easy", "route:auto"))

    def test_restored_route_cannot_replay_pre_pr_approval(self):
        cfg, hub = self.readmission_hub()
        hub.names.discard("route:go")
        hub.names.discard("complexity:medium")
        hub.names.update(("route:auto", "complexity:easy"))
        hub.notes = [{"id": 10, "body": runtime._admission_marker(
            1, SPEC, cfg, hub.issue(1)), "created_at": "2025-12-31T00:00:00Z",
            "user": {"login": "owner"}}]
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(hub, cfg, conn))
                self.assertEqual(state.job(conn, 1)["status"], "blocked")
        with patch.object(runtime, "GitHub", return_value=hub), patch.object(
                runtime, "_require_pr"):
            runtime.admit(cfg, 1, "easy")
        self.assertEqual(len(hub.notes), 2)
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                conn.execute("UPDATE jobs SET review_next_check_at=0 WHERE issue=1")
                self.assertIsNotNone(runtime._correction(hub, cfg, conn))

    def test_changed_scope_cannot_readmit_existing_pr(self):
        cfg, hub = self.readmission_hub()
        hub.body = BODY.replace("file.txt", "other.txt")
        changed = parse_issue(hub.body)
        hub.notes = [{"id": 20, "body": runtime._admission_marker(
            1, changed, cfg, hub.issue(1)), "user": {"login": "owner"}}]
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(hub, cfg, conn))
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["complexity"]),
                                 ("blocked", "easy"))

    def test_changed_scope_rejects_before_any_admission_write(self):
        cfg, hub = self.readmission_hub()
        hub.body = BODY.replace("file.txt", "other.txt")
        names = set(hub.names)
        notes = list(hub.notes)
        with patch.object(runtime, "GitHub", return_value=hub), patch.object(
                runtime, "_require_pr"):
            with self.assertRaisesRegex(runtime.WorkflowError,
                                        "allowed paths changed"):
                runtime.admit(cfg, 1, "hard")
        self.assertEqual(hub.names, names)
        self.assertEqual(hub.notes, notes)

    def test_separate_maintainer_state_rejects_scope_change_before_write(self):
        cfg, hub = self.readmission_hub()
        cfg = replace(cfg, state_dir=cfg.state_dir.parent / "maintainer-state")
        hub.body = BODY.replace("file.txt", "other.txt")
        names = set(hub.names)
        notes = list(hub.notes)
        with patch.object(runtime, "GitHub", return_value=hub), patch.object(
                runtime, "_require_pr"):
            with self.assertRaisesRegex(runtime.WorkflowError,
                                        "allowed paths changed"):
                runtime.admit(cfg, 1, "hard")
        self.assertEqual(hub.names, names)
        self.assertEqual(hub.notes, notes)

    def test_changed_head_during_readmission_keeps_job_blocked(self):
        cfg, hub = self.readmission_hub()
        hub.advance_head = True
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(hub, cfg, conn))
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["complexity"], row["head_sha"]),
                                 ("blocked", "easy", "b" * 40))

    def test_canceled_issue_during_readmission_keeps_job_blocked(self):
        cfg, hub = self.readmission_hub()
        hub.cancel_on_request = True
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(hub, cfg, conn))
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["complexity"], row["route_key"]),
                                 ("blocked", "easy", "route:auto"))

    def test_blocked_prepublication_readmission_resumes_after_restart(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        source, base, _git = self.disposable_git(cfg)
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=? WHERE issue=1",
                         ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        self.assertEqual(hub.notes[-1]["body"].splitlines()[1],
                         hub.notes[-1]["body"].splitlines()[0].replace(
                             "SEMI_ADMIT", "ERMIK_READMIT", 1))
        # A new connection stands for a controller restart after approval.
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"], row["route_key"]),
                             ("retry_wait", "medium", "route:go"))
            self.assertEqual(row["last_request"], 22)
            from ermik.recovery import activate_claim, claim_due
            claim = claim_due(conn)[0]
            self.assertTrue(activate_claim(conn, claim))
            self.assertEqual(state.job(conn, 1)["status"], "running")

    def test_changed_prepublication_branch_blocks_without_rebinding(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        source, base, git = self.disposable_git(cfg)
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=? WHERE issue=1",
                         ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        git("branch", "work/issue-1-delivery")
        git("switch", "work/issue-1-delivery")
        (source / "file.txt").write_text("changed\n", encoding="utf-8")
        git("commit", "-am", "changed")
        git("push", "origin", "work/issue-1-delivery")
        git("switch", "main")
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"], row["route_key"]),
                             ("blocked", "easy", "route:auto"))
            self.assertIn("local branch changed", row["error"])

    def test_ref_moves_after_label_restores_block_and_allows_fresh_admit(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        source, base, git = self.disposable_git(cfg)
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=? WHERE issue=1",
                         ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        original_label = hub.label

        def move_ref(number, *, add, remove):
            original_label(number, add=add, remove=remove)
            if add == "state:in-progress":
                git("push", "origin", "main:refs/heads/work/issue-1-delivery")

        hub.label = move_ref
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"], row["attempts_count"],
                              row["pr"], row["last_request"]),
                             ("blocked", "easy", 0, None, 22))
            self.assertIn("ref changed", row["error"])
        self.assertIn("state:blocked", hub.names)
        self.assertNotIn("state:in-progress", hub.names)
        git("push", "origin", "--delete", "work/issue-1-delivery")
        hub.label = original_label
        hub.approval_id = 23
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["last_request"]), ("retry_wait", 23))

    def test_ambiguous_label_response_allows_fresh_admit(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        source, base, _git = self.disposable_git(cfg)
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=? WHERE issue=1",
                         ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        original_label = hub.label

        def uncertain_label(number, *, add, remove):
            if add == "state:in-progress":
                original_label(number, add=add, remove=remove)
            raise runtime.GitHubError("label response lost")

        hub.label = uncertain_label
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["attempts_count"], row["pr"],
                              row["last_request"]), ("blocked", 0, None, 22))
            self.assertIn("label write was unconfirmed", row["error"])
        self.assertIn("state:in-progress", hub.names)
        hub.label = original_label
        hub.approval_id = 23
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            self.assertEqual((state.job(conn, 1)["status"],
                              state.job(conn, 1)["last_request"]),
                             ("retry_wait", 23))

    def test_state_write_failure_restores_label_and_rolls_back(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        source, base, _git = self.disposable_git(cfg)
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=? WHERE issue=1",
                         ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base), \
                patch.object(state, "event", side_effect=sqlite3.OperationalError("write failed")):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"], row["attempts_count"],
                              row["pr"], row["last_request"]),
                             ("blocked", "easy", 0, None, 22))
            self.assertIsNone(conn.execute("SELECT 1 FROM retry_state WHERE issue=1").fetchone())
            self.assertIn("state write failed", row["error"])
        self.assertIn("state:blocked", hub.names)
        hub.approval_id = 23
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")

    def test_started_prepublication_attempt_cannot_replay(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=?,attempts_count=1 "
                         "WHERE issue=1", ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        with patch.object(runtime, "mirror") as mirror:
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
            mirror.assert_not_called()
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"]), ("blocked", "easy"))
            self.assertIn("unstarted execution", row["error"])

    def test_unacknowledged_remote_branch_keeps_policy_bound(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.approval_id = 22
        source, base, git = self.disposable_git(cfg)
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=? WHERE issue=1",
                         ("work/issue-1-delivery",))
        with patch.object(runtime, "GitHub", return_value=hub):
            runtime.admit(cfg, 1, "medium")
        git("push", "origin", "main:refs/heads/work/issue-1-delivery")
        with patch.object(runtime, "mirror", return_value=source), \
                patch.object(runtime, "main_sha", return_value=base):
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"]), ("blocked", "easy"))
            self.assertIn("remote branch or push is uncertain", row["error"])

    def test_historical_approval_cannot_readmit_blocked_job(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        hub.route = "route:auto"
        hub.names.discard("route:go")
        hub.names.add("route:auto")
        hub.names.discard("complexity:medium")
        hub.names.add("complexity:easy")
        old_marker = runtime._admission_marker(1, SPEC, cfg, hub.issue(1))
        hub.notes.insert(0, {"id": 10, "body": old_marker,
                             "user": {"login": "owner"}})
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET pr=NULL,head_sha=NULL,branch=?,route_key=?,"
                         "complexity=?,profile_sha=? WHERE issue=1",
                         ("work/issue-1-delivery", "route:go", "medium",
                          runtime._profile_sha(cfg, "medium", "route:go")))
        with patch.object(runtime, "mirror") as mirror:
            with state.connect(cfg.state_dir) as conn:
                runtime._readmit_prepublication(hub, cfg, conn)
            mirror.assert_not_called()
        with state.connect(cfg.state_dir) as conn:
            row = state.job(conn, 1)
            self.assertEqual((row["status"], row["complexity"], row["last_request"]),
                             ("blocked", "medium", 0))
            self.assertIn("fresh blocked-job approval is missing", row["error"])
