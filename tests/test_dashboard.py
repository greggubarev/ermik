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
                      "error": "secret-token",
                      "branch": "private-branch"}],
            "events": [{"issue": 7, "kind": "model_selected", "model": "provider/model",
                        "created_at": "2026-10-25 12:00:00", "head_sha": "private-sha"},
                       {"issue": 7, "kind": "<script>", "model": "evil"}],
            "provider_cooldown": [{"provider": "provider", "retry_at": 1792930000}],
        }
        projected = public_snapshot(source)
        self.assertEqual(projected["jobs"][0]["model"], "provider/model")
        payload = json.dumps(projected)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("private-branch", payload)
        self.assertNotIn("private-sha", payload)
        self.assertNotIn("<script>", payload)
        html = render_html(projected).decode("utf-8")
        self.assertIn("provider/model", html)
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

    def test_server_loopback_only_and_read_only_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = {"queue": {"count": None, "ready_issues": None},
                      "slots": {"capacity": 2, "active": 0, "available": 2},
                      "jobs": [], "events": [], "provider_cooldown": []}
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
                    self.assertEqual(json.loads(response.read())["slots"]["available"], 2)
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
