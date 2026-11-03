"""Review records stay complete and private across replacement failures."""

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.review import _load, _save


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.cfg = Config("owner/repo", "owner", root / "state", root / "work",
                          "demo:latest", ("true",), (("true",),), 2, 60,
                          (), "main", (), 0)
        self.path = self.cfg.state_dir / "reviews" / "pr-29.json"

    def test_round_trip_replaces_review_and_post_merge_record(self):
        review = {"pr": 29, "issue": 29, "head": "a" * 40,
                  "decision": "reviewed"}
        merged = {**review, "merged": {"merge_commit": "b" * 40}}
        _save(self.cfg, 29, review)
        self.assertEqual(_load(self.cfg, 29), review)
        _save(self.cfg, 29, merged)
        self.assertEqual(_load(self.cfg, 29), merged)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_replace_failure_preserves_complete_prior_record(self):
        prior = {"pr": 29, "head": "a" * 40}
        _save(self.cfg, 29, prior)
        original = self.path.read_bytes()
        with patch("ermik.evidence_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                _save(self.cfg, 29, {"pr": 29, "head": "b" * 40})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(_load(self.cfg, 29), prior)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_directory_sync_failure_leaves_complete_new_record(self):
        _save(self.cfg, 29, {"pr": 29, "head": "a" * 40})
        updated = {"pr": 29, "head": "b" * 40}
        with patch("ermik.evidence_store._fsync_directory",
                   side_effect=OSError("directory sync failed")):
            with self.assertRaises(OSError):
                _save(self.cfg, 29, updated)
        self.assertEqual(_load(self.cfg, 29), updated)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_load_rejects_truncated_duplicate_and_non_object_json(self):
        _save(self.cfg, 29, {"valid": True})
        for content in (b'{"valid":', b'{"head":"a","head":"b"}',
                        b'[1,2]', b'{"head":NaN}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(ValueError):
                    _load(self.cfg, 29)

    def test_oversized_update_preserves_prior_review(self):
        prior = {"pr": 29, "decision": "reviewed"}
        _save(self.cfg, 29, prior)
        with self.assertRaisesRegex(RuntimeError, "review record unexpectedly large"):
            _save(self.cfg, 29, {"pr": 29, "payload": "x" * 20000})
        self.assertEqual(_load(self.cfg, 29), prior)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_oversized_existing_record_is_not_loaded(self):
        _save(self.cfg, 29, {"pr": 29})
        self.path.write_bytes(b" " * 20001)
        with self.assertRaisesRegex(RuntimeError, "review record unexpectedly large"):
            _load(self.cfg, 29)

    def test_load_and_save_reject_symlink(self):
        _save(self.cfg, 29, {"valid": True})
        target = self.path.parent / "target.json"
        target.write_bytes(b'{"valid":true}')
        self.path.unlink()
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            _load(self.cfg, 29)
        with self.assertRaises(ValueError):
            _save(self.cfg, 29, {"valid": False})
        self.assertEqual(target.read_bytes(), b'{"valid":true}')

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_legacy_0644_record_in_private_directory_upgrades_on_save(self):
        _save(self.cfg, 29, {"pr": 29})
        self.path.chmod(0o644)
        self.assertEqual(_load(self.cfg, 29), {"pr": 29})
        _save(self.cfg, 29, {"pr": 29, "decision": "reviewed"})
        self.assertEqual(_load(self.cfg, 29), {"pr": 29, "decision": "reviewed"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_group_writable_review_record_is_rejected(self):
        _save(self.cfg, 29, {"pr": 29})
        self.path.chmod(0o660)
        with self.assertRaises(ValueError):
            _load(self.cfg, 29)
        with self.assertRaises(ValueError):
            _save(self.cfg, 29, {"pr": 30})


if __name__ == "__main__":
    unittest.main()
