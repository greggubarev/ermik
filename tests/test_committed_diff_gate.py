"""Committed whitespace must fail before a container check can run."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from ermik.config import Config
from ermik.runtime import WorkflowError, local_checks


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


class CommittedDiffGateTests(unittest.TestCase):
    def test_committed_trailing_whitespace_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout = root / "checkout"
            checkout.mkdir()
            git(checkout, "init")
            git(checkout, "config", "user.name", "Review")
            git(checkout, "config", "user.email", "review@example.invalid")
            git(checkout, "config", "core.autocrlf", "false")
            (checkout / "note.txt").write_text("clean\n", encoding="utf-8")
            git(checkout, "add", "note.txt")
            git(checkout, "commit", "-m", "base")
            base = git(checkout, "rev-parse", "HEAD")
            git(checkout, "update-ref", "refs/remotes/origin/main", base)
            (checkout / "note.txt").write_text("bad trailing space \n", encoding="utf-8")
            git(checkout, "add", "note.txt")
            git(checkout, "commit", "-m", "bad change")
            cfg = Config("owner/repo", "owner", root / "state", root / "workspace",
                         "unused:image", ("true",), (("true",),), 1, 90,
                         (), "main")
            with self.assertRaisesRegex(WorkflowError, "git command failed"):
                local_checks(cfg, checkout)
            self.assertFalse((root / "state" / "ci-logs").exists())


if __name__ == "__main__":
    unittest.main()
