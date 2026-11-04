"""Exact-HEAD export uses local checks; verification needs only live GitHub."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.evidence_export import export_manifest, verify_manifest
from ermik.evidence import make_manifest
from ermik.evidence_store import read_evidence, write_evidence
from ermik.policy import parse_issue
from ermik.runtime import _admission_marker, WorkflowError
from ermik import state


BASE = "a" * 40
HEAD = "b" * 40
IMAGE = "sha256:" + "c" * 64
BODY = "## Goal\nDo one thing\n\n## Allowed paths\nsrc\n\n## Acceptance\nCheck it\n"


class FakeGitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.head = HEAD
        self.base = BASE
        self.issue_body = BODY
        self.paths = ["src/one.py"]

    def pr(self, number):
        return {"number": number, "state": "OPEN", "isDraft": True,
                "baseRefName": "main", "baseRefOid": self.base,
                "headRefName": "work/issue-7-delivery", "headRefOid": self.head,
                "headRepository": {"nameWithOwner": self.cfg.repo},
                "body": f"Closes #7\nExecution image: `{IMAGE}`",
                "files": [{"path": path} for path in self.paths],
                "changedFiles": len(self.paths)}

    def validate_pr_identity(self, pr, *, branch, base_branch, number, state):
        if (pr["headRefName"] != branch or pr["baseRefName"] != base_branch
                or pr["number"] != number or pr["state"] != state
                or pr["headRepository"]["nameWithOwner"] != self.cfg.repo):
            raise WorkflowError("invalid PR")

    def issue(self, number):
        return {"body": self.issue_body, "labels": [{"name": "queue:worker"},
                {"name": "state:in-progress"}]}

    def comments(self, number):
        return [{"body": _admission_marker(7, parse_issue(self.issue_body)),
                 "user": {"login": self.cfg.maintainer}}]

    def api(self, endpoint):
        return {"object": {"sha": self.base}}


class EvidenceExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = Config("owner/project", "keeper", root / "state", root / "work",
                          IMAGE, ("run",), (("check", "all"),), 1, 60, (), "main")
        self.gh = FakeGitHub(self.cfg)
        self.spec = parse_issue(BODY)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?)",
                         (7, self.spec.body_sha256, json.dumps(self.spec.allowed_paths)))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,"
                         "allowed_json,attempts_count,result_model,image_id) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (7, "work/issue-7-delivery", BASE, HEAD, 12,
                          "awaiting_review", json.dumps(self.spec.allowed_paths),
                          1, "provider/model", IMAGE))
        evidence = self.cfg.state_dir / "evidence" / "7"
        evidence.mkdir(parents=True)
        write_evidence(evidence / f"{HEAD}.json", {
            "issue": 7, "pr": 12, "branch": "work/issue-7-delivery",
            "base": BASE, "head": HEAD, "body_sha256": self.spec.body_sha256,
            "paths": ["src/one.py"], "model": "provider/model",
            "ci": {"head": HEAD, "committed_diff_base": BASE, "result": "passed",
                   "image_id": IMAGE,
                   "checks": [{"command": ["check", "all"], "exit_code": 0,
                               "log_sha256": "d" * 64,
                               "log": "/private/secret.log"}]}})

    def _patches(self):
        return (patch("ermik.evidence_export._github", return_value=self.gh),
                patch("ermik.evidence_export._resolve_image_id", return_value=IMAGE))

    def test_export_and_verify_without_controller_state(self):
        gh_patch, image_patch = self._patches()
        with gh_patch, image_patch:
            manifest = export_manifest(self.cfg, 12)
            self.assertEqual(manifest["unknowns"], ["usage"])
            self.assertEqual(manifest["checks"][0]["log_sha256"], "d" * 64)
            self.assertNotIn("/private/", json.dumps(manifest))
            other = Config(self.cfg.repo, self.cfg.maintainer,
                           Path(self.temp.name) / "other-state",
                           Path(self.temp.name) / "other-work", IMAGE,
                           self.cfg.worker_command, self.cfg.checks, 1, 60, (), "main")
            self.assertEqual(verify_manifest(other, 12, manifest)["status"], "current")

    def test_changed_remote_identity_rejects_old_record(self):
        gh_patch, image_patch = self._patches()
        with gh_patch, image_patch:
            manifest = export_manifest(self.cfg, 12)
            self.gh.head = "e" * 40
            self.assertEqual(verify_manifest(self.cfg, 12, manifest)["status"], "stale")
            self.gh.head = HEAD
            self.gh.issue_body = BODY + "\n"
            self.assertEqual(verify_manifest(self.cfg, 12, manifest)["status"], "stale")

    def test_changed_check_policy_and_missing_evidence_fail_closed(self):
        gh_patch, image_patch = self._patches()
        with gh_patch, image_patch:
            manifest = export_manifest(self.cfg, 12)
            altered = Config(self.cfg.repo, self.cfg.maintainer, self.cfg.state_dir,
                             self.cfg.workspace, IMAGE, self.cfg.worker_command,
                             (("check", "different"),), 1, 60, (), "main")
            self.assertEqual(verify_manifest(altered, 12, manifest)["status"], "stale")
            (self.cfg.state_dir / "evidence" / "7" / f"{HEAD}.json").unlink()
            with self.assertRaises(FileNotFoundError):
                export_manifest(self.cfg, 12)

    def test_recomputed_digest_does_not_authenticate_observations(self):
        gh_patch, image_patch = self._patches()
        with gh_patch, image_patch:
            manifest = export_manifest(self.cfg, 12)
            fields = {key: value for key, value in manifest.items()
                      if key not in ("version", "sha256")}
            fields["checks"] = [{"index": 1, "result": "passed",
                                 "log_sha256": "f" * 64}]
            altered = make_manifest(**fields)
            result = verify_manifest(self.cfg, 12, altered)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["observations"],
                         "controller_claim_not_independently_verified")

    def test_correction_on_same_pr_has_new_exact_head_record(self):
        gh_patch, image_patch = self._patches()
        with gh_patch, image_patch:
            before = export_manifest(self.cfg, 12)
            corrected_head = "e" * 40
            self.gh.head = corrected_head
            self.assertEqual(verify_manifest(self.cfg, 12, before)["status"], "stale")
            with state.connect(self.cfg.state_dir) as conn:
                conn.execute("UPDATE jobs SET head_sha=?,attempts_count=2 WHERE issue=7",
                             (corrected_head,))
            old_path = self.cfg.state_dir / "evidence" / "7" / f"{HEAD}.json"
            corrected = read_evidence(old_path)
            corrected["head"] = corrected_head
            corrected["ci"]["head"] = corrected_head
            corrected["ci"]["checks"][0]["log_sha256"] = "f" * 64
            write_evidence(old_path.with_name(f"{corrected_head}.json"), corrected)
            after = export_manifest(self.cfg, 12)
            self.assertEqual(after["pr"], before["pr"])
            self.assertNotEqual(after["head"], before["head"])
            self.assertNotEqual(after["sha256"], before["sha256"])
            self.assertEqual(verify_manifest(self.cfg, 12, after)["status"], "current")


if __name__ == "__main__":
    unittest.main()
