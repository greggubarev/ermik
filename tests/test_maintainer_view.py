"""Focused queue projection and loopback boundary tests."""

import http.client
import threading
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.maintainer_view import queue_snapshot, render_html, create_server


HEAD = "a" * 40


class MaintainerViewTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config("owner/project", "keeper", None, None, "image", ("run",),
                          (("check",),), 1, 60, (), "main")
        self.pulls = [{"number": 12, "head": {"ref": "work/issue-7-fix"}}]

    def snapshot(self, packet, *, ready=(), labels=("state:in-progress", "route:local")):
        with (patch("ermik.maintainer_view.GitHub") as gh,
              patch("ermik.maintainer_view.build_review_packet", return_value=packet)):
            gh.return_value.ready_issues.return_value = list(ready)
            gh.return_value._pages.return_value = self.pulls
            gh.return_value.issue.return_value = {
                "labels": [{"name": label} for label in labels]}
            return queue_snapshot(self.cfg), gh.return_value.issue.call_count

    def packet(self, status="current", review="missing", evidence="current"):
        return {"status": status, "issue": 7, "head": HEAD,
                "head_verified": True, "review": {"status": review},
                "evidence": {"status": evidence, "observation": "publisher_claim",
                             "model_ref": "provider/model",
                             "checks": [{"result": "passed"}]}}

    def test_current_review_packet_and_remote_label_are_separate(self):
        snapshot, calls = self.snapshot(self.packet())
        row = snapshot["items"][0]
        self.assertEqual(calls, 1)
        self.assertEqual(row["head"], HEAD)
        self.assertEqual(row["checks"], "publisher_claim_passed")
        self.assertEqual(row["controller_runtime"], "unverified")
        self.assertEqual(row["model_route_label"], "route:local")
        self.assertEqual(row["publisher_model_claim"], "provider/model")
        self.assertEqual(row["next_action"], "review_current_head")

    def test_stale_missing_blocked_and_correction_never_look_approved(self):
        for status in ("missing", "stale", "incomplete"):
            with self.subTest(status=status):
                row = self.snapshot(self.packet(status, "stale", status),
                                    labels=("state:blocked",))[0]["items"][0]
                self.assertEqual(row["remote_label_state"], "blocked")
                self.assertEqual(row["checks"], "unverified")
                self.assertEqual(row["next_action"], "reconcile_evidence_then_review")
        corrected = self.snapshot(self.packet("current", "stale"))[0]["items"][0]
        self.assertEqual(corrected["next_action"], "review_current_head")
        self.assertEqual(corrected["review"], "stale")

    def test_redacted_identity_suppresses_route_and_head(self):
        packet = {"status": "stale", "reason": "remote_identity"}
        row, calls = self.snapshot(packet)
        self.assertEqual(calls, 0)
        self.assertIsNone(row["items"][0]["issue"])
        self.assertIsNone(row["items"][0]["head"])
        self.assertEqual(row["items"][0]["model_route_label"], "unknown")
        self.assertIsNone(row["items"][0]["publisher_model_claim"])
        self.assertEqual(row["items"][0]["next_action"],
                         "reconcile_remote_identity_manually")

    def test_admitted_waiting_has_no_claimed_runtime(self):
        self.pulls = []
        row = self.snapshot({}, ready=(7,))[0]["items"][0]
        self.assertEqual(row["remote_label_state"], "ready")
        self.assertEqual(row["evidence"], "missing")
        self.assertEqual(row["controller_runtime"], "unverified")

    def test_html_escapes_and_http_rejects_writes_and_external_host(self):
        html = render_html({"items": [{key: "<script>" for key in (
            "issue", "pr", "head", "remote_label_state", "controller_runtime",
            "model_route_label", "publisher_model_claim", "evidence", "checks",
            "review", "next_action")}]})
        self.assertNotIn(b"<script>", html)
        with patch("ermik.maintainer_view.queue_snapshot", return_value={
                "repo": "owner/project", "items": []}):
            server = create_server(self.cfg, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", "/api/queue")
                self.assertEqual(conn.getresponse().status, 200)
                conn.request("POST", "/api/queue")
                self.assertEqual(conn.getresponse().status, 405)
                conn.request("GET", "/api/queue", headers={"Host": "example.com"})
                self.assertEqual(conn.getresponse().status, 400)
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
