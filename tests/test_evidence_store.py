"""Durability and rejection tests for local evidence files."""

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ermik.evidence_store import MAX_EVIDENCE_BYTES, read_evidence, write_evidence


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "proof.json"

    def test_round_trip_replaces_existing_evidence(self):
        write_evidence(self.path, {"issue": 7, "checks": ["passed"]})
        self.assertEqual(read_evidence(self.path), {"issue": 7, "checks": ["passed"]})
        write_evidence(self.path, {"issue": 7, "pr": 12})
        self.assertEqual(read_evidence(self.path), {"issue": 7, "pr": 12})
        self.assertEqual(list(self.root.iterdir()), [self.path])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_shared_evidence_mode(self):
        write_evidence(self.path, {"pr": 12}, mode=0o640)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(read_evidence(self.path), {"pr": 12})

    def test_replace_failure_preserves_previous_record_and_cleans_temp(self):
        write_evidence(self.path, {"valid": True})
        original = self.path.read_bytes()
        with patch("ermik.evidence_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                write_evidence(self.path, {"valid": False})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_directory_sync_failure_leaves_complete_new_record(self):
        write_evidence(self.path, {"valid": True})
        with patch("ermik.evidence_store._fsync_directory",
                   side_effect=OSError("directory sync failed")):
            with self.assertRaises(OSError):
                write_evidence(self.path, {"valid": False})
        self.assertEqual(read_evidence(self.path), {"valid": False})
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_reject_symlink_and_unsafe_existing_path(self):
        target = self.root / "target.json"
        target.write_text('{"valid":true}', encoding="utf-8")
        link = self.root / "link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        for operation in (lambda: read_evidence(link),
                          lambda: write_evidence(link, {"valid": False})):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(target.read_text(encoding="utf-8"), '{"valid":true}')
        self.path.mkdir()
        with self.assertRaises(ValueError):
            write_evidence(self.path, {"x": 1})

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_reject_world_writable_existing_file(self):
        self.path.write_text("{}", encoding="utf-8")
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            read_evidence(self.path)
        with self.assertRaises(ValueError):
            write_evidence(self.path, {"x": 1})

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_reject_unsafe_parent_directory(self):
        self.root.chmod(0o777)
        with self.assertRaises(ValueError):
            write_evidence(self.path, {"x": 1})

    def test_reject_oversized_write_and_read_without_replacing_old_record(self):
        write_evidence(self.path, {"valid": True})
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            write_evidence(self.path, {"data": "x" * MAX_EVIDENCE_BYTES})
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_bytes(b" " * (MAX_EVIDENCE_BYTES + 1))
        with self.assertRaises(ValueError):
            read_evidence(self.path)

    def test_reject_truncated_duplicate_and_non_object_json(self):
        for content in (b'{"valid":', b'{"x":1,"x":2}', b'[1,2]', b'{"x":NaN}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(ValueError):
                    read_evidence(self.path)


if __name__ == "__main__":
    unittest.main()
