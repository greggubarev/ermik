"""Exact proof and crash recovery checks for the private decision journal."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.decision_trail import (MAX_BYTES, MAX_ENTRIES, append_decision,
                                  check_decision, read_decisions, review_digest)
from ermik.evidence_store import write_evidence


HEAD = "b" * 40
BASE = "a" * 40
IMAGE = "sha256:" + "c" * 64
IMAGE_CONFIG_DIGEST = "sha256:" + "f" * 64


class DecisionTrailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cfg = Config("owner/project", "keeper", root / "state", root / "work",
                          "image:stable", ("run",), (("check", "one"), ("check", "two")),
                          1, 60, (), "main")
        self.path = self.cfg.state_dir / "decision-trail" / "pr-12.json"

    def record(self, first_hash="d" * 64):
        return {"repo": self.cfg.repo, "issue": 7, "pr": 12,
                "base": BASE, "head": HEAD, "image_id": IMAGE,
                "image_config_digest": IMAGE_CONFIG_DIGEST,
                "ci": {"head": HEAD, "committed_diff_base": BASE,
                       "image_id": IMAGE,
                       "image_config_digest": IMAGE_CONFIG_DIGEST,
                       "result": "passed",
                       "checks": [
                           {"command": ["check", "one"], "exit_code": 0,
                            "log_sha256": first_hash, "log": "/private/one.log"},
                           {"command": ["check", "two"], "exit_code": 0,
                            "log_sha256": "e" * 64, "log": "/private/two.log"}]}}

    def test_append_and_duplicate_are_idempotent_and_private(self):
        record = self.record()
        first = append_decision(self.cfg, record, "reviewed")
        self.assertEqual(append_decision(self.cfg, record, "reviewed"), first)
        journal = read_decisions(self.cfg, 12)
        self.assertEqual(journal["entries"], [first])
        self.assertEqual(first["evidence_sha256"], review_digest(self.cfg, record))
        self.assertEqual(set(first), {"repo", "issue", "pr", "base", "head",
                                      "evidence_sha256", "decision", "timestamp"})
        self.assertNotIn(str(self.cfg.state_dir), self.path.read_text())
        self.assertNotIn("/private", self.path.read_text())

    def test_changed_proof_and_decision_append(self):
        first = append_decision(self.cfg, self.record(), "reviewed")
        changed = append_decision(self.cfg, self.record("f" * 64), "reviewed")
        requested = append_decision(self.cfg, self.record("f" * 64),
                                    "changes_requested")
        self.assertNotEqual(first["evidence_sha256"], changed["evidence_sha256"])
        self.assertEqual(len(read_decisions(self.cfg, 12)["entries"]), 3)
        self.assertEqual(requested["decision"], "changes_requested")

    def test_image_config_digest_changes_review_identity(self):
        record = self.record()
        first = append_decision(self.cfg, record, "reviewed")
        changed = self.record()
        changed["ci"]["image_config_digest"] = "sha256:" + "0" * 64
        changed["image_config_digest"] = changed["ci"]["image_config_digest"]
        second = append_decision(self.cfg, changed, "reviewed")
        self.assertNotEqual(first["evidence_sha256"], second["evidence_sha256"])
        self.assertEqual(read_decisions(self.cfg, 12)["version"], 1)

    def test_conflicting_transitions_fail_closed(self):
        record = self.record()
        with self.assertRaisesRegex(ValueError, "prior review"):
            append_decision(self.cfg, record, "merged")
        append_decision(self.cfg, record, "reviewed")
        with self.assertRaisesRegex(ValueError, "prior intent"):
            append_decision(self.cfg, record, "merged")
        append_decision(self.cfg, record, "changes_requested")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            append_decision(self.cfg, record, "merge_intent")
        self.assertEqual(len(read_decisions(self.cfg, 12)["entries"]), 2)

        changed = self.record("f" * 64)
        append_decision(self.cfg, changed, "reviewed")
        append_decision(self.cfg, changed, "merge_intent")
        append_decision(self.cfg, changed, "merge_incomplete")
        append_decision(self.cfg, changed, "merged")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            append_decision(self.cfg, changed, "changes_requested")

    def test_decision_preflight_does_not_write_and_rejects_conflict(self):
        record = self.record()
        check_decision(self.cfg, record, "reviewed")
        self.assertFalse(self.path.exists())
        append_decision(self.cfg, record, "reviewed")
        append_decision(self.cfg, record, "merge_intent")
        before = self.path.read_bytes()
        check_decision(self.cfg, record, "merge_intent")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            check_decision(self.cfg, record, "changes_requested")
        self.assertEqual(self.path.read_bytes(), before)

    def test_digest_ignores_logs_and_timestamps_but_rejects_invalid_proof(self):
        record = self.record()
        digest = review_digest(self.cfg, record)
        record["ci"]["checks"][0]["log"] = "/another/private/path"
        record["ci"]["started"] = "later"
        self.assertEqual(review_digest(self.cfg, record), digest)
        for change in (
                lambda row: row["ci"].update(result="failed"),
                lambda row: row["ci"].update(head="0" * 40),
                lambda row: row["ci"]["checks"][0].update(exit_code=1),
                lambda row: row["ci"]["checks"][0].update(log_sha256="wrong"),
                lambda row: row["ci"]["checks"].reverse(),
                lambda row: row.update(image_id="sha256:" + "0" * 64),
                lambda row: row["ci"].pop("image_config_digest"),
                lambda row: row.update(image_config_digest="sha256:" + "0" * 64),
                lambda row: row["ci"].update(image_config_digest="sha256:" + "A" * 64)):
            with self.subTest(change=change):
                bad = self.record()
                change(bad)
                with self.assertRaises(ValueError):
                    review_digest(self.cfg, bad)

    def test_malformed_existing_journal_fails_closed(self):
        append_decision(self.cfg, self.record(), "reviewed")
        for raw in (b'{"entries":', b'{"x":1,"x":2}', b'[]',
                    b'{"version":1,"repo":"other/repo","pr":12,"entries":[]}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_decisions(self.cfg, 12)
                with self.assertRaises(ValueError):
                    append_decision(self.cfg, self.record(), "merged")

    def test_interrupted_replace_preserves_prior_journal(self):
        first = append_decision(self.cfg, self.record(), "reviewed")
        with patch("ermik.evidence_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                append_decision(self.cfg, self.record(), "merge_intent")
        self.assertEqual(read_decisions(self.cfg, 12)["entries"], [first])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_directory_sync_failure_reconciles_exact_entry(self):
        append_decision(self.cfg, self.record(), "reviewed")
        append_decision(self.cfg, self.record(), "merge_intent")
        with patch("ermik.evidence_store._fsync_directory",
                   side_effect=OSError("sync failed")):
            entry = append_decision(self.cfg, self.record(), "merged")
        self.assertEqual(read_decisions(self.cfg, 12)["entries"][-1], entry)

    def test_entry_and_byte_bounds_fail_closed(self):
        first = append_decision(self.cfg, self.record(), "reviewed")
        journal = read_decisions(self.cfg, 12)
        journal["entries"] = [{**first, "issue": i} for i in range(1, MAX_ENTRIES + 1)]
        write_evidence(self.path, journal, max_bytes=MAX_BYTES)
        with self.assertRaisesRegex(ValueError, "full"):
            append_decision(self.cfg, self.record(), "merged")
        self.path.write_bytes(b" " * (MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            read_decisions(self.cfg, 12)

    def test_invalid_identity_and_decision_do_not_create_file(self):
        self.assertEqual(read_decisions(self.cfg, 12)["entries"], [])
        for decision in ("approve", 1, None):
            with self.assertRaises(ValueError):
                append_decision(self.cfg, self.record(), decision)
        bad = self.record()
        bad["pr"] = True
        with self.assertRaises(ValueError):
            append_decision(self.cfg, bad, "reviewed")
        legacy = self.record()
        legacy["ci"].pop("image_config_digest")
        with self.assertRaisesRegex(ValueError, "local CI identity"):
            append_decision(self.cfg, legacy, "reviewed")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
