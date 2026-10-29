"""The unattended service must fail preflight when a key is absent."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik import __main__ as cli
from ermik.config import Config
from ermik.providers import ModelCandidate


class PreflightTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
