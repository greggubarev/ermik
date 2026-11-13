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

    def snapshot(self, packet, *, ready=(), blocked=(),
                 labels=("queue:worker", "state:in-progress", "route:local")):
        with (patch("ermik.maintainer_view.GitHub") as gh,
              patch("ermik.maintainer_view.build_review_packet", return_value=packet)):
            def listing(endpoint):
                if "state%3Aready" in endpoint:
                    return [{"number": issue} for issue in ready]
                if "state%3Ablocked" in endpoint:
                    return [{"number": issue} for issue in blocked]
                return self.pulls
            gh.return_value.api.side_effect = listing
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

    def test_current_local_provenance_is_same_in_queue_json_and_html(self):
        packet = self.packet(review="current")
        packet["review"].update({
            "head": HEAD, "checks_sha256": "b" * 64,
            "image_id": "sha256:" + "c" * 64,
            "published_image_id": "sha256:" + "d" * 64,
            "image_config_digest": "sha256:" + "e" * 64,
            "checks": [{"index": 1, "result": "passed", "log_sha256": "f" * 64}],
        })
        snapshot = self.snapshot(packet)[0]
        self.assertEqual(snapshot["items"][0]["local_review"], packet["review"])
        html = render_html(snapshot)
        self.assertIn(b'&quot;log_sha256&quot;', html)
        self.assertIn(b"f" * 64, html)
        stale = self.packet(review="stale")
        stale["review"]["checks"] = packet["review"]["checks"]
        self.assertIsNone(self.snapshot(stale)[0]["items"][0]["local_review"])

    def test_stale_missing_blocked_and_correction_never_look_approved(self):
        for status in ("missing", "stale", "incomplete"):
            with self.subTest(status=status):
                row = self.snapshot(self.packet(status, "stale", status),
                                    labels=("queue:worker", "state:blocked"))[0]["items"][0]
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
        row = self.snapshot({}, ready=(7,), labels=(
            "queue:worker", "state:ready"))[0]["items"][0]
        self.assertEqual(row["remote_label_state"], "ready")
        self.assertEqual(row["evidence"], "missing")
        self.assertEqual(row["controller_runtime"], "unverified")

    def test_ready_and_blocked_without_pr_show_verified_route(self):
        self.pulls = []
        ready = self.snapshot({}, ready=(7,), labels=(
            "queue:worker", "state:ready", "route:local"))[0]["items"][0]
        self.assertEqual(ready["remote_label_state"], "ready")
        self.assertEqual(ready["model_route_label"], "route:local")
        blocked = self.snapshot({}, blocked=(8,), labels=(
            "queue:worker", "state:blocked", "route:local"))[0]["items"][0]
        self.assertEqual(blocked["issue"], 8)
        self.assertEqual(blocked["remote_label_state"], "blocked")
        self.assertEqual(blocked["model_route_label"], "route:local")
        self.assertEqual(blocked["next_action"],
                         "reconcile_issue_branch_and_pr_manually")
        self.assertEqual(blocked["checks"], "unverified")

    def test_listing_overflow_probes_only_one_extra_item(self):
        with patch("ermik.maintainer_view.GitHub") as gh:
            gh.return_value.api.side_effect = lambda endpoint: (
                [{"number": n} for n in range(1, 101)]
                if "per_page=100&page=1" in endpoint else [{"number": 101}])
            with self.assertRaisesRegex(ValueError, "display limit"):
                queue_snapshot(self.cfg)
            calls = [call.args[0] for call in gh.return_value.api.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertIn("per_page=1&page=101", calls[1])

    def test_html_escapes_and_http_rejects_writes_and_external_host(self):
        html = render_html({"items": [{key: "<script>" for key in (
            "issue", "pr", "head", "remote_label_state", "controller_runtime",
            "model_route_label", "publisher_model_claim", "evidence", "checks",
            "review", "local_review", "next_action")}]})
        self.assertNotIn(b"<script>", html)
        self.assertIn(b'http-equiv="refresh" content="120"', html)
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
