"""Approved complexity is an execution boundary, not a task hint."""

from dataclasses import replace
from pathlib import Path
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
                    supported_efforts=("low", "medium", "high"))
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
                "headRefOid": ("c" if self.advance_head and self.pr_reads > 1
                               else "b") * 40, "body": "Closes #1"}

    def label(self, number, *, add, remove):
        if remove:
            self.names.discard(remove)
        if add:
            self.names.add(add)

    def comment(self, number, body):
        self.notes.append({"id": self.approval_id, "body": body,
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
                              "hard": (replace(GO, effort="high"),)})
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

    def test_old_request_cannot_readmit_blocked_pr(self):
        cfg, hub = self.readmission_hub(request_id=19)
        with patch.object(runtime, "_require_pr"):
            with state.connect(cfg.state_dir) as conn:
                self.assertIsNone(runtime._correction(hub, cfg, conn))
                row = state.job(conn, 1)
                self.assertEqual((row["status"], row["complexity"], row["route_key"]),
                                 ("blocked", "easy", "route:auto"))

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

    def test_blocked_prepublication_job_cannot_readmit_as_pr(self):
        cfg, hub = self.readmission_hub()
        hub.find_branch_pr = lambda branch: None
        with patch.object(runtime, "GitHub", return_value=hub):
            with self.assertRaisesRegex(runtime.WorkflowError,
                                        "pre-publication job requires separate recovery"):
                runtime.admit(cfg, 1, "medium")
