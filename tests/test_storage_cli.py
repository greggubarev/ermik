"""Storage command safety defaults and retention configuration boundaries."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik.__main__ import main
from ermik.config import load_config


class StorageCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.json"
        self.config = {
            "repo": "example/project", "maintainer": "owner",
            "state_dir": str(self.root / "state"),
            "workspace": str(self.root / "workspace"),
            "image": "demo:test", "worker_command": ["true"],
            "checks": [["true"]], "max_parallel": 1, "poll_seconds": 60,
        }

    def write(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def test_retention_defaults_and_explicit_limits(self):
        self.write()
        cfg = load_config(self.path)
        self.assertEqual((cfg.retention_days, cfg.artifact_warn_mb), (30, 1024))
        self.config.update(retention_days=90, artifact_warn_mb=2048)
        self.write()
        cfg = load_config(self.path)
        self.assertEqual((cfg.retention_days, cfg.artifact_warn_mb), (90, 2048))

    def test_invalid_retention_limits_fail_before_any_cleanup(self):
        for key, values in (("retention_days", (0, -1, 3651, True, "30")),
                            ("artifact_warn_mb", (-1, 1000001, True, "1024"))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.config[key] = value
                    self.write()
                    with self.assertRaises(ValueError):
                        load_config(self.path)
            self.config.pop(key)

    def test_cleanup_is_dry_run_without_apply_and_apply_needs_cleanup(self):
        self.write()
        with patch("ermik.__main__.cleanup_artifacts", return_value={"removed": []}) as clean:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(self.path), "storage", "--cleanup"]), 0)
                self.assertEqual(main(["--config", str(self.path), "storage", "--apply"]), 1)
                self.assertEqual(main(["--config", str(self.path), "storage", "--cleanup",
                                       "--apply", "--max-items", "2", "--max-bytes", "4096"]), 0)
        self.assertEqual(clean.call_count, 2)
        self.assertEqual(clean.call_args_list[0].kwargs["dry_run"], True)
        self.assertEqual(clean.call_args_list[1].kwargs,
                         {"dry_run": False, "max_items": 2, "max_bytes": 4096})


if __name__ == "__main__":
    unittest.main()
