"""Process-kill checks for durable evidence replacement.

Run on Linux to include directory sync: python3 -m unittest
tests.test_evidence_process_kills -v
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ermik import evidence_store


OLD = {"issue": 1, "head": "a" * 40}
NEW = {"issue": 1, "head": "b" * 40}


def _child(case: str, path: Path) -> None:
    if case in {"before_write", "partial_write"}:
        def interrupted_fdopen(fd, mode):
            if case == "partial_write":
                os.write(fd, b'{"issue":1,')
                os.fsync(fd)
            os._exit(0)

        evidence_store.os.fdopen = interrupted_fdopen
    elif case == "after_replace":
        original = evidence_store.os.replace

        def interrupted_replace(source, destination):
            original(source, destination)
            os._exit(0)

        evidence_store.os.replace = interrupted_replace
    else:
        raise ValueError("unknown case")
    evidence_store.write_evidence(path, NEW)
    raise AssertionError("fault was not triggered")


class EvidenceProcessKillTests(unittest.TestCase):
    def test_replacement_windows(self):
        for case in ("before_write", "partial_write", "after_replace"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "evidence.json"
                evidence_store.write_evidence(path, OLD)
                child = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--child", case,
                     str(path)], capture_output=True, timeout=15, check=False)
                self.assertEqual(child.returncode, 0, case)
                self.assertEqual(evidence_store.read_evidence(path),
                                 NEW if case == "after_replace" else OLD)
                for sibling in path.parent.glob(".evidence.json.*.tmp"):
                    with self.assertRaises(ValueError):
                        evidence_store.read_evidence(sibling)

    def test_truncated_final_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_bytes(b'{"issue":1,')
            with self.assertRaisesRegex(ValueError, "invalid evidence JSON"):
                evidence_store.read_evidence(path)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        _child(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
