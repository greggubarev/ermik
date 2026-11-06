"""Controller to provider routing contracts without Docker or network calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
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
IMAGE_ID = "sha256:" + "b" * 64
CONFIG_DIGEST = "sha256:" + "9" * 64


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
        with state.connect(self.cfg.state_dir) as conn:
            for issue in (1, 2):
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                             "VALUES(?,?,?,'running',?)",
                             (issue, f"work/issue-{issue}", BASE, '["file.txt"]'))
        resolver = patch.object(runtime, "_resolve_image_id", return_value=IMAGE_ID)
        resolver.start()
        self.addCleanup(resolver.stop)
        portable = patch.object(runtime, "_portable_image_digest",
                                return_value=CONFIG_DIGEST)
        portable.start()
        self.addCleanup(portable.stop)

    def tearDown(self):
        self.temp.cleanup()

    def _perform(self, issue: int):
        return runtime._perform(self.cfg, issue, f"work/issue-{issue}", self.spec,
                                correction_note=None, pr_number=None,
                                expected_base=BASE, expected_head=None)

    def test_failure_reason_uses_typed_outcomes_only(self):
        self.assertEqual(runtime._failure_reason(runtime.WorkerFailure(77)), "provider_auth")
        self.assertEqual(runtime._failure_reason(
            runtime.ProviderQuotaExhausted("quota")), "provider_quota")
        self.assertEqual(runtime._failure_reason(
            runtime.LocalCheckFailure("check")), "local_check_failed")
        self.assertIsNone(runtime._failure_reason(runtime.WorkerFailure(75)))
        self.assertIsNone(runtime._failure_reason(runtime.WorkflowError("secret auth failure")))
        self.assertEqual(runtime._failure_reason(runtime.BrokerUnavailable()), "broker_unavailable")
        self.assertEqual(runtime._failure_reason(runtime.PublicationUnconfirmed()), "publication_unconfirmed")
        self.assertEqual(runtime._failure_reason(runtime.PublishCredentialUnavailable()),
                         "publish_credential_unavailable")

    def test_broker_connection_and_reply_boundaries(self):
        cfg = replace(self.cfg, draft_pr_socket=self.cfg.state_dir / "broker.sock")

        class Client:
            def __init__(self, *, connect_error=False, send_error=False, reply=b'{"ok":true,"pr":11}\n'):
                self.connect_error = connect_error
                self.send_error = send_error
                self.reply = reply
                self.closed = False
                self.sent = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def settimeout(self, _seconds):
                pass

            def connect(self, _path):
                if self.connect_error:
                    raise OSError("private connection detail")

            def sendall(self, _payload):
                self.sent = True
                if self.send_error:
                    raise OSError("partial send")

            def makefile(self, _mode):
                return io.BytesIO(self.reply)

        def publish(client):
            with patch.object(runtime.socket, "AF_UNIX", 1, create=True), \
                    patch.object(runtime.socket, "socket", return_value=client):
                return runtime._draft_pr(cfg, Hub(self.cfg.repo), issue=1,
                                         branch="work/issue-1", head=BASE, base=BASE,
                                         paths=["file.txt"], proof={"result": "passed"},
                                         model=None, body_sha256="c" * 64, title="Issue 1")

        disconnected = Client(connect_error=True)
        with self.assertRaises(runtime.BrokerUnavailable):
            publish(disconnected)
        self.assertTrue(disconnected.closed)
        self.assertFalse(disconnected.sent)
        for client in (Client(send_error=True), Client(reply=b""),
                       Client(reply=b'{"ok":false}\n'), Client(reply=b'{"ok":true,"pr":"11"}\n')):
            with self.subTest(reply=client.reply, send_error=client.send_error):
                with self.assertRaises(runtime.PublicationUnconfirmed):
                    publish(client)
                self.assertTrue(client.closed)
                self.assertTrue(client.sent)
        self.assertEqual(publish(Client()), 11)

    def test_only_confirmed_check_command_exit_has_typed_failure(self):
        checkout = self.cfg.workspace / "checkout"
        checkout.mkdir(parents=True)
        with patch.object(runtime, "_cleanup_containers"), \
             patch.object(runtime, "_check_git_snapshot",
                          return_value=(checkout / "snapshot", checkout / ".git")), \
             patch.object(runtime, "_run_capped", return_value=(1, b"check failed")):
            with self.assertRaises(runtime.LocalCheckFailure):
                runtime._docker(self.cfg, checkout, ("check",), issue_file=None,
                                network=False, timeout=10, image_id=IMAGE_ID)
        with patch.object(runtime, "_cleanup_containers"), \
             patch.object(runtime, "_check_git_snapshot",
                          return_value=(checkout / "snapshot", checkout / ".git")), \
             patch.object(runtime, "_run_capped", return_value=(125, b"docker failed")):
            with self.assertRaises(runtime.WorkflowError) as error:
                runtime._docker(self.cfg, checkout, ("check",), issue_file=None,
                                network=False, timeout=10, image_id=IMAGE_ID)
        self.assertNotIsInstance(error.exception, runtime.LocalCheckFailure)

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
        self.assertIn(IMAGE_ID, args)
        self.assertNotIn(self.cfg.image, args)

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
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual([state.job(conn, issue)["image_id"] for issue in (1, 2)],
                             [IMAGE_ID, IMAGE_ID])

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
        image_ids = []

        def fake_docker(cfg, path, command, *, selected_model, **kwargs):
            selected.append(selected_model.ref)
            image_ids.append(kwargs["image_id"])
            with state.connect(cfg.state_dir) as conn:
                pending = conn.execute("SELECT result_code,finished_at FROM usage_events "
                                       "WHERE issue=1 ORDER BY rowid DESC LIMIT 1").fetchone()
                self.assertEqual(tuple(pending), (-2, None))
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
        self.assertEqual(image_ids, [IMAGE_ID, IMAGE_ID])
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(choose_model(conn, (KIMI, GO)), GO)
            count = conn.execute("SELECT count(*) FROM provider_leases").fetchone()[0]
            self.assertEqual(count, 0)
            attempts = conn.execute("SELECT provider_id,model_id,phase,result_code,"
                                    "finished_at,duration_ms FROM usage_events "
                                    "WHERE issue=1 ORDER BY created_at,rowid").fetchall()
            self.assertEqual([(row[0], row[1], row[2], row[3]) for row in attempts],
                             [(KIMI.provider_id, KIMI.model_id, "initial", 75),
                              (GO.provider_id, GO.model_id, "initial", -1)])
            self.assertTrue(all(row[4] is not None and row[5] is not None
                                for row in attempts))

    def test_unconfigured_model_still_records_worker_attempt(self):
        cfg = replace(self.cfg, models=())
        checkout = cfg.workspace / "no-model"
        checkout.mkdir(parents=True)
        with patch.object(runtime, "GitHub", Hub), patch.object(
                runtime, "_checkout", return_value=checkout), patch.object(
                runtime, "_docker", side_effect=runtime.WorkerFailure(1)) as worker:
            with self.assertRaises(runtime.WorkerFailure):
                runtime._perform(cfg, 1, "work/issue-1", self.spec,
                                 correction_note=None, pr_number=None,
                                 expected_base=BASE, expected_head=None)
        self.assertEqual(worker.call_count, 1)
        with state.connect(cfg.state_dir) as conn:
            row = conn.execute("SELECT * FROM usage_events WHERE issue=1").fetchone()
            self.assertEqual((row["provider_id"], row["model_id"], row["phase"],
                              row["result_code"]), ("", "", "initial", 1))
            self.assertIsNotNone(row["finished_at"])

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
