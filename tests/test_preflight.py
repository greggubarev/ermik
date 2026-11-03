"""The unattended service must fail preflight when a key is absent."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik import __main__ as cli
from ermik.config import Config
from ermik.github import GitHubError
from ermik.providers import ModelCandidate


class PreflightTests(unittest.TestCase):
    def fork_config(self, root):
        return Config("example/project", "owner", root / "state", root / "work",
                      "demo:latest", ("true",), (("true",),), 1, 60, (), "main",
                      publish_repo="fork/project", publish_token_env="PUBLISH_TOKEN")

    def test_missing_selected_provider_key_stops_before_github(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config("example/project", "owner", root / "state", root / "work",
                         "demo:latest", ("true",), (("true",),), 1, 60, (), "main",
                         (ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY"),), 0)
            with patch.dict("os.environ", {}, clear=True), patch.object(cli, "GitHub") as gh:
                with self.assertRaisesRegex(ValueError, "KIMI_CODE_API_KEY"):
                    cli.doctor(cfg)
            gh.assert_not_called()

    def test_missing_generic_worker_env_also_stops_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config("example/project", "owner", root / "state", root / "work",
                         "demo:latest", ("true",), (("true",),), 1, 60,
                         ("SERVICE_TOKEN",), "main", (), 0)
            with patch.dict("os.environ", {}, clear=True), patch.object(cli, "GitHub") as gh:
                with self.assertRaisesRegex(ValueError, "SERVICE_TOKEN"):
                    cli.doctor(cfg)
            gh.assert_not_called()

    def test_fork_doctor_inspects_identity_without_write_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.fork_config(Path(directory))
            with (patch.dict("os.environ", {"PUBLISH_TOKEN": "fork-token"}, clear=True),
                  patch.object(cli, "GitHub") as github_class,
                  patch.object(cli, "checked"),
                  patch.object(cli, "main_sha", return_value="a" * 40)):
                github = github_class.return_value
                github.command.return_value = json.dumps({
                    "nameWithOwner": cfg.repo,
                    "defaultBranchRef": {"name": cfg.base_branch}})
                github.identity.return_value = "owner"
                github.api.return_value = {"permission": "read"}
                result = cli.doctor(cfg)

            github_class.assert_called_once_with(cfg.repo, cfg.publish_repo,
                                                 cfg.publish_token_env)
            github.inspect_publish_identity.assert_called_once_with()
            github.validate_publish_access.assert_not_called()
            github._probe_branch_write.assert_not_called()
            github._probe_existing_branch_update.assert_not_called()
            self.assertEqual(result["publish_write_capabilities"], "unverified")

    def test_fork_doctor_fails_closed_when_identity_inspection_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.fork_config(Path(directory))
            with (patch.dict("os.environ", {"PUBLISH_TOKEN": "fork-token"}, clear=True),
                  patch.object(cli, "GitHub") as github_class,
                  patch.object(cli, "checked") as checked):
                github = github_class.return_value
                github.inspect_publish_identity.side_effect = GitHubError("fork identity is unsafe")
                with self.assertRaisesRegex(GitHubError, "fork identity is unsafe"):
                    cli.doctor(cfg)

            github.validate_publish_access.assert_not_called()
            github._probe_branch_write.assert_not_called()
            github._probe_existing_branch_update.assert_not_called()
            checked.assert_not_called()

    def test_direct_doctor_also_reports_write_capability_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config("example/project", "owner", root / "state", root / "work",
                         "demo:latest", ("true",), (("true",),), 1, 60, (), "main")
            with (patch.object(cli, "GitHub") as github_class,
                  patch.object(cli, "checked"),
                  patch.object(cli, "main_sha", return_value="a" * 40)):
                github = github_class.return_value
                github.command.return_value = json.dumps({
                    "nameWithOwner": cfg.repo,
                    "defaultBranchRef": {"name": cfg.base_branch}})
                github.identity.return_value = "owner"
                github.api.return_value = {"permission": "write"}
                result = cli.doctor(cfg)

            github.inspect_publish_identity.assert_not_called()
            github.validate_publish_access.assert_not_called()
            self.assertEqual(result["publish_write_capabilities"], "unverified")


if __name__ == "__main__":
    unittest.main()
