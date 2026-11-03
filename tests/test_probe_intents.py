"""Durable recovery of disposable publication capability probes."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ermik.github import GitHub, GitHubError


HEAD = "a" * 40
OTHER = "b" * 40
PROBE_ID = "f" * 32
REF = f"refs/heads/ermik-access-probe-{PROBE_ID}"
REPO = "publisher/project"
TOKEN = "disposable-fork-token"


class Response(io.BytesIO):
    def __init__(self, status=200, body=None):
        super().__init__(json.dumps(body or {}).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def missing():
    return HTTPError("https://api.github.com", 404, "missing", None, None)


def ref_row(sha=HEAD):
    return {"ref": REF, "object": {"sha": sha}}


class ProbeIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "controller"
        self.github = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                             probe_state_dir=self.state_dir)
        self.env = patch.dict(os.environ, {"GH_TOKEN": "upstream-token",
                                        "ERMIK_FORK_TOKEN": TOKEN})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _intents(self):
        return list((self.state_dir / "probe-intents").glob("*.json"))

    def _stop_at_post(self, *, post_result=KeyboardInterrupt()):
        with patch("ermik.github.uuid.uuid4") as unique, patch(
                "ermik.github.urlopen", side_effect=[post_result]) as request:
            unique.return_value.hex = PROBE_ID
            with self.assertRaises(KeyboardInterrupt):
                self.github._probe_branch_write(REPO, HEAD, TOKEN)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0].get_method(), "POST")
        return self._intents()

    def test_intent_is_durable_before_post_and_excludes_credentials(self):
        intents = self._stop_at_post()
        self.assertEqual(len(intents), 1)
        raw = intents[0].read_text(encoding="utf-8")
        self.assertIn(REPO, raw)
        self.assertIn(REF, raw)
        self.assertIn(HEAD, raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("upstream-token", raw)
        self.assertNotIn("main", raw)

    def test_persistence_failure_blocks_post(self):
        with patch("ermik.github.os.fsync", side_effect=OSError("disk full")), patch(
                "ermik.github.urlopen") as request:
            with self.assertRaisesRegex(GitHubError, "persisted"):
                self.github._probe_branch_write(REPO, HEAD, TOKEN)
            request.assert_not_called()

    def test_probe_without_state_directory_fails_before_post(self):
        github = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN")
        with patch("ermik.github.urlopen") as request:
            with self.assertRaises(GitHubError):
                github._probe_branch_write(REPO, HEAD, TOKEN)
            request.assert_not_called()

    def test_post_422_does_not_adopt_preexisting_same_sha_ref(self):
        preexisting = HTTPError("https://api.github.com", 422, "exists", None, None)
        with patch("ermik.github.uuid.uuid4") as unique, patch(
                "ermik.github.urlopen", side_effect=[preexisting]) as request:
            unique.return_value.hex = PROBE_ID
            with self.assertRaises(GitHubError):
                self.github._probe_branch_write(REPO, HEAD, TOKEN)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0].get_method(), "POST")
        self.assertFalse(self._intents())
        with patch("ermik.github.urlopen") as request:
            self.github.reconcile_probe_intents()
            request.assert_not_called()

    def test_restart_after_stop_before_post_clears_only_missing_intent(self):
        self._stop_at_post()
        restarted = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                          probe_state_dir=self.state_dir)
        metadata = {"full_name": REPO, "default_branch": "main"}
        with patch("ermik.github.urlopen", side_effect=[missing(), Response(body=metadata),
                                                         Response(body={"ref": "refs/heads/main",
                                                                        "object": {"sha": HEAD}})]) as request:
            restarted.reconcile_probe_intents()
        self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                         ["GET", "GET", "GET"])
        self.assertFalse(self._intents())

    def test_restart_after_post_deletes_matching_ref_once(self):
        self._stop_at_post()
        restarted = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                          probe_state_dir=self.state_dir)
        with patch("ermik.github.urlopen", side_effect=[Response(body=ref_row()),
                                                         Response(body=ref_row()),
                                                         Response(204)]) as request:
            restarted.reconcile_probe_intents()
        self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                         ["GET", "GET", "DELETE"])
        self.assertIn("/git/refs/heads/ermik-access-probe-", request.call_args.args[0].full_url)
        self.assertFalse(self._intents())
        with patch("ermik.github.urlopen") as request:
            restarted.reconcile_probe_intents()
            request.assert_not_called()

    def test_restart_blocks_changed_and_unreadable_ref_without_delete(self):
        for index, result in enumerate((Response(body=ref_row(OTHER)), URLError("offline"),
                                        Response(body={"ref": REF, "object": {"sha": "bad"}}))):
            with self.subTest(result=type(result).__name__):
                self.state_dir = Path(self.temp.name) / f"controller-{index}"
                self.github = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                                     probe_state_dir=self.state_dir)
                self._stop_at_post()
                restarted = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                                  probe_state_dir=self.state_dir)
                with patch("ermik.github.urlopen", side_effect=[result]) as request:
                    with self.assertRaises(GitHubError):
                        restarted.reconcile_probe_intents()
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[0].get_method(), "GET")
                self.assertEqual(len(self._intents()), 1)

    def test_ref_change_on_final_read_blocks_delete(self):
        self._stop_at_post()
        with patch("ermik.github.urlopen", side_effect=[Response(body=ref_row()),
                                                         Response(body=ref_row(OTHER))]) as request:
            with self.assertRaisesRegex(GitHubError, "changed"):
                self.github.reconcile_probe_intents()
        self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                         ["GET", "GET"])
        self.assertEqual(len(self._intents()), 1)

    def test_reconcile_cannot_erase_fresh_intent_before_post(self):
        """A second instance must not turn a pending POST into an orphan ref."""
        saved = threading.Event()
        release_post = threading.Event()
        reconcile_started = threading.Event()
        reconcile_done = threading.Event()
        reconcile_read = threading.Event()
        remote = {"sha": None}
        failures = []
        first = self.github
        second = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                        probe_state_dir=self.state_dir)
        original_save = first._save_probe_intent

        def pause_after_save(*args):
            path = original_save(*args)
            saved.set()
            if not release_post.wait(3):
                raise AssertionError("POST was never released")
            return path

        def fake_urlopen(request, *_args, **_kwargs):
            method = request.get_method()
            url = request.full_url
            if method == "POST":
                remote["sha"] = HEAD
                return Response(201)
            if method == "DELETE":
                self.assertEqual(remote["sha"], HEAD)
                remote["sha"] = None
                return Response(204)
            if url.endswith("/git/ref/heads/ermik-access-probe-" + PROBE_ID):
                if threading.current_thread() is thread_b:
                    reconcile_read.set()
                return Response(body=ref_row()) if remote["sha"] else missing()
            if url == f"https://api.github.com/repos/{REPO}":
                return Response(body={"full_name": REPO, "default_branch": "main"})
            if url == f"https://api.github.com/repos/{REPO}/git/ref/heads/main":
                return Response(body={"ref": "refs/heads/main",
                                      "object": {"sha": HEAD}})
            raise AssertionError("unexpected GitHub request")

        def run_probe():
            try:
                self.assertTrue(first._probe_branch_write(REPO, HEAD, TOKEN))
            except BaseException as exc:
                failures.append(exc)

        def run_reconcile():
            reconcile_started.set()
            try:
                second.reconcile_probe_intents()
            except GitHubError:
                # A safe refusal may finish before the pending POST.
                pass
            except BaseException as exc:
                failures.append(exc)
            finally:
                reconcile_done.set()

        with patch.object(first, "_save_probe_intent", side_effect=pause_after_save), patch(
                "ermik.github.uuid.uuid4") as unique, patch(
                "ermik.github.urlopen", side_effect=fake_urlopen):
            unique.return_value.hex = PROBE_ID
            thread_a = threading.Thread(target=run_probe)
            thread_b = threading.Thread(target=run_reconcile)
            thread_a.start()
            try:
                self.assertTrue(saved.wait(3), "A did not persist its intent")
                self.assertEqual(len(self._intents()), 1)
                thread_b.start()
                self.assertTrue(reconcile_started.wait(3), "B did not start")
                self.assertFalse(reconcile_read.wait(0.2),
                                 "B inspected A's ref before A sent POST")
                if reconcile_done.wait(0.2):
                    self.assertEqual(len(self._intents()), 1,
                                     "B erased A's intent before its POST")
            finally:
                release_post.set()
                thread_a.join(3)
                if thread_b.ident is not None:
                    thread_b.join(3)
        self.assertFalse(thread_a.is_alive() or thread_b.is_alive(),
                         "probe or reconciliation did not finish")
        self.assertEqual(failures, [])
        self.assertIsNone(remote["sha"], "probe ref was orphaned")
        self.assertFalse(self._intents())

    def test_other_controller_cannot_reconcile_this_controllers_intent(self):
        self._stop_at_post()
        other = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                       probe_state_dir=Path(self.temp.name) / "other-controller")
        with patch("ermik.github.urlopen") as request:
            other.reconcile_probe_intents()
            request.assert_not_called()
        self.assertEqual(len(self._intents()), 1)

    def test_restart_after_delete_stop_reconciles_absence_without_new_delete(self):
        with patch("ermik.github.uuid.uuid4") as unique, patch(
                "ermik.github.urlopen", side_effect=[Response(201), Response(body=ref_row()),
                                                       KeyboardInterrupt()]) as request:
            unique.return_value.hex = PROBE_ID
            with self.assertRaises(KeyboardInterrupt):
                self.github._probe_branch_write(REPO, HEAD, TOKEN)
        self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                         ["POST", "GET", "DELETE"])
        self.assertEqual(len(self._intents()), 1)
        restarted = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                          probe_state_dir=self.state_dir)
        metadata = {"full_name": REPO, "default_branch": "main"}
        with patch("ermik.github.urlopen", side_effect=[missing(), Response(body=metadata),
                                                         Response(body={"ref": "refs/heads/main",
                                                                        "object": {"sha": HEAD}})]) as request:
            restarted.reconcile_probe_intents()
        self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                         ["GET", "GET", "GET"])
        self.assertFalse(self._intents())

    def test_lost_post_and_delete_responses_leave_recoverable_intent(self):
        for phase in ("post", "delete"):
            with self.subTest(phase=phase):
                self.state_dir = Path(self.temp.name) / f"controller-{phase}"
                self.github = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                                     probe_state_dir=self.state_dir)
                effects = ([URLError("lost POST"), URLError("unreadable")]
                           if phase == "post" else
                           [Response(201), Response(body=ref_row()),
                            URLError("lost DELETE"), URLError("unreadable")])
                with patch("ermik.github.uuid.uuid4") as unique, patch(
                        "ermik.github.urlopen", side_effect=effects):
                    unique.return_value.hex = PROBE_ID
                    with self.assertRaises(GitHubError):
                        self.github._probe_branch_write(REPO, HEAD, TOKEN)
                self.assertEqual(len(self._intents()), 1)
                restarted = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                                  probe_state_dir=self.state_dir)
                with patch("ermik.github.urlopen", side_effect=[Response(body=ref_row()),
                                                                 Response(body=ref_row()),
                                                                 Response(204)]) as request:
                    restarted.reconcile_probe_intents()
                self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                                 ["GET", "GET", "DELETE"])
                self.assertFalse(self._intents())

    def test_run_once_reconciles_when_no_issue_is_ready(self):
        from ermik import runtime
        from ermik.config import Config

        cfg = Config("example/project", "owner", self.state_dir,
                     Path(self.temp.name) / "work", "demo:latest", ("true",),
                     (("true",),), 1, 60, (), "main", publish_repo=REPO,
                     publish_token_env="ERMIK_FORK_TOKEN")
        with patch.object(runtime, "_github") as factory, patch.object(
                runtime, "_cleanup_containers"), patch.object(
                runtime, "_reconcile_interrupted"), patch.object(
                runtime, "_correction", return_value=None), patch.object(
                runtime, "_claim_new", return_value=None) as claim:
            self.assertEqual(runtime.run_once(cfg), [])
        factory.return_value.reconcile_probe_intents.assert_called_once_with()
        claim.assert_called_once()

    def test_mismatched_credential_cannot_delete_matching_ref(self):
        self._stop_at_post()
        restarted = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                          probe_state_dir=self.state_dir)
        with patch.dict(os.environ, {"ERMIK_FORK_TOKEN": "other-fork-token"}), patch(
                "ermik.github.urlopen") as request:
            with self.assertRaises(GitHubError):
                restarted.reconcile_probe_intents()
            request.assert_not_called()
        self.assertEqual(len(self._intents()), 1)

    def test_stale_or_main_target_intent_blocks_all_remote_requests(self):
        for case in ("stale", "main"):
            with self.subTest(case=case):
                self.state_dir = Path(self.temp.name) / f"controller-{case}"
                self.github = GitHub("example/project", REPO, "ERMIK_FORK_TOKEN",
                                     probe_state_dir=self.state_dir)
                intent = self._stop_at_post()[0]
                row = json.loads(intent.read_text(encoding="utf-8"))
                if case == "stale":
                    row["created"] = time.time() - 8 * 86400
                else:
                    row["ref"] = "refs/heads/main"
                intent.write_text(json.dumps(row), encoding="utf-8")
                with patch("ermik.github.urlopen") as request:
                    with self.assertRaises(GitHubError):
                        self.github.reconcile_probe_intents()
                    request.assert_not_called()
                self.assertTrue(intent.exists())


if __name__ == "__main__":
    unittest.main()
