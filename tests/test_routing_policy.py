"""Quality routing requires complete, fresh, internally consistent evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from ermik.providers import ModelCandidate
from ermik.routing_policy import audit_route_order, order_candidates


KIMI = ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY")
GO = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY")
GO_ALT = ModelCandidate("opencode-go", "other", "OPENCODE_GO_API_KEY")
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RoutingConfig:
    repo: str
    quality_metrics_file: Path | None
    quality_min_samples: int = 2
    quality_max_age_seconds: int = 3600


class QualityRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "quality.json"
        self.cfg = RoutingConfig("owner/repo", self.path)
        self.snapshot = {
            "version": 1, "repo": "owner/repo", "observed_at": NOW.isoformat(),
            "providers": {
                "kimi-code": {"samples": 4, "first_review_acceptances": 1,
                              "quality_score": 0.25},
                "opencode-go": {"samples": 4, "first_review_acceptances": 3,
                                "quality_score": 0.75},
            },
        }

    def write(self, value=None):
        self.path.write_text(json.dumps(self.snapshot if value is None else value),
                             encoding="utf-8")
        if os.name == "posix":
            self.path.chmod(0o600)

    def assert_priority(self):
        self.assertEqual(order_candidates(self.cfg, (KIMI, GO), NOW),
                         ((KIMI, GO), "priority"))

    def test_quality_reorders_and_reproduces_stable_ties(self):
        self.write()
        expected = ((GO, GO_ALT, KIMI), "quality")
        self.assertEqual(order_candidates(self.cfg, (KIMI, GO, GO_ALT), NOW), expected)
        self.assertEqual(order_candidates(self.cfg, (KIMI, GO, GO_ALT), NOW), expected)
        self.snapshot["providers"]["opencode-go"] = {
            "samples": 4, "first_review_acceptances": 1, "quality_score": 0.25}
        self.write()
        self.assertEqual(order_candidates(self.cfg, (KIMI, GO, GO_ALT), NOW),
                         ((KIMI, GO, GO_ALT), "quality"))

    def test_single_provider_and_disabled_source_preserve_priority(self):
        self.write()
        self.assertEqual(order_candidates(self.cfg, (GO, GO_ALT), NOW),
                         ((GO, GO_ALT), "priority"))
        self.assertEqual(order_candidates(RoutingConfig("owner/repo", None),
                                          (KIMI, GO), NOW), ((KIMI, GO), "priority"))
        self.assertEqual(order_candidates(self.cfg, (), NOW), ((), "priority"))

    def test_missing_stale_future_and_wrong_repo_fall_back(self):
        self.assert_priority()
        self.snapshot["observed_at"] = (NOW - timedelta(seconds=3601)).isoformat()
        self.write()
        self.assert_priority()
        self.snapshot["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
        self.write()
        self.assert_priority()
        self.snapshot["observed_at"] = NOW.isoformat()
        self.snapshot["repo"] = "other/repo"
        self.write()
        self.assert_priority()

    def test_insufficient_or_missing_provider_falls_back_as_a_whole(self):
        self.snapshot["providers"]["opencode-go"] = {
            "samples": 1, "first_review_acceptances": 1, "quality_score": 1.0}
        self.write()
        self.assert_priority()
        del self.snapshot["providers"]["opencode-go"]
        self.write()
        self.assert_priority()

    def test_malformed_counts_scores_timestamp_and_duplicate_keys_fall_back(self):
        invalid_rows = (
            {"samples": True, "first_review_acceptances": 1, "quality_score": 1.0},
            {"samples": 2, "first_review_acceptances": 3, "quality_score": 1.0},
            {"samples": 2, "first_review_acceptances": 1, "quality_score": 0.9},
            {"samples": 0, "first_review_acceptances": 0, "quality_score": 0.0},
            {"samples": 2, "first_review_acceptances": 1, "quality_score": None},
            {"samples": 2, "first_review_acceptances": 1, "quality_score": 0.5,
             "unknown": 1},
        )
        for row in invalid_rows:
            with self.subTest(row=row):
                self.snapshot["providers"]["opencode-go"] = row
                self.write()
                self.assert_priority()
        self.snapshot["observed_at"] = "2026-11-07T12:00:00+01:00"
        self.write()
        self.assert_priority()
        self.path.write_text('{"version":1,"version":1}', encoding="utf-8")
        self.assert_priority()

    def test_oversize_public_file_and_symlink_fall_back(self):
        self.path.write_bytes(b" " * (64 * 1024 + 1))
        self.assert_priority()
        self.write()
        if os.name == "posix":
            self.path.chmod(0o644)
            self.assert_priority()
        target = Path(self.temp.name) / "target.json"
        self.path.replace(target)
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assert_priority()

    def test_audit_refusal_preserves_active_runtime_quality_order(self):
        self.write()
        missing_audit = Path(self.temp.name) / "missing-audit.json"
        result = audit_route_order(self.cfg, {}, missing_audit, (KIMI, GO), NOW)
        self.assertEqual(result["configured_order"], [KIMI.ref, GO.ref])
        self.assertEqual(result["current_policy"], "quality")
        self.assertEqual(result["current_order"], [GO.ref, KIMI.ref])
        self.assertEqual(result["proposed_order"], [GO.ref, KIMI.ref])
        self.assertEqual(result["fallback"], "current_runtime_order")
        self.assertEqual(result["decision"], "refused")


if __name__ == "__main__":
    unittest.main()
