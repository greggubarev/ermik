"""Focused checks for release evidence bundle acceptance and rejection."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ermik import release_verify


COMMIT = "a" * 40
IMAGE = "sha256:" + "b" * 64


def _report(docker=False, platform="linux"):
    report = {
        "commit": COMMIT, "platform": platform, "result": "passed", "dirty": False,
        "unit": {"result": "passed", "run": 2, "executed": 2, "skipped": 0,
                 "allowed_skips": 0, "failures": 0, "errors": 0,
                 "unexpected_successes": 0, "unexpected_skips": [],
                 "unexpected_skips_truncated": False},
        "host": {"result": "not_requested"},
    }
    if docker:
        report["docker"] = {"server_version": "28.0", "image": "demo:fixed",
                            "image_id": IMAGE}
        report["host"] = {name: {"result": "passed", "exit_code": 0}
                          for name in ("smoke", "container_recovery")}
    return report


class ReleaseVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.windows = self.root / "windows.json"
        self.linux = self.root / "linux.json"
        self.docker = self.root / "docker.json"
        for path, report in ((self.windows, _report(platform="windows")), (self.linux, _report()),
                             (self.docker, _report(True))):
            path.write_text(json.dumps(report), encoding="utf-8")
        stem = f"ermik-source-{COMMIT[:12]}"
        self.archive = self.root / (stem + ".tar.gz")
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            payload = b"release source"
            item = tarfile.TarInfo(stem + "/README.md")
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
        with self.archive.open("wb") as target:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as writer:
                writer.write(raw.getvalue())
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.checksum = self.root / (stem + ".sha256")
        self.checksum.write_text(f"{digest}  {self.archive.name}\n", encoding="ascii")
        self.manifest = self.root / (stem + ".manifest.json")
        self.manifest.write_text(json.dumps({"commit": COMMIT, "archive": self.archive.name,
                                             "archive_sha256": digest, "file_count": 1,
                                             "inventory": {}}), encoding="utf-8")

    def _verify(self):
        with patch.object(release_verify, "_release"), \
             patch.object(release_verify.release_gate, "_tree",
                          return_value=[("README.md", "100644", "blob-id")]), \
             patch.object(release_verify.release_gate, "_git", return_value=b"release source"):
            release_verify.verify(COMMIT, "v0.3.0", self.windows, self.linux,
                                  self.docker, self.manifest, self.archive, self.checksum)

    def _replace_archive(self, payload):
        stem = f"ermik-source-{COMMIT[:12]}"
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            item = tarfile.TarInfo(stem + "/README.md")
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
        with self.archive.open("wb") as target:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as writer:
                writer.write(raw.getvalue())
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["archive_sha256"] = digest
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.checksum.write_text(f"{digest}  {self.archive.name}\n", encoding="ascii")

    def test_valid_bundle(self):
        self._verify()

    def test_rejects_missing_oversized_and_malformed_report(self):
        for content in (None, b"x" * (release_verify._MAX_REPORT + 1), b"not json"):
            with self.subTest(content=content is None and "missing" or len(content)):
                if content is None:
                    self.windows.unlink(missing_ok=True)
                else:
                    self.windows.write_bytes(content)
                with self.assertRaisesRegex(release_verify.BundleError,
                                            "host_report_invalid"):
                    self._verify()

    def test_rejects_bad_host_counts_and_docker_evidence(self):
        cases = [
            (self.windows, lambda r: r.update(commit="c" * 40)),
            (self.windows, lambda r: r.update(dirty=True)),
            (self.windows, lambda r: r.update(platform="linux")),
            (self.windows, lambda r: r["unit"].update(executed=0)),
            (self.windows, lambda r: r["unit"].update(failures=1)),
            (self.windows, lambda r: r["unit"].update(unexpected_skips=[{"case": "x"}])),
            (self.docker, lambda r: r["host"].pop("smoke")),
            (self.docker, lambda r: r["host"]["container_recovery"].update(result="failed")),
            (self.docker, lambda r: r["docker"].pop("image_id")),
        ]
        for path, change in cases:
            original = path.read_bytes()
            with self.subTest(change=change):
                report = json.loads(original)
                change(report)
                path.write_text(json.dumps(report), encoding="utf-8")
                with self.assertRaises(release_verify.BundleError):
                    self._verify()
                path.write_bytes(original)

    def test_rejects_archive_hash_and_manifest_count(self):
        original = self.archive.read_bytes()
        with self.archive.open("ab") as target:
            target.write(b"tamper")
        with self.assertRaisesRegex(release_verify.BundleError, "archive_hash_mismatch"):
            self._verify()
        self.archive.write_bytes(original)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["file_count"] = 2
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(release_verify.BundleError, "manifest_count_mismatch"):
            self._verify()

    def test_rejects_archive_content_replaced_with_matching_manifest_and_checksum(self):
        self._replace_archive(b"altered source")
        with self.assertRaisesRegex(release_verify.BundleError, "archive_tree_mismatch"):
            self._verify()

    def test_rejects_excess_unpacked_archive_size(self):
        with patch.object(release_verify, "_MAX_UNPACKED", 10):
            with self.assertRaisesRegex(release_verify.BundleError, "archive_size_limit"):
                self._verify()
        with patch.object(release_verify, "_MAX_TAR_BYTES", 1024):
            with self.assertRaisesRegex(release_verify.BundleError, "archive_size_limit"):
                self._verify()

    def test_release_identity_requires_exact_head_and_ancestor_tag(self):
        with patch.object(release_verify, "_git", side_effect=[COMMIT, "", "b" * 40]), \
             patch.object(release_verify.subprocess, "run") as run:
            run.return_value.returncode = 0
            release_verify._release(COMMIT, "v0.3.0")
        with patch.object(release_verify, "_git", return_value="b" * 40):
            with self.assertRaisesRegex(release_verify.BundleError,
                                        "release_commit_mismatch"):
                release_verify._release(COMMIT, "v0.3.0")

    def test_cli_failure_output_is_bounded_and_contains_no_input(self):
        output = io.StringIO()
        with patch.object(release_verify, "verify", side_effect=release_verify.BundleError(
                "host_report_invalid")), redirect_stdout(output):
            result = release_verify.main([
                "--commit", COMMIT, "--previous-tag", "v0.3.0",
                "--windows", "private-token-path", "--linux", "linux.json",
                "--docker", "docker.json", "--manifest", "manifest.json",
                "--archive", "archive.tar.gz", "--checksum", "checksum.sha256",
            ])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"result": "failed", "reason": "host_report_invalid"})
        self.assertNotIn("private-token-path", output.getvalue())


if __name__ == "__main__":
    unittest.main()
