from http.client import HTTPConnection
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from ermik.dashboard import create_server, public_snapshot, render_html


class DashboardTests(unittest.TestCase):
    def test_projection_escapes_or_discards_untrusted_fields(self):
        source = {
            "observed_at": "2026-10-25T12:00:00Z",
            "queue": {"count": 1, "ready_issues": [7]},
            "slots": {"capacity": 2, "active": 1, "available": 1},
            "jobs": [{"issue": 7, "pr": 8, "status": "running", "attempts": 2,
                      "next_retry_at": 1792930000, "next_review_at": 1792930001,
                      "reason_code": "requires_inspection",
                      "next_action": "reconcile_issue_branch_and_pr_manually",
                      "last_recorded_action": "<script>",
                      "error": "secret-token",
                      "branch": "private-branch"},
                     {"issue": 9, "pr": None, "status": "blocked",
                      "reason_code": "requires_inspection",
                      "next_action": "reconcile_issue_branch_and_pr_manually",
                      "last_recorded_action": "pr_published"}],
            "events": [{"issue": 7, "kind": "model_selected", "model": "provider/model",
                        "created_at": "2026-10-25 12:00:00", "head_sha": "private-sha"},
                       {"issue": 7, "kind": "<script>", "model": "evil"}],
            "provider_cooldown": [{"provider": "provider", "retry_at": 1792930000}],
        }
        projected = public_snapshot(source)
        self.assertEqual(projected["jobs"][0]["model"], "provider/model")
        self.assertIsNone(projected["jobs"][0]["reason_code"])
        self.assertIsNone(projected["jobs"][0]["last_recorded_action"])
        self.assertEqual(projected["jobs"][1]["reason_code"], "requires_inspection")
        self.assertEqual(projected["jobs"][1]["last_recorded_action"], "pr_published")
        payload = json.dumps(projected)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("private-branch", payload)
        self.assertNotIn("private-sha", payload)
        self.assertNotIn("<script>", payload)
        html = render_html(projected).decode("utf-8")
        self.assertIn("provider/model", html)
        self.assertIn("reconcile_issue_branch_and_pr_manually", html)
        self.assertIsNotNone(projected["jobs"][0]["next_review_at"])
        self.assertNotIn("secret-token", html)

    def test_collections_are_bounded(self):
        source = {"queue": {"count": 300, "ready_issues": list(range(1, 301))},
                  "jobs": [{"issue": n, "status": "running"} for n in range(1, 301)],
                  "events": [{"issue": 1, "kind": "claimed"}] * 100}
        result = public_snapshot(source)
        self.assertEqual(len(result["queue"]["ready_issues"]), 100)
        self.assertEqual(len(result["jobs"]), 200)
        self.assertEqual(len(result["events"]), 40)
        self.assertTrue(result["jobs_truncated"])

    def test_quota_projection_rejects_private_and_malformed_values(self):
        source = {"quota_observations": [
            {"provider": "kimi", "source": "kimi-cli-local",
             "observed_at": "2026-11-07T10:00:00Z", "status": "ok", "unit": "percent",
             "endpoint": "https://secret.example/api?token=credential",
             "windows": [
                 {"name": "limit5h", "used_percent": 47.5,
                  "reset_at": "2026-11-07T15:00:00Z", "raw": "credential"},
                 {"name": "limit7d", "used_percent": float("nan"),
                  "reset_at": "<script>alert(1)</script>"},
                 {"name": "monthTotal", "used_percent": 101, "reset_at": None},
                 {"name": "<script>", "used_percent": 20},
                 {"name": "monthCode", "used_percent": 30}]},
            {"provider": "<img src=x>", "source": "opencode-local", "windows": []},
            {"provider": "opencode", "source": "https://secret.example", "status": "<script>",
             "unit": "credential", "observed_at": "2026-99-99T10:00:00Z",
             "windows": [{"name": "weekly", "used_percent": 20}]},
        ]}
        result = public_snapshot(source)["quota_observations"]
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["windows"][0],
                         {"name": "limit5h", "used_percent": 47.5,
                          "reset_at": "2026-11-07T15:00:00Z"})
        self.assertEqual(len(result[0]["windows"]), 3)
        self.assertIsNone(result[0]["windows"][1]["used_percent"])
        self.assertIsNone(result[0]["windows"][1]["reset_at"])
        self.assertIsNone(result[1]["source"])
        self.assertEqual(result[1]["status"], "unknown")
        self.assertEqual(result[1]["windows"], [])
        payload = json.dumps(result)
        html = render_html(public_snapshot(source)).decode("utf-8")
        for secret in ("credential", "secret.example", "<script>", "<img"):
            self.assertNotIn(secret, payload)
            self.assertNotIn(secret, html)
        self.assertIn("47.5", html)
        self.assertIn("2026-11-07T15:00:00Z", html)

    def test_quota_collections_are_bounded_and_html_escapes_text(self):
        source = {"quota_observations": [
            {"provider": f"provider{n}", "source": "kimi-cli-local", "unit": "percent",
             "windows": [{"name": "limit5h", "used_percent": 1}] * 6}
            for n in range(40)]}
        projected = public_snapshot(source)
        self.assertEqual(len(projected["quota_observations"]), 32)
        self.assertEqual(len(projected["quota_observations"][0]["windows"]), 4)
        projected["quota_observations"][0]["provider"] = "<unsafe&>"
        self.assertIn("&lt;unsafe&amp;&gt;", render_html(projected).decode("utf-8"))

    def test_guidance_projection_rejects_unknown_or_mismatched_values(self):
        source = {"jobs": [
            {"issue": 1, "status": "retry_wait", "reason_code": "provider_capacity_wait",
             "next_action": "wait_for_controller_reconciliation",
             "last_recorded_action": "quota_paused"},
            {"issue": 2, "status": "blocked", "reason_code": "provider_capacity_wait",
             "next_action": "wait_for_controller_reconciliation",
             "last_recorded_action": ["private log"]},
            {"issue": 3, "status": "blocked", "reason_code": "<script>",
             "next_action": "secret", "last_recorded_action": "private"},
            {"issue": 4, "status": "blocked", "reason_code": "provider_auth",
             "next_action": "verify_provider_credential_manually"},
            {"issue": 5, "status": "blocked", "reason_code": "local_check_failed",
             "next_action": "inspect_local_check_results"},
            {"issue": 6, "status": "retry_wait", "reason_code": "provider_quota",
             "next_action": "wait_for_controller_reconciliation"},
            {"issue": 7, "status": "retry_wait", "reason_code": "publication_unconfirmed",
             "next_action": "reconcile_publication_remotely"},
            {"issue": 8, "status": "blocked", "reason_code": "broker_unavailable",
             "next_action": "inspect_broker_manually"},
            {"issue": 9, "status": "blocked", "reason_code": "publish_credential_unavailable",
             "next_action": "verify_fork_credential_manually"},
            {"issue": 10, "status": "blocked", "reason_code": "publication_unconfirmed",
             "next_action": "retry_now"}]}
        rows = public_snapshot(source)["jobs"]
        self.assertEqual(rows[0]["reason_code"], "provider_capacity_wait")
        self.assertEqual(rows[0]["last_recorded_action"], "quota_paused")
        for row in rows[1:3]:
            self.assertIsNone(row["reason_code"])
            self.assertIsNone(row["next_action"])
            self.assertIsNone(row["last_recorded_action"])
        self.assertEqual([row["reason_code"] for row in rows[3:9]],
                         ["provider_auth", "local_check_failed", "provider_quota",
                          "publication_unconfirmed", "broker_unavailable",
                          "publish_credential_unavailable"])
        self.assertIsNone(rows[9]["reason_code"])
        self.assertNotIn("private log", json.dumps(rows))
        self.assertNotIn("secret", json.dumps(rows))

    def test_server_loopback_only_and_read_only_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = {"queue": {"count": None, "ready_issues": None},
                      "slots": {"capacity": 2, "active": 0, "available": 2},
                      "jobs": [], "events": [], "provider_cooldown": [],
                      "quota_observations": [{
                          "provider": "kimi", "source": "kimi-cli-local",
                          "observed_at": "2026-11-07T10:00:00Z", "status": "ok",
                          "unit": "percent", "windows": [{"name": "limit5h",
                          "used_percent": 47.5, "reset_at": "2026-11-07T15:00:00Z"}],
                          "raw": "private-credential"}]}
            with patch("ermik.dashboard.status_snapshot", return_value=source):
                server = create_server(Path(tmp), max_parallel=2, port=0)
                self.assertEqual(server.server_address[0], "127.0.0.1")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("GET", "/api/status")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
                    api = json.loads(response.read())
                    self.assertEqual(api["slots"]["available"], 2)
                    self.assertEqual(api["quota_observations"][0]["windows"][0]["used_percent"], 47.5)
                    self.assertNotIn("private-credential", json.dumps(api))
                    conn.close()

                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("GET", "/")
                    html = conn.getresponse().read().decode("utf-8")
                    self.assertIn("47.5", html)
                    self.assertIn("kimi-cli-local", html)
                    self.assertNotIn("private-credential", html)
                    conn.close()

                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("GET", "/", headers={"Host": "example.com"})
                    self.assertEqual(conn.getresponse().status, 400)
                    conn.close()

                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("POST", "/api/status")
                    self.assertEqual(conn.getresponse().status, 405)
                    conn.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
