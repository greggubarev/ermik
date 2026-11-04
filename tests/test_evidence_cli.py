"""Evidence commands write bounded files and report stale verification."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ermik import __main__ as cli


class EvidenceCliTests(unittest.TestCase):
    def test_export_creates_private_file_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            record = {"sha256": "a" * 64}
            with (patch.object(cli, "load_config", return_value=object()),
                  patch.object(cli, "export_manifest", return_value=record),
                  patch.object(cli, "dumps_manifest", return_value=b"record\n"),
                  redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                self.assertEqual(cli.main(["--config", "unused", "export-evidence", "19",
                                           "--output", str(path)]), 0)
                self.assertEqual(path.read_bytes(), b"record\n")
                self.assertEqual(cli.main(["--config", "unused", "export-evidence", "19",
                                           "--output", str(path)]), 1)

    def test_stale_verification_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(b"record\n")
            output = StringIO()
            with (patch.object(cli, "load_config", return_value=object()),
                  patch.object(cli, "loads_manifest", return_value={}),
                  patch.object(cli, "verify_manifest",
                               return_value={"status": "stale", "pr": 19}),
                  redirect_stdout(output)):
                code = cli.main(["--config", "unused", "verify-evidence", "19",
                                 "--input", str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "stale")

    def test_oversized_input_rejected_before_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(b"x" * (cli.MAX_BYTES + 2))
            with patch.object(cli, "load_config", return_value=object()), patch.object(
                    cli, "loads_manifest") as parse, redirect_stderr(StringIO()):
                code = cli.main(["--config", "unused", "verify-evidence", "19",
                                 "--input", str(path)])
            self.assertEqual(code, 1)
            parse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
