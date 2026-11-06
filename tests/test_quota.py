"""Local quota observations are bounded and never expose provider responses."""

import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ermik.quota import probe, unknown
from ermik.__main__ import refresh_quota
from ermik.config import QuotaSource
from ermik.observability import status_snapshot
from ermik.dashboard import public_snapshot
from ermik.providers import ModelCandidate, claim_model_decision
from ermik.state import connect


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
RESET = "2026-11-08T12:00:00Z"
RESET_MS = 1794139200000
SECRET = "private-password"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.command, self.path,
                                     self.headers.get("Authorization")))
        status, body = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.reply = (404, b"{}")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def reply(self, value, status=200):
        self.server.reply = (status, json.dumps(value).encode())

    def query(self, kind="kimi-code", auth=SECRET):
        return probe(kind, self.server.server_port, auth, observed_at=NOW)

    def test_kimi_valid_windows_and_wallet_exclusion(self):
        self.reply({"code": 0, "data": {"kind": "ok", "quota": {
            "usages": {"limit5h": {"usedRatio": 0.375, "resetAt": RESET},
                       "monthCode": {"usedRatio": 1.0}},
            "extraUsage": {"balanceCents": 999, "secret": SECRET}}}})
        result = self.query()
        self.assertEqual(result["source"], "kimi-cli-local")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unit"], "percent")
        self.assertEqual(result["windows"], [
            {"name": "limit5h", "used_percent": 37.5, "reset_at": RESET},
            {"name": "monthCode", "used_percent": 100.0, "reset_at": None},
        ])
        self.assertEqual(self.server.requests, [("GET", "/api/v1/oauth/usage",
                                                  "Bearer " + SECRET)])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("balanceCents", json.dumps(result))

    def test_go_valid_windows_and_budget_exclusion(self):
        self.reply({"data": {"go": {"weekly": {"percent": 42.5,
                                                 "limited": False,
                                                 "resetsAt": RESET_MS}},
                             "budgets": [{"spent": 999, "secret": SECRET}]}})
        result = self.query("opencode-go")
        self.assertEqual(result["source"], "opencode-local")
        self.assertEqual(result["windows"], [
            {"name": "weekly", "used_percent": 42.5, "reset_at": RESET}])
        expected = "Basic " + base64.b64encode(("opencode:" + SECRET).encode()).decode()
        self.assertEqual(self.server.requests, [("GET", "/api/experimental/usage/limits",
                                                  expected)])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("budgets", json.dumps(result))

    def test_missing_endpoint_auth_failure_and_rate_limit(self):
        for code, reason in ((404, "missing_endpoint"), (401, "unauthorized"),
                             (429, "rate_limited")):
            with self.subTest(code=code):
                self.reply({"message": SECRET}, code)
                result = self.query()
                self.assertEqual((result["status"], result["reason"]),
                                 ("unknown", reason))
                self.assertEqual(result["windows"], [])
                self.assertNotIn(SECRET, json.dumps(result))

    def test_malformed_and_inconsistent_units_are_unknown(self):
        samples = [
            ("kimi-code", {"code": 0, "data": {"kind": "ok", "quota":
                           {"usages": {"limit5h": {"usedRatio": 45}}}}}),
            ("kimi-code", {"code": 0, "data": {"kind": "ok", "quota":
                           {"usages": {"limit5h": {"usedRatio": -0.1}}}}}),
            ("opencode-go", {"data": {"go": {"rolling": {"percent": 0.5,
                               "limited": "false", "resetsAt": RESET_MS}}}}),
            ("opencode-go", {"data": {"go": {"rolling": {"percent": 101,
                               "limited": False, "resetsAt": RESET_MS}}}}),
            ("opencode-go", {"data": {"go": {"rolling": {"percent": 50,
                               "limited": False, "resetsAt": RESET_MS // 1000}}}}),
        ]
        for kind, body in samples:
            with self.subTest(kind=kind, body=body):
                self.reply(body)
                self.assertEqual(self.query(kind)["status"], "unknown")
        self.server.reply = (200, b"not-json " + SECRET.encode())
        self.assertEqual(self.query()["reason"], "invalid_response")

    def test_stale_and_in_band_errors(self):
        self.reply({"code": 0, "data": {"kind": "ok", "quota": {"usages": {
            "limit5h": {"usedRatio": 0.1, "resetAt": "2026-11-06T00:00:00Z"}}}}})
        self.assertEqual(self.query()["reason"], "stale")
        self.reply({"code": 0, "data": {"kind": "error", "status": 429,
                                         "message": SECRET}})
        self.assertEqual(self.query()["reason"], "rate_limited")

    def test_no_redirect_or_untrusted_reason(self):
        self.server.reply = (302, b"")
        self.assertEqual(self.query()["reason"], "unavailable")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(unknown("kimi-code", SECRET, NOW)["reason"],
                         "invalid_response")
        self.assertEqual(self.query(auth="broken\nHeader: leaked")["reason"],
                         "invalid_configuration")
        self.assertEqual(len(self.server.requests), 1)

    def test_local_probe_to_cli_and_dashboard_snapshot(self):
        self.reply({"data": {"go": {"weekly": {"percent": 31,
            "limited": True, "resetsAt": RESET_MS}},
            "budgets": [{"secret": SECRET}]}})
        with tempfile.TemporaryDirectory() as directory:
            cfg = SimpleNamespace(repo="owner/repo", state_dir=Path(directory),
                models=(SimpleNamespace(provider_id="opencode-go"),),
                quota_sources={"opencode-go": QuotaSource(
                    "opencode-go", self.server.server_port, "LOCAL_SERVER_PASSWORD")})
            with patch.dict("os.environ", {"LOCAL_SERVER_PASSWORD": SECRET}):
                cli = refresh_quota(cfg)["quota_observations"]
            dashboard = public_snapshot(status_snapshot(
                cfg.state_dir, max_parallel=1,
                quota_providers=("opencode-go",),
                quota_sources={"opencode-go": "opencode-go"}))[
                    "quota_observations"]
            self.assertEqual(cli, dashboard)
            self.assertNotIn(SECRET, str(cli))
            self.assertNotIn("budgets", str(cli))
            model = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY",
                                   max_parallel=2)
            policy = {"opencode-go": SimpleNamespace(estimated_budget_ceiling=1.0,
                       attempt_reservation=1.0)}
            with connect(cfg.state_dir) as db:
                self.assertEqual(claim_model_decision(db, [model], "first",
                    policies=policy)["chosen"], model)
                denied = claim_model_decision(db, [model], "second", policies=policy)
                self.assertEqual((denied["chosen"], denied["reason"]), (None, "budget"))


if __name__ == "__main__":
    unittest.main()
