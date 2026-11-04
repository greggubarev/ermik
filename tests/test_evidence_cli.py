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

    def test_unsupported_manifest_version_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.json"
            path.write_bytes(b'{"version":3,"future_required":"value"}\n')
            error = StringIO()
            with (patch.object(cli, "load_config", return_value=object()),
                  redirect_stderr(error)):
                code = cli.main(["--config", "unused", "verify-evidence", "19",
                                 "--input", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("unsupported manifest version", error.getvalue())

    def test_fetch_writes_only_current_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fetched.json"
            with (patch.object(cli, "load_config", return_value=object()),
                  patch.object(cli, "fetch_manifest", return_value=(
                      {"status": "missing"}, None)), redirect_stdout(StringIO())):
                self.assertEqual(cli.main(["--config", "unused", "fetch-evidence", "19",
                                           "--output", str(path)]), 2)
            self.assertFalse(path.exists())
            with (patch.object(cli, "load_config", return_value=object()),
                  patch.object(cli, "fetch_manifest", return_value=(
                      {"status": "current", "sha256": "a" * 64}, {"sha256": "a" * 64})),
                  patch.object(cli, "dumps_manifest", return_value=b"record\n"),
                  redirect_stdout(StringIO())):
                self.assertEqual(cli.main(["--config", "unused", "fetch-evidence", "19",
                                           "--output", str(path)]), 0)
            self.assertEqual(path.read_bytes(), b"record\n")

    def test_fetch_reports_unsupported_version_without_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fetched.json"
            output = StringIO()
            with (patch.object(cli, "load_config", return_value=object()),
                  patch.object(cli, "fetch_manifest", return_value=(
                      {"status": "unsupported_version"}, None)),
                  redirect_stdout(output)):
                code = cli.main(["--config", "unused", "fetch-evidence", "19",
                                 "--output", str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"],
                             "unsupported_version")
            self.assertFalse(path.exists())

    def test_run_once_delivers_when_publisher_is_configured(self):
        cfg = type("ConfigStub", (), {"evidence_publisher": "keeper"})()
        output = StringIO()
        with (patch.object(cli, "load_config", return_value=cfg),
              patch.object(cli, "run_once", return_value=[]),
              patch.object(cli, "deliver_pending", return_value=[{"status": "delivered"}]),
              redirect_stdout(output)):
            self.assertEqual(cli.main(["--config", "unused", "run-once"]), 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"work": [], "delivery": [{"status": "delivered"}]})

    def test_run_once_retries_delivery_when_work_fails(self):
        cfg = type("ConfigStub", (), {"evidence_publisher": "keeper"})()
        with (patch.object(cli, "load_config", return_value=cfg),
              patch.object(cli, "run_once", side_effect=ValueError("work failed")),
              patch.object(cli, "deliver_pending", return_value=[]) as deliver,
              redirect_stderr(StringIO())):
            self.assertEqual(cli.main(["--config", "unused", "run-once"]), 1)
        deliver.assert_called_once_with(cfg)

    def test_review_packet_noncurrent_exits_nonzero_without_writing(self):
        output = StringIO()
        with (patch.object(cli, "load_config", return_value=object()),
              patch.object(cli, "build_review_packet", return_value={
                  "status": "stale", "reason": "remote_identity"}),
              redirect_stdout(output)):
            self.assertEqual(cli.main(["--config", "unused", "review-packet", "19"]), 2)
        self.assertEqual(json.loads(output.getvalue()),
                         {"status": "stale", "reason": "remote_identity"})


if __name__ == "__main__":
    unittest.main()
