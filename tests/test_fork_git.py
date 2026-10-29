"""Linux fork transport keeps its token out of Git arguments and helpers."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik import runtime


@unittest.skipIf(os.name == "nt", "fork publisher runs on the POSIX server")
class ForkGitTests(unittest.TestCase):
    def test_fork_git_uses_only_scoped_askpass(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            cfg = Config("owner/upstream", "owner", root / "state", root / "work",
                         "demo:latest", ("/worker",), (("true",),), 1, 60,
                         (), "main", (), 0, "publisher/upstream", "FORK_TEST_TOKEN")
            cfg.workspace.mkdir()
            captured = {}
            real_run = subprocess.run

            def fake_run(args, **kwargs):
                captured["args"] = args
                captured["env"] = kwargs["env"]
                return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

            with patch.dict(os.environ, {"FORK_TEST_TOKEN": "dummy-fork-secret"}), patch.object(
                    runtime.subprocess, "run", side_effect=fake_run):
                output = runtime._publish_git(
                    cfg, cfg.workspace, "push", "--force-with-lease=refs/heads/work:abc",
                    "HEAD:refs/heads/work")

            self.assertEqual(output, "ok")
            self.assertNotIn("dummy-fork-secret", " ".join(captured["args"]))
            self.assertIn("https://github.com/publisher/upstream.git", captured["args"])
            self.assertIn("credential.helper=", captured["args"])
            helper = cfg.state_dir / "fork-askpass"
            self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("dummy-fork-secret", helper.read_text(encoding="utf-8"))
            password = real_run([str(helper), "Password for GitHub"],
                                env=captured["env"], text=True, capture_output=True,
                                check=True)
            self.assertEqual(password.stdout.strip(), "dummy-fork-secret")

    def test_nonzero_push_is_ambiguous_until_remote_reconciliation(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            cfg = Config("owner/upstream", "owner", root / "state", root / "work",
                         "demo:latest", ("/worker",), (("true",),), 1, 60,
                         (), "main", (), 0, "publisher/upstream", "FORK_TEST_TOKEN")
            cfg.workspace.mkdir()
            response = subprocess.CompletedProcess([], 1, stdout="", stderr="connection lost")
            with patch.dict(os.environ, {"FORK_TEST_TOKEN": "dummy-fork-secret"}), patch.object(
                    runtime.subprocess, "run", return_value=response):
                with self.assertRaises(runtime.TransientFailure):
                    runtime._publish_git(cfg, cfg.workspace, "push", "HEAD:refs/heads/work")


if __name__ == "__main__":
    unittest.main()
