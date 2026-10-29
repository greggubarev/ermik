"""Aggregate usage without retaining prompts, responses, or credentials."""

from pathlib import Path
import json
import tempfile
import unittest

from ermik import state
from ermik.observability import status_snapshot
from ermik.usage import parse_worker_usage


class UsageTests(unittest.TestCase):
    def test_usage_marker_validation_and_no_raw_text(self):
        metric = {"complete": True, "input": 20, "output": 3,
                  "reasoning": 1, "cache_read": 5, "cache_write": 0,
                  "total": 29, "estimated_cost": 0.01}
        raw = b"private task text\nWORKER_USAGE " + json.dumps(metric).encode() + b"\nWORKER_OK\n"
        self.assertEqual(parse_worker_usage(raw), metric)
        self.assertIsNone(parse_worker_usage(b"WORKER_USAGE {\"complete\":true,\"input\":-1}\n"))
        self.assertIsNone(parse_worker_usage(b"WORKER_USAGE nonsense\n"))

    def test_status_separates_measured_and_unknown_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with state.connect(root) as conn:
                conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                             "VALUES(1,'work/issue-1','a','awaiting_review','[\"src\"]')")
                state.record_usage(conn, "attempt-1", 1, "provider", "model",
                                   {"complete": True, "input": 20, "output": 3,
                                    "reasoning": 1, "cache_read": 5, "cache_write": 0,
                                    "total": 29, "estimated_cost": 0.01}, 0)
                state.record_usage(conn, "attempt-2", 1, "provider", "model", None, 1)
            snapshot = status_snapshot(root, max_parallel=1)
            usage = snapshot["usage"][0]
            self.assertEqual((usage["attempts"], usage["measured_attempts"],
                              usage["unknown_attempts"]), (2, 1, 1))
            self.assertEqual(usage["total_tokens"], 29)
            self.assertNotIn("private task text", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
