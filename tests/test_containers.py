"""Controller-owned Docker containers are reclaimed after interruption."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ermik import runtime
from ermik.config import Config


class ContainerCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config("example/project", "owner", root / "state", root / "work",
                          "demo:latest", ("/worker",), (("true",),), 1, 60,
                          (), "main", (), 0)
        (self.cfg.workspace / "source" / ".git").mkdir(parents=True)
        self.namespace = runtime._container_namespace(self.cfg)

    def tearDown(self):
        runtime._CLEANED_NAMESPACES.discard(self.namespace)
        self.temp.cleanup()

    def create_checkout(self):
        source = self.cfg.workspace / "source"
        (source / ".git").rmdir()

        def run(*args):
            return subprocess.run(["git", *map(str, args)], cwd=source,
                                  text=True, capture_output=True, check=True).stdout.strip()

        run("init", "-b", "main")
        run("config", "user.name", "Test")
        run("config", "user.email", "test@example.invalid")
        (source / "base.txt").write_text("base\n")
        run("add", "base.txt")
        run("commit", "-m", "base")
        run("branch", "secret")
        run("switch", "secret")
        (source / "secret.txt").write_text("hidden\n")
        run("add", "secret.txt")
        run("commit", "-m", "secret")
        run("switch", "main")
        checkout = self.cfg.workspace / "job"
        run("worktree", "add", "-b", "feature", checkout)
        (checkout / "feature.txt").write_text("feature\n")
        run("-C", checkout, "add", "feature.txt")
        run("-C", checkout, "commit", "-m", "feature")
        (checkout / "pending.txt").write_text("pending\n")
        return checkout

    def test_first_launch_marks_namespace_without_removing_other_containers(self):
        with patch.object(runtime, "checked") as checked:
            runtime._cleanup_containers(self.cfg, mark=True)
        checked.assert_not_called()
        self.assertEqual((self.cfg.state_dir / "container-namespace").read_text().strip(),
                         self.namespace)

    def test_restart_removes_only_verified_labelled_containers(self):
        runtime._cleanup_containers(self.cfg, mark=True)
        runtime._CLEANED_NAMESPACES.discard(self.namespace)
        container_id = "a" * 12
        calls = []

        def checked(args, **_kwargs):
            calls.append(args)
            return container_id if args[1] == "ps" else ""

        with patch.object(runtime, "checked", side_effect=checked):
            runtime._cleanup_containers(self.cfg)
        self.assertEqual(calls, [
            ["docker", "ps", "--all", "--quiet", "--filter",
             f"label=ermik.namespace={self.namespace}"],
            ["docker", "rm", "--force", container_id],
        ])

    def test_invalid_listing_blocks_cleanup_and_new_execution(self):
        runtime._cleanup_containers(self.cfg, mark=True)
        runtime._CLEANED_NAMESPACES.discard(self.namespace)
        with patch.object(runtime, "checked", return_value="not-a-container"):
            with self.assertRaises(runtime.WorkflowError):
                runtime._cleanup_containers(self.cfg)
        self.assertNotIn(self.namespace, runtime._CLEANED_NAMESPACES)

    def test_timeout_force_removes_named_container(self):
        checkout = self.create_checkout()
        commands = []

        def failed_run(args, _timeout):
            commands.append(args)
            raise runtime.TransientFailure("timeout")

        with patch.object(runtime, "_run_capped", side_effect=failed_run), patch.object(
                runtime, "_force_remove_container") as remove:
            with self.assertRaises(runtime.TransientFailure):
                runtime._docker(self.cfg, checkout, ("true",),
                                issue_file=None, network=False, timeout=1)
        command = commands[0]
        name = command[command.index("--name") + 1]
        self.assertEqual(command[command.index("--label") + 1],
                         f"ermik.namespace={self.namespace}")
        self.assertFalse(any(str(self.cfg.workspace / "source" / ".git") in arg
                             for arg in command))
        self.assertIn("target=/ci-git,readonly", " ".join(command))
        self.assertIn("GIT_OPTIONAL_LOCKS=0", command)
        remove.assert_called_once_with(self.cfg, name)
        self.assertEqual(list(self.cfg.state_dir.glob("check-git-*")), [])

    def test_check_receives_only_base_and_checkout_git_history(self):
        checkout = self.create_checkout()
        head = runtime.git(checkout, "rev-parse", "HEAD")
        secret = runtime.git(self.cfg.workspace / "source", "rev-parse", "secret")
        inspected = []

        def inspect(args, _timeout):
            mounts = [args[index + 1] for index, item in enumerate(args[:-1])
                      if item == "--mount"]
            git_mount = next(item for item in mounts if "target=/ci-git" in item)
            file_mount = next(item for item in mounts if "target=/work/.git" in item)
            snapshot_git = Path(git_mount.split(",", 2)[1].removeprefix("source="))
            git_file = Path(file_mount.split(",", 2)[1].removeprefix("source="))
            self.assertEqual(git_file.read_text(), "gitdir: /ci-git\n")
            self.assertEqual(runtime.git(snapshot_git.parent, "rev-parse", "HEAD"), head)
            refs = runtime.git(snapshot_git.parent, "for-each-ref", "--format=%(refname)")
            self.assertIn("refs/heads/ci-head", refs)
            self.assertIn("refs/remotes/origin/main", refs)
            self.assertNotIn("secret", refs)
            self.assertNotIn("feature", refs)
            hidden = subprocess.run(["git", "-C", str(snapshot_git.parent),
                                     "cat-file", "-e", f"{secret}^{{commit}}"],
                                    capture_output=True, check=False)
            self.assertNotEqual(hidden.returncode, 0)
            environment = os.environ.copy()
            environment["GIT_DIR"] = str(snapshot_git)
            environment["GIT_WORK_TREE"] = str(checkout)
            status = subprocess.run(["git", "status", "--short"], env=environment,
                                    text=True, capture_output=True, check=True).stdout
            self.assertIn("?? pending.txt", status)
            self.assertNotIn("feature.txt", status)
            self.assertFalse(any(str(self.cfg.workspace / "source" / ".git") in item
                                 for item in mounts))
            inspected.append(snapshot_git)
            return 0, b"ok\n"

        with patch.object(runtime, "_run_capped", side_effect=inspect):
            runtime._docker(self.cfg, checkout, ("true",), issue_file=None,
                            network=False, timeout=1)
        self.assertEqual(len(inspected), 1)
        self.assertFalse(inspected[0].exists())


if __name__ == "__main__":
    unittest.main()
