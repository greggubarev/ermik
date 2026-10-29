"""Controller to provider routing contracts without Docker or network calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.policy import parse_issue
from ermik.providers import ModelCandidate, choose_model
from ermik import runtime, state


KIMI = ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY")
GO = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY")
BODY = "## Goal\nChange one file.\n\n## Allowed paths\nfile.txt\n\n## Acceptance\nThe file changes.\n"
BASE = "a" * 40


class StopAttempt(RuntimeError):
    pass


class Hub:
    def __init__(self, repo):
        self.repo = repo

    def issue(self, number):
        return {"body": BODY, "title": f"Issue {number}"}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "worker:test", ("python3", "/worker.py"), (("true",),),
                          2, 60, ("KIMI_CODE_API_KEY", "OPENCODE_GO_API_KEY"),
                          "main", (KIMI, GO))
        state.initialize(self.cfg.state_dir)
        self.spec = parse_issue(BODY)

    def tearDown(self):
        self.temp.cleanup()

    def _perform(self, issue: int):
        return runtime._perform(self.cfg, issue, f"work/issue-{issue}", self.spec,
                                correction_note=None, pr_number=None,
                                expected_base=BASE, expected_head=None)

    def test_docker_passes_only_selected_credential_name(self):
        checkout = self.cfg.workspace / "checkout"
        checkout.mkdir(parents=True)
        task = self.cfg.state_dir / "task" / "issue.md"
        task.parent.mkdir()
        task.write_text(BODY, encoding="utf-8")
        calls = []

        def fake_run(args, timeout):
            calls.append(args)
            return 0, b"WORKER_OK\n"

        with patch.dict(os.environ, {"KIMI_CODE_API_KEY": "secret-a",
                                      "OPENCODE_GO_API_KEY": "secret-b"}), patch.object(
                                          runtime, "_run_capped", side_effect=fake_run):
            runtime._docker(self.cfg, checkout, self.cfg.worker_command,
                            issue_file=task, network=True, timeout=10,
                            selected_model=KIMI)
        args = calls[0]
        forwarded = [args[index + 1] for index, item in enumerate(args[:-1])
                     if item == "--env"]
        self.assertEqual(forwarded[:3], ["PYTHONDONTWRITEBYTECODE=1", "KIMI_CODE_API_KEY",
                                         "ERMIK_MODEL_REF=kimi-code/k3-256k"])
        self.assertTrue(forwarded[3].startswith("ERMIK_MODEL_SPEC={"))
        self.assertNotIn("secret-a", args)
        self.assertNotIn("secret-b", args)

    def test_concurrent_attempts_claim_different_accounts(self):
        gate = threading.Barrier(2, timeout=5)
        selected = []
        selected_lock = threading.Lock()

        def fake_checkout(cfg, issue, branch, *, correction):
            path = cfg.workspace / f"issue-{issue}"
            path.mkdir(parents=True)
            return path

        def fake_docker(cfg, checkout, command, *, selected_model, **kwargs):
            with selected_lock:
                selected.append(selected_model.ref)
            gate.wait()
            raise StopAttempt()

        with patch.object(runtime, "GitHub", Hub), patch.object(
                runtime, "_checkout", side_effect=fake_checkout), patch.object(
                runtime, "_docker", side_effect=fake_docker):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self._perform, issue) for issue in (1, 2)]
                for future in futures:
                    with self.assertRaises(StopAttempt):
                        future.result(timeout=10)
        self.assertCountEqual(selected, [KIMI.ref, GO.ref])

    def test_quota_falls_back_after_resetting_partial_edit(self):
        checkout = self.cfg.workspace / "repo"
        checkout.mkdir(parents=True)
        subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"], check=True)
        (checkout / "file.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-m", "Baseline"],
                       check=True, capture_output=True)
        selected = []

        def fake_docker(cfg, path, command, *, selected_model, **kwargs):
            selected.append(selected_model.ref)
            if len(selected) == 1:
                (path / "file.txt").write_text("partial\n", encoding="utf-8")
                (path / "extra.txt").write_text("untracked\n", encoding="utf-8")
                raise runtime.WorkerFailure(75)
            self.assertEqual((path / "file.txt").read_text(encoding="utf-8"), "baseline\n")
            self.assertFalse((path / "extra.txt").exists())
            raise StopAttempt()

        with patch.object(runtime, "GitHub", Hub), patch.object(
                runtime, "_checkout", return_value=checkout), patch.object(
                runtime, "_remove_checkout"), patch.object(
                runtime, "_docker", side_effect=fake_docker):
            with self.assertRaises(StopAttempt):
                self._perform(1)
        self.assertEqual(selected, [KIMI.ref, GO.ref])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(choose_model(conn, (KIMI, GO)), GO)
            count = conn.execute("SELECT count(*) FROM provider_leases").fetchone()[0]
            self.assertEqual(count, 0)

    def test_auth_failure_does_not_fall_back_or_pause_account(self):
        checkout = self.cfg.workspace / "checkout"
        checkout.mkdir(parents=True)
        selected = []

        def fake_docker(cfg, path, command, *, selected_model, **kwargs):
            selected.append(selected_model.ref)
            raise runtime.WorkerFailure(77)

        with patch.object(runtime, "GitHub", Hub), patch.object(
                runtime, "_checkout", return_value=checkout), patch.object(
                runtime, "_docker", side_effect=fake_docker):
            with self.assertRaises(runtime.WorkerFailure) as failure:
                self._perform(1)
        self.assertEqual(failure.exception.returncode, 77)
        self.assertEqual(selected, [KIMI.ref])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(choose_model(conn, (KIMI, GO)), KIMI)


if __name__ == "__main__":
    unittest.main()
