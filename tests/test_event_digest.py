"""Focused delivery and recovery checks for the optional digest."""

import json
import tempfile
from urllib.error import HTTPError
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ermik import state
from ermik.event_digest import deliver_once, _send


HEAD = "a" * 40
BASE = "b" * 40
ENV = {"ERMIK_DIGEST_URL": "https://example.test/hook",
       "ERMIK_DIGEST_TOKEN": "private-token"}


class FakeGitHub:
    def issue(self, number, *, allow_closed=False):
        return {"number": number, "state": "open"}

    def pr(self, number):
        return {"number": number, "headRefName": "work/issue-7-test",
                "headRefOid": HEAD, "headRepository": {"nameWithOwner": "owner/repo"},
                "baseRefName": "main", "body": "Closes #7", "isDraft": True}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = SimpleNamespace(repo="owner/repo", state_dir=root,
                                   publish_repo=None, publish_token_env=None,
                                   base_branch="main")
        state.initialize(root)
        with state.connect(root) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,allowed_json,error) "
                         "VALUES(7,'work/issue-7-test',?,?,23,'awaiting_review','[]',?)",
                         (BASE, HEAD, "private task output"))

    def event(self, kind):
        with state.connect(self.cfg.state_dir) as conn:
            state.event(conn, 7, kind, head_sha=HEAD)

    def test_disabled_by_default(self):
        self.event("pr_published")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(deliver_once(self.cfg)["status"], "disabled")
        self.assertFalse((self.cfg.state_dir / "event-digest.json").exists())

    def test_failure_restart_rate_limit_and_redaction(self):
        self.event("pr_published")
        sent = []

        def fail_then_succeed(url, token, payload, key):
            sent.append((url, token, payload, key))
            return 120 if len(sent) == 1 else None

        with patch.dict("os.environ", ENV), patch(
                "ermik.event_digest._send", side_effect=fail_then_succeed):
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=100)["status"],
                             "retry_wait")
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=219)["status"],
                             "rate_limited")
            self.assertEqual(len(sent), 1)
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=220)["status"],
                             "delivered")
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=280)["status"],
                             "empty")
        self.assertEqual(sent[0][3], sent[1][3])
        self.assertEqual(sent[0][2], {
            "version": 1, "event": "draft_pr", "issue": 7,
            "issue_url": "https://github.com/owner/repo/issues/7", "pr": 23,
            "pr_url": "https://github.com/owner/repo/pull/23"})
        self.assertNotIn("private task output", json.dumps([row[2] for row in sent]))
        self.assertNotIn("private-token", (self.cfg.state_dir / "event-digest.json").read_text())

    def test_unverified_identity_never_sends(self):
        self.event("pr_published")

        class Wrong(FakeGitHub):
            def pr(self, number):
                return {**super().pr(number), "body": "Closes #8"}

        with patch.dict("os.environ", ENV), patch("ermik.event_digest._send") as send:
            self.assertEqual(deliver_once(self.cfg, gh=Wrong(), now=100)["status"],
                             "identity_unverified")
            send.assert_not_called()

    def test_blocked_and_completed_are_bounded(self):
        self.event("blocked")
        self.event("merged")
        events = []
        with patch.dict("os.environ", ENV), patch(
                "ermik.event_digest._send", side_effect=lambda u, t, p, k:
                events.append(p["event"])):
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=100)["status"],
                             "delivered")
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=160)["status"],
                             "delivered")
        self.assertEqual(events, ["blocked", "completed"])

    def test_corrected_publication_is_not_new_draft(self):
        self.event("pr_published")
        with patch.dict("os.environ", ENV), patch(
                "ermik.event_digest._send", return_value=None) as send:
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=100)["status"],
                             "delivered")
            self.event("pr_published")
            self.assertEqual(deliver_once(self.cfg, gh=FakeGitHub(), now=160)["status"],
                             "empty")
            send.assert_called_once()

    def test_requested_correction_from_verified_decision_trail(self):
        root = self.cfg.state_dir / "decision-trail"
        root.mkdir()
        common = {"repo": "owner/repo", "issue": 7, "pr": 23,
                  "base": BASE, "head": HEAD, "evidence_sha256": "c" * 64,
                  "timestamp": "2026-11-09T00:00:00+00:00"}
        (root / "pr-23.json").write_text(json.dumps({
            "version": 1, "repo": "owner/repo", "pr": 23,
            "entries": [{**common, "decision": "reviewed"},
                        {**common, "decision": "changes_requested"}]}), encoding="utf-8")
        received = []
        with patch.dict("os.environ", ENV), patch(
                "ermik.event_digest._send", side_effect=lambda u, t, p, k:
                received.append(p) or None):
            result = deliver_once(self.cfg, gh=FakeGitHub(), now=100)
        self.assertEqual(result["event"], "correction_requested")
        self.assertEqual(received[0]["pr_url"], "https://github.com/owner/repo/pull/23")

    def test_http_rate_limit_respects_bounded_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "7200"

        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                raise HTTPError(request.full_url, 429, "limited", headers, None)

        opener = Opener()
        with patch("ermik.event_digest.build_opener", return_value=opener):
            delay = _send("https://example.test/hook", "private-token",
                          {"event": "blocked"}, "a" * 64)
        self.assertEqual(delay, 3600)
        self.assertEqual(opener.timeout, 5)
        self.assertEqual(opener.request.get_header("Idempotency-key"), "a" * 64)


if __name__ == "__main__":
    unittest.main()
