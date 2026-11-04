"""Decision history is a read-only, non-approval summary."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ermik import __main__ as cli
from ermik.config import Config


class DecisionCliTests(unittest.TestCase):
    def test_missing_history_does_not_create_state_or_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config("owner/example", "keeper", root / "state", root / "work",
                         "image:stable", ("run",), (("check",),), 1, 60, (), "main")
            output = StringIO()
            with patch.object(cli, "load_config", return_value=cfg), redirect_stdout(output):
                self.assertEqual(cli.main(["--config", "unused", "decision-history", "7"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "historical_only")
            self.assertEqual(result["approval"], "none")
            self.assertEqual(result["entries"], [])
            self.assertFalse(cfg.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
