"""Source release candidate gate tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ermik.release_gate import (ReleaseError, _allowed_path, _git,
                                _sensitive_category, build_release, main)


class ReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("README.md", "Example source\n")
        self.write("LICENSE", "Example license\n")
        self.write("examples/Dockerfile.demo", "FROM python:3.11-alpine\nRUN apk add --no-cache ca-certificates\n")
        self.commit()

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.root, check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", "test")
        return self.git("rev-parse", "HEAD")

    def test_builds_repeatable_archive_and_inventory(self) -> None:
        head = self.git("rev-parse", "HEAD")
        first = build_release(head, Path(self.temp.name) / "first", self.root)
        second = build_release(head, Path(self.temp.name) / "second", self.root)
        self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
        manifest = json.loads(first[2].read_text(encoding="utf-8"))
        self.assertEqual(manifest["commit"], head)
        self.assertEqual(len(manifest["inventory"]["referenced_images"]), 1)
        self.assertEqual(manifest["inventory"]["bundled_images"], [])
        self.assertEqual(manifest["inventory"]["referenced_dependencies"][0]["packages"], ["ca-certificates"])
        self.assertIn(manifest["archive_sha256"], first[1].read_text(encoding="utf-8"))
        with tarfile.open(first[0], "r:gz") as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith("/README.md") for name in names))

    def test_includes_exact_support_files(self) -> None:
        files = {
            ".gitattributes": "*.py text eol=lf\n",
            ".github/workflows/release-host-verify.yml": "on: workflow_dispatch\n",
            "scripts/bootstrap-ubuntu.sh": "#!/bin/sh\n",
            "tests/fixtures/evidence/h1.v1.json": "{}\n",
            "tests/fixtures/evidence/h2.v1.json": "{}\n",
        }
        for name, content in files.items():
            self.write(name, content)
        head = self.commit()
        archive, _, _ = build_release(head, Path(self.temp.name) / "out", self.root)
        with tarfile.open(archive, "r:gz") as contents:
            for name, content in files.items():
                member = next(item for item in contents if item.name.endswith("/" + name))
                self.assertEqual(contents.extractfile(member).read(), content.encode())

    def test_rejects_unlisted_support_paths(self) -> None:
        for name in (
            ".github/workflows/other.yml",
            "scripts/other.sh",
            "tests/fixtures/evidence/h3.v1.json",
            ".git/config",
        ):
            with self.subTest(name=name):
                self.assertFalse(_allowed_path(name))

    def test_accepts_documented_service_home_paths(self) -> None:
        self.write("docs/paths.md", "\n".join(
            f"/home/{account}/ermik" for account in ("ermikctl", "ACCOUNT", "%i")) + "\n")
        head = self.commit()
        build_release(head, Path(self.temp.name) / "out", self.root)

    def test_rejects_other_private_home_paths(self) -> None:
        for path in ("/home/" + "alice/private/", "/root/" + "private/",
                     "C:/Users/" + "alice/private/"):
            with self.subTest(path=path):
                self.assertEqual(_sensitive_category(path), "private home path")

    def test_rejects_dirty_checkout_and_wrong_head(self) -> None:
        head = self.git("rev-parse", "HEAD")
        self.write("README.md", "modified\n")
        with self.assertRaisesRegex(ReleaseError, "not clean"):
            build_release(head, Path(self.temp.name) / "out", self.root)
        self.git("checkout", "--", "README.md")
        with self.assertRaisesRegex(ReleaseError, "differs from HEAD"):
            build_release("0" * 40, Path(self.temp.name) / "out", self.root)

    def test_rejects_unexpected_path(self) -> None:
        self.write("state/session.sqlite3", "contents\n")
        head = self.commit()
        with self.assertRaisesRegex(ReleaseError, "unexpected release path"):
            build_release(head, Path(self.temp.name) / "out", self.root)

    def test_rejects_deleted_historical_secret_without_echo(self) -> None:
        secret = "ghp_" + "A" * 32
        self.write("docs/old.md", "credential: " + secret + "\n")
        self.commit()
        (self.root / "docs/old.md").unlink()
        head = self.commit()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with contextlib.chdir(self.root):
                result = main(["--commit", head, "--output", str(Path(self.temp.name) / "out")])
        self.assertEqual(result, 1)
        self.assertIn("GitHub credential", captured.getvalue())
        self.assertNotIn(secret, captured.getvalue())
        self.assertFalse((Path(self.temp.name) / "out").exists())

    def test_rejects_historical_provider_keys_without_echo(self) -> None:
        keys = (
            ("oc_" + "sk_" + "B" * 32, "OpenCode credential"),
            ("a" * 32 + "." + "C" * 32, "provider credential"),
        )
        for index, (secret, category) in enumerate(keys):
            name = f"docs/old-{index}.md"
            self.write(name, "credential: " + secret + "\n")
            self.commit()
            (self.root / name).unlink()
            head = self.commit()
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                with contextlib.chdir(self.root):
                    result = main(["--commit", head, "--output", str(Path(self.temp.name) / f"out-{index}")])
            self.assertEqual(result, 1)
            self.assertIn(category, captured.getvalue())
            self.assertNotIn(secret, captured.getvalue())

    def test_rejects_historical_bearer_value_without_echo(self) -> None:
        secret = "Bearer " + "D" * 32
        self.write("docs/old.md", secret + "\n")
        self.commit()
        (self.root / "docs/old.md").unlink()
        head = self.commit()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with contextlib.chdir(self.root):
                result = main(["--commit", head, "--output", str(Path(self.temp.name) / "out")])
        self.assertEqual(result, 1)
        self.assertIn("Bearer credential", captured.getvalue())
        self.assertNotIn(secret, captured.getvalue())

    def test_rejects_unquoted_environment_secret_without_echo(self) -> None:
        secret = "GITHUB_TOKEN" + "=" + "abcdefghijklmnopqrstuvwx"
        self.write("docs/old.md", secret + "\n")
        self.commit()
        (self.root / "docs/old.md").unlink()
        head = self.commit()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with contextlib.chdir(self.root):
                result = main(["--commit", head, "--output", str(Path(self.temp.name) / "out")])
        self.assertEqual(result, 1)
        self.assertIn("credential environment value", captured.getvalue())
        self.assertNotIn(secret, captured.getvalue())

    def test_rejects_short_quoted_password_without_echo(self) -> None:
        secret = "password" + " = " + '"' + "hunter2" + '"'
        self.write("docs/old.md", secret + "\n")
        head = self.commit()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with contextlib.chdir(self.root):
                result = main(["--commit", head, "--output", str(Path(self.temp.name) / "out")])
        self.assertEqual(result, 1)
        self.assertIn("password assignment", captured.getvalue())
        self.assertNotIn(secret, captured.getvalue())

    def test_rejects_symlink(self) -> None:
        target = self.root / "docs" / "link.md"
        target.parent.mkdir(exist_ok=True)
        try:
            target.symlink_to("../README.md")
        except OSError:
            self.skipTest("symlink creation unavailable")
        head = self.commit()
        with self.assertRaisesRegex(ReleaseError, "unsupported file type"):
            build_release(head, Path(self.temp.name) / "out", self.root)

    def test_git_replace_ref_cannot_change_candidate_blob(self) -> None:
        original = self.git("rev-parse", "HEAD:README.md")
        alternate = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.root,
                                   input=b"replacement source\n", stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=True).stdout.decode().strip()
        self.git("replace", original, alternate)
        self.assertEqual(_git("cat-file", "blob", original, cwd=self.root), b"Example source\n")
        head = self.git("rev-parse", "HEAD")
        archive, _, _ = build_release(head, Path(self.temp.name) / "out", self.root)
        with tarfile.open(archive, "r:gz") as contents:
            readme = next(item for item in contents if item.name.endswith("/README.md"))
            self.assertEqual(contents.extractfile(readme).read(), b"Example source\n")

    def test_ignores_ambient_repository_selector(self) -> None:
        head = self.git("rev-parse", "HEAD")
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "not-a-repository")}, clear=False):
            self.assertEqual(_git("rev-parse", "HEAD", cwd=self.root).decode().strip(), head)

    def test_rejects_private_root_path_without_echo(self) -> None:
        path = "/root/" + "private/project/"
        self.write("docs/old.md", path + "\n")
        head = self.commit()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with contextlib.chdir(self.root):
                result = main(["--commit", head, "--output", str(Path(self.temp.name) / "out")])
        self.assertEqual(result, 1)
        self.assertIn("private home path", captured.getvalue())
        self.assertNotIn(path, captured.getvalue())


if __name__ == "__main__":
    unittest.main()
