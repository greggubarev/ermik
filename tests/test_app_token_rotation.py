"""Local GitHub App token rotation and crash recovery contracts."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from ermik.app_token_rotation import TokenPair, TokenRotationError, TokenStore
from ermik import app_token_login, pr_broker


NOW = 2_000_000_000
OLD_ACCESS = "old-access-value"
OLD_REFRESH = "old-refresh-value"
NEW_ACCESS = "new-access-value"
NEW_REFRESH = "new-refresh-value"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return json.dumps(self.payload).encode("utf-8")


class TokenRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "github-app-tokens.json"
        self.store = TokenStore(self.path)

    def seed(self, *, access_expiry=NOW + 300, refresh_expiry=NOW + 86400):
        self.path.write_text(json.dumps({
            "access_token": OLD_ACCESS,
            "refresh_token": OLD_REFRESH,
            "expires_at": access_expiry,
            "refresh_token_expires_at": refresh_expiry,
        }), encoding="utf-8")
        self.path.chmod(0o600)
        return self.path.read_bytes()

    @staticmethod
    def renewed():
        return Response({
            "access_token": NEW_ACCESS,
            "refresh_token": NEW_REFRESH,
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
        })

    def test_due_pair_refreshes_and_persists_both_tokens_together(self):
        self.seed()
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return self.renewed()

        pair = self.store.refresh("app-client-id", opener=opener, now=NOW)
        self.assertEqual((pair.access_token, pair.refresh_token),
                         (NEW_ACCESS, NEW_REFRESH))
        self.assertEqual((pair.expires_at, pair.refresh_token_expires_at),
                         (NOW + 28800, NOW + 15897600))
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(timeout, 20)
        self.assertEqual(request.full_url, "https://github.com/login/oauth/access_token")
        self.assertEqual(request.get_method(), "POST")
        form = parse_qs(request.data.decode("ascii"))
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["client_id"], ["app-client-id"])
        self.assertEqual(form["refresh_token"], [OLD_REFRESH])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], NEW_ACCESS)
        self.assertEqual(saved["refresh_token"], NEW_REFRESH)
        self.assertEqual(self.store.load(), pair)
        self.assertFalse(self.path.with_name(self.path.name + ".pending").exists())

    def test_valid_pair_before_refresh_window_does_not_contact_github(self):
        original = self.seed(access_expiry=NOW + 7200)

        def forbidden(*args, **kwargs):
            self.fail("fresh token should not be refreshed")

        pair = self.store.refresh("app-client-id", opener=forbidden, now=NOW)
        self.assertEqual(pair.access_token, OLD_ACCESS)
        self.assertEqual(self.path.read_bytes(), original)

    def test_expired_access_uses_live_refresh_token(self):
        self.seed(access_expiry=NOW - 1)
        pair = self.store.refresh("app-client-id", opener=lambda *a, **k: self.renewed(),
                                  now=NOW)
        self.assertEqual(pair.access_token, NEW_ACCESS)

    def test_expired_refresh_token_refuses_without_http_request(self):
        original = self.seed(access_expiry=NOW - 1, refresh_expiry=NOW - 1)

        def forbidden(*args, **kwargs):
            self.fail("expired refresh token should not be sent")

        with self.assertRaises(TokenRotationError):
            self.store.refresh("app-client-id", opener=forbidden, now=NOW)
        self.assertEqual(self.path.read_bytes(), original)

    def test_rejected_refresh_preserves_old_pair_and_blocks_retry(self):
        original = self.seed()
        error = HTTPError("https://github.com/login/oauth/access_token", 401,
                          "invalid refresh " + OLD_REFRESH, None, None)
        with self.assertRaises(TokenRotationError):
            self.store.refresh("app-client-id", opener=lambda *a, **k: (_ for _ in ()).throw(error),
                               now=NOW)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.path.with_name(self.path.name + ".pending").exists())
        with self.assertRaises(TokenRotationError):
            TokenStore(self.path).load()

    def test_interrupted_http_attempt_fails_closed_after_restart(self):
        original = self.seed()

        def interrupted(*args, **kwargs):
            raise OSError("connection closed after request")

        with self.assertRaises(TokenRotationError):
            self.store.refresh("app-client-id", opener=interrupted, now=NOW)
        self.assertEqual(self.path.read_bytes(), original)
        restarted = TokenStore(self.path)
        with self.assertRaises(TokenRotationError):
            restarted.refresh("app-client-id", opener=lambda *a, **k: self.renewed(),
                              now=NOW)

    def test_restart_promotes_complete_new_pair_after_failed_atomic_replace(self):
        original = self.seed()
        real_replace = __import__("os").replace

        def fail_pair_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("disk write failed")
            return real_replace(source, destination)

        with patch("ermik.app_token_rotation.os.replace", side_effect=fail_pair_replace):
            with self.assertRaises(TokenRotationError):
                self.store.refresh("app-client-id", opener=lambda *a, **k: self.renewed(),
                                   now=NOW)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.path.with_name(self.path.name + ".pending").exists())
        self.assertTrue(self.path.with_name(self.path.name + ".new").exists())
        recovered = TokenStore(self.path).load()
        self.assertEqual((recovered.access_token, recovered.refresh_token),
                         (NEW_ACCESS, NEW_REFRESH))
        self.assertFalse(self.path.with_name(self.path.name + ".pending").exists())
        self.assertFalse(self.path.with_name(self.path.name + ".new").exists())

    def test_stale_new_file_without_pending_blocks_rotation(self):
        original = self.seed()
        staged = self.path.with_name(self.path.name + ".new")
        staged.write_text(json.dumps({
            "access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH,
            "expires_at": NOW + 28800,
            "refresh_token_expires_at": NOW + 15897600,
        }), encoding="utf-8")
        staged.chmod(0o600)

        def forbidden(*args, **kwargs):
            self.fail("a stale staged pair must be reconciled before HTTP")

        with self.assertRaises(TokenRotationError):
            self.store.refresh("app-client-id", opener=forbidden, now=NOW)
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_staged_pair_with_pending_fails_closed(self):
        original = self.seed()
        real_replace = __import__("os").replace

        def fail_pair_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("disk write failed")
            return real_replace(source, destination)

        with patch("ermik.app_token_rotation.os.replace", side_effect=fail_pair_replace):
            with self.assertRaises(TokenRotationError):
                self.store.refresh("app-client-id", opener=lambda *a, **k: self.renewed(),
                                   now=NOW)
        staged = self.path.with_name(self.path.name + ".new")
        staged.write_text("{broken", encoding="utf-8")
        staged.chmod(0o600)
        with self.assertRaises(TokenRotationError):
            TokenStore(self.path).load()
        self.assertEqual(self.path.read_bytes(), original)

    def test_errors_and_pending_metadata_do_not_disclose_tokens(self):
        self.seed()

        def rejected(*args, **kwargs):
            raise OSError("request failed: " + OLD_ACCESS + " " + OLD_REFRESH)

        with self.assertRaises(TokenRotationError) as caught:
            self.store.refresh("app-client-id", opener=rejected, now=NOW)
        pending = self.path.with_name(self.path.name + ".pending")
        exposed = str(caught.exception) + pending.read_text(encoding="utf-8")
        self.assertNotIn(OLD_ACCESS, exposed)
        self.assertNotIn(OLD_REFRESH, exposed)

    def test_broker_uses_refreshed_access_token_at_startup(self):
        self.seed()
        replacement = TokenPair(NEW_ACCESS, NEW_REFRESH, NOW + 28800,
                                NOW + 15897600)
        socket_path = self.path.parent / "broker.sock"
        evidence_dir = self.path.parent / "evidence"
        evidence_dir.mkdir()
        captured = {}

        class StopServer(Exception):
            pass

        class FakeSocket:
            def bind(self, address):
                captured["socket_path"] = address

            def listen(self, backlog):
                pass

            def settimeout(self, seconds):
                pass

            def accept(self):
                raise StopServer()

            def close(self):
                pass

        real_broker = pr_broker.Broker

        def capture_broker(**kwargs):
            captured["api"] = kwargs["api"]
            return real_broker(**kwargs)

        argv = ["pr_broker", "serve", "--socket", str(socket_path),
                "--socket-gid", "0", "--repo", "org/upstream", "--repo-id", "11",
                "--fork", "person/fork", "--fork-id", "22",
                "--maintainer", "maintainer", "--controller-uid", "1",
                "--evidence-dir", str(evidence_dir),
                "--app-token-store", str(self.path), "--app-client-id", "app-client-id"]
        with patch.object(sys, "argv", argv), patch.object(pr_broker.time, "time",
                                                           return_value=NOW), patch.object(
                TokenStore, "refresh", return_value=replacement) as refresh, patch.object(
                    pr_broker, "Broker", side_effect=capture_broker), patch.object(
                        pr_broker.socket, "AF_UNIX", 1, create=True), patch.object(
                            pr_broker.socket, "socket", return_value=FakeSocket()), patch.object(
                            pr_broker.os, "chown", create=True), patch.object(
                                pr_broker.os, "chmod"):
            with self.assertRaises(StopServer):
                pr_broker.main()
        refresh.assert_called()
        self.assertEqual(captured["api"].token, NEW_ACCESS)
        self.assertEqual(captured["socket_path"], str(socket_path))

    def test_first_device_authorization_saves_private_pair(self):
        clock = [NOW]
        requests = []
        responses = iter([
            {"device_code": "private-device-code", "user_code": "ABCD-EFGH",
             "verification_uri": "https://github.com/login/device",
             "expires_in": 900, "interval": 2},
            {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH,
             "expires_in": 28800, "refresh_token_expires_in": 15897600,
             "token_type": "bearer"},
        ])

        def opener(request, timeout):
            requests.append(request)
            return Response(next(responses))

        def sleep(seconds):
            clock[0] += seconds

        messages = []
        app_token_login.authorize("app-client-id", self.store, opener=opener,
                                  sleep=sleep, now=lambda: clock[0],
                                  announce=messages.append)
        self.assertEqual((self.store.load().access_token, self.store.load().refresh_token),
                         (NEW_ACCESS, NEW_REFRESH))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].full_url, "https://github.com/login/device/code")
        self.assertEqual(requests[1].full_url,
                         "https://github.com/login/oauth/access_token")
        self.assertEqual(parse_qs(requests[1].data.decode("ascii"))["device_code"],
                         ["private-device-code"])
        self.assertIn("ABCD-EFGH", messages[0])
        self.assertNotIn("private-device-code", "\n".join(messages))
        with self.assertRaises(TokenRotationError):
            self.store.initialize(TokenPair("another-access", "another-refresh",
                                            NOW + 28800, NOW + 15897600))

    def test_device_pending_and_slow_down_extend_poll_interval(self):
        clock = [NOW]
        sleeps = []
        responses = iter([
            {"device_code": "private-device-code", "user_code": "ABCD-EFGH",
             "verification_uri": "https://github.com/login/device",
             "expires_in": 900, "interval": 2},
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH,
             "expires_in": 28800, "refresh_token_expires_in": 15897600,
             "token_type": "bearer"},
        ])

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        app_token_login.authorize("app-client-id", self.store,
                                  opener=lambda *a, **k: Response(next(responses)),
                                  sleep=sleep, now=lambda: clock[0], announce=lambda _: None)
        self.assertEqual(sleeps, [2, 2, 7])
        self.assertEqual(self.store.load().access_token, NEW_ACCESS)

    def test_device_flow_errors_hide_response_secrets(self):
        secret = "private-device-response-secret"
        error = HTTPError("https://github.com/login/device/code", 500,
                          secret, None, None)
        self.addCleanup(error.close)

        def fail(*args, **kwargs):
            raise error

        with self.assertRaises(TokenRotationError) as caught:
            app_token_login.authorize("app-client-id", self.store,
                                      opener=fail, announce=lambda _: None)
        self.assertNotIn(secret, str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_reauthorization_resumes_after_interrupted_pair_replace(self):
        self.seed()
        fresh = TokenPair(NEW_ACCESS, NEW_REFRESH, NOW + 28800,
                          NOW + 15897600)
        real_replace = __import__("os").replace

        def fail_pair_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("interrupted replacement")
            return real_replace(source, destination)

        with patch("ermik.app_token_rotation.os.replace", side_effect=fail_pair_replace):
            with self.assertRaises(TokenRotationError):
                self.store.reauthorize(fresh)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))
                         ["access_token"], OLD_ACCESS)
        self.assertTrue(self.store.reauthorization_marker.exists())
        self.assertTrue(self.store.temporary.exists())
        with self.assertRaises(TokenRotationError):
            TokenStore(self.path).load()
        self.assertEqual(TokenStore(self.path).reauthorize(fresh), fresh)
        self.assertEqual(TokenStore(self.path).load(), fresh)
        self.assertFalse(self.store.pending.exists())
        self.assertFalse(self.store.temporary.exists())
        self.assertFalse(self.store.reauthorization_marker.exists())

    def test_reauthorization_restart_clears_marker_after_pair_replace(self):
        self.seed()
        fresh = TokenPair(NEW_ACCESS, NEW_REFRESH, NOW + 28800,
                          NOW + 15897600)
        real_unlink = Path.unlink

        def fail_marker_cleanup(path, *args, **kwargs):
            if path == self.store.reauthorization_marker:
                raise OSError("interrupted cleanup")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_marker_cleanup):
            with self.assertRaises(TokenRotationError):
                self.store.reauthorize(fresh)
        self.assertTrue(self.store.reauthorization_marker.exists())
        self.assertEqual(TokenStore(self.path).load(), fresh)
        self.assertFalse(self.store.reauthorization_marker.exists())


if __name__ == "__main__":
    unittest.main()
