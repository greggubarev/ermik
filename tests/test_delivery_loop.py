"""Automatic evidence delivery retries and exact-HEAD ledger behavior."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik import state
from ermik.config import Config
from ermik.delivery_loop import deliver_pending


HEAD_A = "a" * 40
HEAD_B = "b" * 40
BASE = "c" * 40


class DeliveryLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = Config("owner/repo", "maintainer", root / "state", root / "work",
                          "image:tag", ("run",), (("check",),), 1, 10, (), "main",
                          evidence_publisher="publisher", evidence_publisher_id=123)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,"
                         "allowed_json) VALUES(?,?,?,?,?,?,?)",
                         (7, "work/issue-7-delivery", BASE, HEAD_A, 12,
                          "awaiting_review", '["src"]'))

    def test_failure_retries_and_delivered_head_is_not_reposted(self):
        calls = []

        def publish(cfg, pr):
            self.assertEqual(pr, 12)
            # The GitHub call may need the state lock for export; this proves
            # the loop releases its lock before publication.
            with state.exclusive(cfg.state_dir):
                pass
            calls.append(pr)
            if len(calls) == 1:
                raise RuntimeError("temporary GitHub failure")
            return {"status": "published", "head": HEAD_A}

        with patch("ermik.delivery_loop.evidence_transport.publish_manifest",
                   side_effect=publish):
            self.assertEqual(deliver_pending(self.cfg), [
                {"issue": 7, "pr": 12, "head": HEAD_A, "status": "failed"}])
            with state.connect(self.cfg.state_dir) as conn:
                self.assertEqual(state.job(conn, 7)["status"], "awaiting_review")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(deliver_pending(self.cfg)[0]["status"], "delivered")
            self.assertEqual(deliver_pending(self.cfg), [])
        self.assertEqual(len(calls), 2)

    def test_correction_creates_new_pending_head(self):
        seen = []

        def publish(cfg, pr):
            with state.connect(cfg.state_dir) as conn:
                head = state.job(conn, 7)["head_sha"]
            seen.append(head)
            return {"status": "existing", "head": head}

        with patch("ermik.delivery_loop.evidence_transport.publish_manifest",
                   side_effect=publish):
            self.assertEqual(deliver_pending(self.cfg)[0]["status"], "delivered")
            with state.connect(self.cfg.state_dir) as conn:
                conn.execute("UPDATE jobs SET head_sha=? WHERE issue=7", (HEAD_B,))
            self.assertEqual(deliver_pending(self.cfg)[0]["status"], "delivered")
        self.assertEqual(seen, [HEAD_A, HEAD_B])
        with state.connect(self.cfg.state_dir) as conn:
            heads = [row[0] for row in conn.execute(
                "SELECT head_sha FROM events WHERE kind='evidence_delivered' "
                "ORDER BY id")]
        self.assertEqual(heads, [HEAD_A, HEAD_B])

    def test_head_changed_during_publish_is_not_marked_delivered(self):
        def publish(cfg, pr):
            with state.connect(cfg.state_dir) as conn:
                conn.execute("UPDATE jobs SET head_sha=? WHERE issue=7", (HEAD_B,))
            return {"status": "published", "head": HEAD_A}

        with patch("ermik.delivery_loop.evidence_transport.publish_manifest",
                   side_effect=publish):
            self.assertEqual(deliver_pending(self.cfg)[0]["status"], "superseded")
        with state.connect(self.cfg.state_dir) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_durable_cursor_reaches_ninth_job_after_eight_failures(self):
        with state.connect(self.cfg.state_dir) as conn:
            for issue in (1, 2, 3, 4, 5, 6, 8, 9):
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,"
                             "status,allowed_json) VALUES(?,?,?,?,?,?,?)",
                             (issue, f"work/issue-{issue}-delivery", BASE,
                              f"{issue:040x}", issue + 100, "awaiting_review",
                              '["src"]'))
        attempted = []

        def publish(cfg, pr):
            attempted.append(pr)
            if pr != 109:
                raise RuntimeError("persistent failure")
            return {"status": "published", "head": f"{9:040x}"}

        with patch("ermik.delivery_loop.evidence_transport.publish_manifest",
                   side_effect=publish):
            first = deliver_pending(self.cfg, limit=8)
            self.assertEqual([item["issue"] for item in first], list(range(1, 9)))
            self.assertTrue(all(item["status"] == "failed" for item in first))
            # A separate invocation loads the cursor from the private file.
            second = deliver_pending(self.cfg, limit=8)
        self.assertEqual(second[0]["issue"], 9)
        self.assertEqual(second[0]["status"], "delivered")
        self.assertEqual(attempted[:9], [101, 102, 103, 104, 105, 106, 12, 108, 109])
        with state.connect(self.cfg.state_dir) as conn:
            delivered = conn.execute("SELECT issue FROM events WHERE "
                                     "kind='evidence_delivered'").fetchall()
        self.assertEqual([row[0] for row in delivered], [9])


if __name__ == "__main__":
    unittest.main()
