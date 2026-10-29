"""Issue route labels constrain provider selection without external services."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ermik import runtime, state
from ermik.config import Config
from ermik.policy import parse_issue
from ermik.providers import ModelCandidate, choose_model, claim_model, record_exhaustion


KIMI = ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY")
GO = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY")
BODY = "## Goal\nChange one file.\n\n## Allowed paths\nfile.txt\n\n## Acceptance\nThe file changes.\n"


class StopAttempt(RuntimeError):
    pass


class ReachedPush(RuntimeError):
    pass


class Hub:
    route_labels = []

    def __init__(self, repo):
        self.repo = repo

    def issue(self, number):
        return {"body": BODY, "title": f"Issue {number}",
                "labels": [{"name": label} for label in self.route_labels]}

    def pr(self, number):
        return {"number": number}


class QueueHub:
    def __init__(self, routes):
        self.routes = routes
        self.label_changes = []

    def ready_issues(self):
        return list(self.routes)

    def issue(self, number):
        body = BODY.replace("file.txt", f"file-{number}.txt")
        return {"body": body, "title": f"Issue {number}",
                "labels": [{"name": label} for label in
                           ("queue:worker", "state:ready", self.routes[number])]}

    def label(self, number, *, add, remove):
        self.label_changes.append((number, add, remove))


class CorrectionHub(QueueHub):
    def issue(self, number):
        issue = super().issue(number)
        issue["labels"][1]["name"] = "state:in-progress"
        return issue

    def pr(self, number):
        return {"state": "OPEN", "headRefOid": "b" * 40}

    def comments(self, number):
        return [{"id": number, "body": f"SEMI_REWORK issue={number} head={'b' * 40}",
                 "user": {"login": "owner"}}]


class RouteLabelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "worker:test", ("python3", "/worker.py"), (("true",),),
                          2, 60, ("KIMI_CODE_API_KEY", "OPENCODE_GO_API_KEY"),
                          "main", (KIMI, GO), routes={"route:kimi": "kimi-code",
                                                    "route:go": "opencode-go"})
        state.initialize(self.cfg.state_dir)

    def tearDown(self):
        self.temp.cleanup()

    def perform(self, route_labels, *, correction=False, docker_error=None,
                preferred_provider=None, reserved_elsewhere=frozenset()):
        Hub.route_labels = route_labels
        selected = []
        checkout = self.cfg.workspace / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)

        def docker(cfg, path, command, *, selected_model, **kwargs):
            selected.append(selected_model.ref)
            if docker_error == "quota_once" and len(selected) == 1:
                raise runtime.WorkerFailure(75)
            if docker_error is not None:
                if docker_error == "quota_once":
                    raise StopAttempt()
                raise docker_error
            raise StopAttempt()

        with patch.object(runtime, "GitHub", Hub), patch.object(
                runtime, "_checkout", return_value=checkout) as checkout_mock, patch.object(
                runtime, "_docker", side_effect=docker), patch.object(
                runtime, "git", side_effect=lambda _path, *args, **kwargs:
                    "b" * 40 if correction and args == ("rev-parse", "HEAD")
                    else "a" * 40), patch.object(
                runtime, "main_sha", return_value="a" * 40):
            with self.assertRaises((StopAttempt, runtime.WorkflowError)) as failure:
                runtime._perform(self.cfg, 1, "work/issue-1", parse_issue(BODY),
                                 correction_note="fix" if correction else None,
                                 pr_number=2 if correction else None,
                                 expected_base="a" * 40,
                                 expected_head="b" * 40 if correction else None,
                                 preferred_provider=preferred_provider,
                                 reserved_elsewhere=reserved_elsewhere)
        return selected, failure.exception, checkout_mock.call_count

    def test_explicit_routes_select_only_named_provider(self):
        for route, expected in (("route:kimi", KIMI.ref),
                                ("route:go", GO.ref)):
            with self.subTest(route=route):
                selected, error, _ = self.perform([route])
                self.assertEqual(selected, [expected])
                self.assertIsInstance(error, StopAttempt)

    def test_auto_and_absent_route_preserve_configured_order(self):
        for routes in ([], ["route:auto"]):
            with self.subTest(routes=routes):
                selected, error, _ = self.perform(routes)
                self.assertEqual(selected, [KIMI.ref])
                self.assertIsInstance(error, StopAttempt)

    def test_correction_uses_current_route(self):
        with patch.object(runtime, "_require_pr"):
            selected, error, _ = self.perform(["route:go"], correction=True)
        self.assertEqual(selected, [GO.ref])
        self.assertIsInstance(error, StopAttempt)

    def test_conflicting_routes_block_before_checkout(self):
        selected, error, checkout_count = self.perform(["route:kimi", "route:go"])
        self.assertEqual(selected, [])
        self.assertEqual(checkout_count, 0)
        self.assertIn("conflicting provider route labels", str(error))

    def test_unknown_route_blocks_before_checkout(self):
        selected, error, checkout_count = self.perform(["route:typo"])
        self.assertEqual(selected, [])
        self.assertEqual(checkout_count, 0)
        self.assertIn("is not configured", str(error))

    def test_generic_configured_route_selects_provider(self):
        self.cfg = replace(self.cfg, routes={"route:priority": "opencode-go"})
        selected, error, _ = self.perform(["route:priority"])
        self.assertEqual(selected, [GO.ref])
        self.assertIsInstance(error, StopAttempt)

    def test_explicit_route_does_not_fall_back_to_other_provider(self):
        selected, error, _ = self.perform(["route:kimi"],
                                          docker_error=runtime.WorkerFailure(75))
        self.assertEqual(selected, [KIMI.ref])
        self.assertIn("all configured providers", str(error))

    def test_auto_and_absent_route_fall_back_after_quota(self):
        for routes in ([], ["route:auto"]):
            with self.subTest(routes=routes):
                with state.connect(self.cfg.state_dir) as conn:
                    choose_model(conn, self.cfg.models)
                    conn.execute("DELETE FROM provider_cooldowns")
                selected, error, _ = self.perform(routes, docker_error="quota_once")
                self.assertEqual(selected, [KIMI.ref, GO.ref])
                self.assertIsInstance(error, StopAttempt)

    def test_preferred_auto_route_falls_back_after_quota(self):
        selected, error, _ = self.perform(["route:auto"], docker_error="quota_once",
                                          preferred_provider="opencode-go")
        self.assertEqual(selected, [GO.ref, KIMI.ref])
        self.assertIsInstance(error, StopAttempt)

    def test_quota_fallback_does_not_take_another_jobs_reserved_provider(self):
        selected, error, _ = self.perform(
            ["route:auto"], docker_error="quota_once",
            preferred_provider="opencode-go",
            reserved_elsewhere=frozenset({"kimi-code"}))
        self.assertEqual(selected, [GO.ref])
        self.assertIn("all configured providers", str(error))

    def test_same_provider_parallel_reservation_keeps_preferred_provider(self):
        selected, error, _ = self.perform(
            ["route:kimi"], preferred_provider="kimi-code",
            reserved_elsewhere=frozenset({"kimi-code"}))
        self.assertEqual(selected, [KIMI.ref])
        self.assertIsInstance(error, StopAttempt)

    def test_preferred_auto_route_passes_unchanged_pre_push_gate(self):
        checkout = self.cfg.workspace / "checkout"
        checkout.mkdir(parents=True)
        selected = []
        pushed = []

        class ApprovedHub:
            def issue(self, number):
                return {"body": BODY, "title": "Issue 1", "labels": [
                    {"name": "queue:worker"}, {"name": "state:in-progress"},
                    {"name": "route:auto"}]}

        def fake_docker(cfg, path, command, *, selected_model, **kwargs):
            selected.append(selected_model.ref)
            return None

        def fake_git(path, *args, **kwargs):
            if args[0] == "merge-base":
                return "a" * 40
            if args[:3] == ("diff", "--cached", "--name-only"):
                return "file.txt\x00"
            if args[:2] == ("rev-parse", "HEAD"):
                return "b" * 40
            if args[0] == "push":
                pushed.append(args)
                raise ReachedPush()
            return ""

        with patch.object(runtime, "_github", return_value=ApprovedHub()), patch.object(
                runtime, "_checkout", return_value=checkout), patch.object(
                runtime, "_docker", side_effect=fake_docker), patch.object(
                runtime, "_check_scope", return_value=["file.txt"]), patch.object(
                runtime, "local_checks", return_value={"ok": True}), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "main_sha", return_value="a" * 40), patch.object(
                runtime, "git", side_effect=fake_git), patch.object(
                runtime, "_remove_checkout"):
            with self.assertRaises(ReachedPush):
                runtime._perform(self.cfg, 1, "work/issue-1", parse_issue(BODY),
                                 correction_note=None, pr_number=None,
                                 expected_base="a" * 40, expected_head=None,
                                 preferred_provider="opencode-go")
        self.assertEqual(selected, [GO.ref])
        self.assertEqual(len(pushed), 1)

    def test_unconfigured_requested_provider_is_blocked(self):
        cfg = self.cfg
        self.cfg = replace(cfg, models=(GO,))
        selected, error, checkout_count = self.perform(["route:kimi"])
        self.assertEqual(selected, [])
        self.assertEqual(checkout_count, 0)
        self.assertIn("is not configured", str(error))

    def test_new_queue_skips_cooldown_route_and_claims_go(self):
        hub = QueueHub({1: "route:kimi", 2: "route:go"})
        with state.connect(self.cfg.state_dir) as conn:
            record_exhaustion(conn, KIMI, time.time() + 900)
            with patch.object(runtime, "resource_guard", return_value={"admit": True}), patch.object(
                    runtime, "_approved_on_github", return_value=True), patch.object(
                    runtime, "main_sha", return_value="a" * 40):
                claimed = runtime._claim_new(hub, self.cfg, conn)
            self.assertEqual(claimed[0], 2)
            self.assertIsNone(state.job(conn, 1))
            self.assertEqual(state.job(conn, 2)["status"], "running")
        self.assertEqual(hub.label_changes, [(2, "state:in-progress", "state:ready")])

    def test_new_queue_skips_busy_route_and_cycle_reservations(self):
        hub = QueueHub({1: "route:kimi", 2: "route:kimi", 3: "route:go"})
        with state.connect(self.cfg.state_dir) as conn:
            claim_model(conn, (KIMI,), "busy-kimi")
            with patch.object(runtime, "resource_guard", return_value={"admit": True}), patch.object(
                    runtime, "_approved_on_github", return_value=True), patch.object(
                    runtime, "main_sha", return_value="a" * 40):
                self.assertEqual(runtime._claim_new(hub, self.cfg, conn)[0], 3)
            self.assertIsNone(state.job(conn, 1))
            self.assertIsNone(state.job(conn, 2))
        hub = QueueHub({4: "route:kimi", 5: "route:kimi", 6: "route:go"})
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("DELETE FROM provider_leases")
            reserved = {}
            with patch.object(runtime, "resource_guard", return_value={"admit": True}), patch.object(
                    runtime, "_approved_on_github", return_value=True), patch.object(
                    runtime, "main_sha", return_value="a" * 40):
                self.assertEqual(runtime._claim_new(hub, self.cfg, conn, reserved)[0], 4)
                self.assertEqual(runtime._claim_new(hub, self.cfg, conn, reserved)[0], 6)
            self.assertEqual(reserved, {"kimi-code": 1, "opencode-go": 1})
            self.assertIsNone(state.job(conn, 5))

    def test_correction_stays_pending_when_route_unavailable(self):
        hub = CorrectionHub({1: "route:kimi", 2: "route:go"})
        with state.connect(self.cfg.state_dir) as conn:
            for number in (1, 2):
                spec = parse_issue(hub.issue(number)["body"])
                conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                             (number, spec.body_sha256, json.dumps(spec.allowed_paths)))
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (number, f"work/issue-{number}", "a" * 40, "b" * 40,
                              number, "awaiting_review", json.dumps(spec.allowed_paths)))
            record_exhaustion(conn, KIMI, time.time() + 900)
            reserved = {}
            with patch.object(runtime, "_require_pr"), patch.object(
                    runtime, "_approved_on_github", return_value=True):
                correction = runtime._correction(hub, self.cfg, conn, [10], reserved)
            self.assertEqual(correction[0]["issue"], 2)
            self.assertEqual(state.job(conn, 1)["status"], "awaiting_review")
            self.assertEqual(state.job(conn, 2)["status"], "correcting")
            self.assertEqual(reserved, {"opencode-go": 1})

    def test_concurrent_explicit_and_auto_honor_reserved_providers(self):
        hub = QueueHub({1: "route:kimi", 2: "route:auto"})
        with state.connect(self.cfg.state_dir) as conn:
            reserved = {}
            selected_providers = {}
            with patch.object(runtime, "resource_guard", return_value={"admit": True}), patch.object(
                    runtime, "_approved_on_github", return_value=True), patch.object(
                    runtime, "main_sha", return_value="a" * 40):
                self.assertEqual(runtime._claim_new(hub, self.cfg, conn,
                                                     reserved, selected_providers)[0], 1)
                self.assertEqual(runtime._claim_new(hub, self.cfg, conn,
                                                     reserved, selected_providers)[0], 2)
        self.assertEqual(selected_providers, {1: "kimi-code", 2: "opencode-go"})
        barrier = threading.Barrier(2, timeout=5)
        actual = {}
        lock = threading.Lock()

        def checkout(cfg, issue, branch, *, correction):
            path = cfg.workspace / f"issue-{issue}"
            path.mkdir(parents=True)
            return path

        def docker(cfg, path, command, *, selected_model, **kwargs):
            with lock:
                actual[int(path.name.split("-")[1])] = selected_model.ref
            barrier.wait()
            raise StopAttempt()

        def run(number):
            runtime._perform(self.cfg, number, f"work/issue-{number}",
                             parse_issue(hub.issue(number)["body"]),
                             correction_note=None, pr_number=None,
                             expected_base="a" * 40, expected_head=None,
                             preferred_provider=selected_providers[number])

        with patch.object(runtime, "_github", return_value=hub), patch.object(
                runtime, "_checkout", side_effect=checkout), patch.object(
                runtime, "_docker", side_effect=docker), patch.object(
                runtime, "git", return_value="a" * 40), patch.object(
                runtime, "_remove_checkout"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, number) for number in (2, 1)]
                for future in futures:
                    with self.assertRaises(StopAttempt):
                        future.result(timeout=10)
        self.assertEqual(actual, {1: KIMI.ref, 2: GO.ref})

    def test_run_once_passes_each_scheduled_provider_to_worker(self):
        hub = QueueHub({1: "route:kimi", 2: "route:auto"})
        barrier = threading.Barrier(2, timeout=5)
        selected = {}
        lock = threading.Lock()

        def perform(cfg, issue, branch, spec, *, preferred_provider, **kwargs):
            with lock:
                selected[issue] = preferred_provider
            barrier.wait()
            return {"issue": issue, "head": "b" * 40, "base": "a" * 40,
                    "pr": issue + 10, "model": preferred_provider}

        with patch.object(runtime, "_github", return_value=hub), patch.object(
                runtime, "_cleanup_containers"), patch.object(
                runtime, "_reconcile_interrupted"), patch.object(
                runtime, "recover_stale_worktrees"), patch.object(
                runtime, "resource_guard", return_value={"admit": True}), patch.object(
                runtime, "_approved_on_github", return_value=True), patch.object(
                runtime, "main_sha", return_value="a" * 40), patch.object(
                runtime, "_perform", side_effect=perform):
            results = runtime.run_once(self.cfg)
        self.assertEqual({result["issue"] for result in results}, {1, 2})
        self.assertEqual(selected, {1: "kimi-code", 2: "opencode-go"})


if __name__ == "__main__":
    unittest.main()
