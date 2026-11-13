"""Focused stopped-installation snapshot checks."""

import hashlib
from contextlib import closing
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "installation_snapshot.py"
SPEC = importlib.util.spec_from_file_location("installation_snapshot", SCRIPT)
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


class InstallationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.workspace = self.root / "workspace"
        self.backup = self.root / "backup"
        self.state.mkdir()
        self.workspace.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"repo": "example/project",
                                           "state_dir": str(self.state),
                                           "workspace": str(self.workspace)}))
        marker = hashlib.sha256(f"example/project\n{self.state}".encode()).hexdigest()[:32]
        (self.state / "container-namespace").write_text(marker + "\n")
        with closing(sqlite3.connect(self.state / "state.sqlite3")) as db, db:
            db.execute("PRAGMA user_version = 6")
            db.execute("CREATE TABLE jobs (issue INTEGER PRIMARY KEY, pr INTEGER)")
            db.execute("INSERT INTO jobs VALUES (7, 8)")
        (self.state / "review.json").write_text("review-head")
        (self.workspace / "checkout.txt").write_text("pending-work")

    def test_schema_six_restores_state_and_workspace(self):
        snapshot.backup(self.config, self.backup)
        with closing(sqlite3.connect(self.state / "state.sqlite3")) as db, db:
            db.execute("DELETE FROM jobs")
        (self.state / "state.sqlite3-journal").write_text("stale journal")
        (self.state / "review.json").write_text("changed")
        (self.workspace / "checkout.txt").write_text("changed")
        snapshot.restore(self.config, self.backup)
        with closing(sqlite3.connect(self.state / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT * FROM jobs").fetchall(), [(7, 8)])
        self.assertEqual((self.state / "review.json").read_text(), "review-head")
        self.assertFalse((self.state / "state.sqlite3-journal").exists())
        self.assertEqual((self.workspace / "checkout.txt").read_text(), "pending-work")
        preserved = self.workspace.with_name("workspace.post-upgrade-backup")
        self.assertEqual((preserved / "checkout.txt").read_text(), "changed")

    def test_incomplete_snapshot_preserves_original(self):
        snapshot.backup(self.config, self.backup)
        (self.backup / "workspace" / "checkout.txt").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete or changed"):
            snapshot.restore(self.config, self.backup)
        self.assertEqual((self.workspace / "checkout.txt").read_text(), "pending-work")
        self.assertFalse(self.state.with_name("state.post-upgrade-backup").exists())

    def test_newer_schema_preserves_original(self):
        snapshot.backup(self.config, self.backup)
        with closing(sqlite3.connect(self.backup / "state.sqlite3")) as db, db:
            db.execute("PRAGMA user_version = 7")
        manifest_path = self.backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["database"] = snapshot._digest(self.backup)["state.sqlite3"]
        manifest["schema"] = 7
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "unsupported schema"):
            snapshot.restore(self.config, self.backup)
        self.assertEqual((self.state / "review.json").read_text(), "review-head")
        self.assertFalse(self.state.with_name("state.post-upgrade-backup").exists())


if __name__ == "__main__":
    unittest.main()
