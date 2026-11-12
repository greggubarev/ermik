"""Exact-HEAD export uses local checks; verification needs only live GitHub."""

import json
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.evidence_export import export_manifest, verify_manifest, _live, _scope_sha256
from ermik.evidence import loads_manifest, make_manifest
from ermik.evidence_store import read_evidence, write_evidence
from ermik.github import GitHubError
from ermik.policy import parse_issue
from ermik.runtime import _admission_marker, WorkflowError
from ermik.runtime import _profile_sha
from ermik.providers import ModelCandidate
from ermik.review import _checks_sha256
from ermik import state


BASE = "a" * 40
HEAD = "b" * 40
IMAGE = "sha256:" + "c" * 64
RECEIVER_IMAGE = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "9" * 64
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
                "body": (f"Closes #7\nExecution image: `{IMAGE}`\n"
                         f"Execution image config: `{CONFIG_DIGEST}`"),
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
                   "image_id": IMAGE, "image_config_digest": CONFIG_DIGEST,
                   "checks": [{"command": ["check", "all"], "exit_code": 0,
                               "log_sha256": "d" * 64,
                               "log": "/private/secret.log"}]}})

    def _patches(self):
        return (patch("ermik.evidence_export._github", return_value=self.gh),
                patch("ermik.evidence_export._resolve_image_id", return_value=IMAGE),
                patch("ermik.evidence_export._portable_image_digest",
                      return_value=CONFIG_DIGEST))

    def test_live_overlaps_independent_remote_reads(self):
        base_started = Event()
        comments_started = Event()
        original_pr = self.gh.pr
        original_issue = self.gh.issue

        def pr(number):
            if not base_started.wait(2):
                raise AssertionError("base read did not overlap PR read")
            return original_pr(number)

        def base(_gh, _cfg):
            base_started.set()
            return BASE

        def issue(number):
            if not comments_started.wait(2):
                raise AssertionError("comments did not overlap Issue read")
            return original_issue(number)

        def comments(number):
            comments_started.set()
            return [{"body": _admission_marker(7, self.spec),
                     "user": {"login": self.cfg.maintainer}}]

        with (patch.object(self.gh, "pr", side_effect=pr),
              patch.object(self.gh, "issue", side_effect=issue),
              patch.object(self.gh, "comments", side_effect=comments),
              patch("ermik.evidence_export.main_sha", side_effect=base)):
            result = _live(self.cfg, self.gh, 12)
        self.assertEqual(result[0]["headRefOid"], HEAD)
        self.assertEqual(result[1], 7)

    def test_live_timeout_and_partial_admission_cannot_validate(self):
        with patch.object(self.gh, "comments",
                          side_effect=GitHubError("remote timeout")):
            with self.assertRaises(GitHubError):
                _live(self.cfg, self.gh, 12)
        with patch.object(self.gh, "comments", return_value=None):
            with self.assertRaises(TypeError):
                _live(self.cfg, self.gh, 12)

    def test_export_and_verify_without_controller_state(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
            manifest = export_manifest(self.cfg, 12)
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["image_config_digest"], CONFIG_DIGEST)
            self.assertEqual(manifest["unknowns"], ["usage"])
            self.assertEqual(manifest["checks"][0]["log_sha256"], "d" * 64)
            self.assertNotIn("/private/", json.dumps(manifest))
            other = Config(self.cfg.repo, self.cfg.maintainer,
                           Path(self.temp.name) / "other-state",
                           Path(self.temp.name) / "other-work", IMAGE,
                           self.cfg.worker_command, self.cfg.checks, 1, 60, (), "main")
            self.assertEqual(verify_manifest(other, 12, manifest)["status"], "current")

    def test_profiled_verify_reuses_live_snapshot_but_rechecks_route(self):
        model = ModelCandidate("provider", "model", "FREE_API_KEY",
                               "https://api.example.net/v1",
                               supported_efforts=("low",))
        selected = replace(model, effort="low")
        cfg = replace(self.cfg, models=(model,),
                      complexity_profiles={"easy": (selected,),
                                           "medium": (selected,),
                                           "hard": (selected,)})
        gh = FakeGitHub(cfg)
        issue = {"body": BODY, "labels": [{"name": name} for name in
                 ("queue:worker", "state:in-progress", "complexity:easy")]}
        marker = _admission_marker(7, self.spec, cfg, issue)
        v2 = make_manifest(
            repo=cfg.repo, head_repository=cfg.repo, issue=7, pr=12,
            branch="work/issue-7-delivery", base=BASE, head=HEAD,
            body_sha256=self.spec.body_sha256,
            scope_sha256=_scope_sha256(self.spec.allowed_paths),
            checks_sha256=_checks_sha256(cfg),
            paths=["src/one.py"],
            checks=[{"index": 1, "result": "passed", "log_sha256": "d" * 64}],
            image=IMAGE, image_config_digest=CONFIG_DIGEST,
            model=model.ref, attempts=1, usage={"observation": "unknown"},
            unknowns=["usage"])
        fields = {key: value for key, value in v2.items()
                  if key not in ("version", "sha256")}
        record = make_manifest(**fields, complexity="easy",
                               profile_sha256=_profile_sha(cfg, "easy"),
                               effort="low")
        live = (gh.pr(12), 7, self.spec, ["src/one.py"])
        with (patch("ermik.evidence_export._github", return_value=gh),
              patch.object(gh, "issue", return_value=issue) as issue_read,
              patch.object(gh, "comments", return_value=[
                  {"body": marker, "user": {"login": cfg.maintainer}}]),
              patch("ermik.evidence_export._resolve_image_id", return_value=IMAGE),
              patch("ermik.evidence_export._portable_image_digest",
                    return_value=CONFIG_DIGEST)):
            self.assertEqual(verify_manifest(cfg, 12, record,
                                             _live_result=live)["status"], "current")
            self.assertGreaterEqual(issue_read.call_count, 1)
            changed = {"body": BODY, "labels": issue["labels"] +
                       [{"name": "route:one"}, {"name": "route:two"}]}
            issue_read.return_value = changed
            self.assertEqual(verify_manifest(cfg, 12, record,
                                             _live_result=live)["status"], "stale")

    def test_routed_profile_rejects_model_from_another_provider(self):
        one = ModelCandidate("one", "model", "ONE_API_KEY",
                             "https://one.example.net/v1",
                             supported_efforts=("low",))
        go = ModelCandidate("go", "model", "GO_API_KEY",
                            "https://go.example.net/v1",
                            supported_efforts=("low",))
        selected = (replace(one, effort="low"), replace(go, effort="low"))
        cfg = replace(self.cfg, models=(one, go), routes={"route:go": "go"},
                      complexity_profiles={tier: selected for tier in
                                           ("easy", "medium", "hard")})
        issue = {"body": BODY, "labels": [{"name": name} for name in
                 ("queue:worker", "state:in-progress", "complexity:easy", "route:go")]}
        gh = FakeGitHub(cfg)
        marker = _admission_marker(7, self.spec, cfg, issue)
        profile = _profile_sha(cfg, "easy", "route:go")
        with state.connect(cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET route_key='route:go',complexity='easy',"
                         "profile_sha=?,result_effort='low' WHERE issue=7", (profile,))
        fields = dict(repo=cfg.repo, head_repository=cfg.repo, issue=7, pr=12,
                      branch="work/issue-7-delivery", base=BASE, head=HEAD,
                      body_sha256=self.spec.body_sha256,
                      scope_sha256=_scope_sha256(self.spec.allowed_paths),
                      checks_sha256=_checks_sha256(cfg), paths=["src/one.py"],
                      checks=[{"index": 1, "result": "passed",
                               "log_sha256": "d" * 64}],
                      image=IMAGE, image_config_digest=CONFIG_DIGEST,
                      model=one.ref, attempts=1, usage={"observation": "unknown"},
                      unknowns=["usage"], complexity="easy",
                      profile_sha256=profile, effort="low")
        record = make_manifest(**fields)
        with (patch("ermik.evidence_export._github", return_value=gh),
              patch.object(gh, "issue", return_value=issue),
              patch.object(gh, "comments", return_value=[
                  {"body": marker, "user": {"login": cfg.maintainer}}]),
              patch("ermik.evidence_export._resolve_image_id", return_value=IMAGE),
              patch("ermik.evidence_export._portable_image_digest",
                    return_value=CONFIG_DIGEST)):
            self.assertEqual(verify_manifest(cfg, 12, record),
                             {"status": "stale", "reason": "complexity_changed"})
            with self.assertRaisesRegex(WorkflowError,
                                        "approved complexity evidence is stale"):
                export_manifest(cfg, 12)
            with state.connect(cfg.state_dir) as conn:
                conn.execute("UPDATE jobs SET result_model=? WHERE issue=7", (go.ref,))
            self.assertEqual(export_manifest(cfg, 12)["model"], go.ref)
            self.assertEqual(verify_manifest(cfg, 12,
                             make_manifest(**{**fields, "model": go.ref}))["status"],
                             "current")

    def test_cross_host_local_id_may_differ_when_config_matches(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
            manifest = export_manifest(self.cfg, 12)
            receiver = Config(self.cfg.repo, self.cfg.maintainer,
                              Path(self.temp.name) / "receiver-state",
                              Path(self.temp.name) / "receiver-work", "receiver:tag",
                              self.cfg.worker_command, self.cfg.checks,
                              1, 60, (), "main")
            with patch("ermik.evidence_export._resolve_image_id",
                       return_value=RECEIVER_IMAGE) as resolved:
                result = verify_manifest(receiver, 12, manifest)
            self.assertEqual(result["status"], "current")
            self.assertNotEqual(RECEIVER_IMAGE, manifest["image"])
            resolved.assert_called_once_with("receiver:tag")

    def test_cross_host_changed_config_digest_rejected(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
            manifest = export_manifest(self.cfg, 12)
            with patch("ermik.evidence_export._resolve_image_id",
                       return_value=RECEIVER_IMAGE), patch(
                           "ermik.evidence_export._portable_image_digest",
                           return_value="sha256:" + "8" * 64):
                result = verify_manifest(self.cfg, 12, manifest)
            self.assertEqual(result,
                             {"status": "stale", "reason": "image_changed"})

    def test_legacy_manifest_without_portable_identity_is_stale(self):
        fixture = (Path(__file__).parent / "fixtures" / "evidence"
                   / "h1.v1.json").read_bytes()
        legacy = loads_manifest(fixture)
        self.assertEqual(legacy["version"], 1)
        self.assertNotIn("image_config_digest", legacy)
        self.assertEqual(verify_manifest(self.cfg, 12, legacy),
                         {"status": "stale", "reason": "image_identity_missing"})

    def test_missing_config_digest_in_saved_checks_blocks_export(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
            evidence_path = self.cfg.state_dir / "evidence" / "7" / f"{HEAD}.json"
            saved = read_evidence(evidence_path)
            del saved["ci"]["image_config_digest"]
            write_evidence(evidence_path, saved)
            with self.assertRaisesRegex(WorkflowError, "saved check evidence is stale"):
                export_manifest(self.cfg, 12)

    def test_changed_remote_identity_rejects_old_record(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
            manifest = export_manifest(self.cfg, 12)
            self.gh.head = "e" * 40
            self.assertEqual(verify_manifest(self.cfg, 12, manifest)["status"], "stale")
            self.gh.head = HEAD
            self.gh.issue_body = BODY + "\n"
            self.assertEqual(verify_manifest(self.cfg, 12, manifest)["status"], "stale")

    def test_changed_check_policy_and_missing_evidence_fail_closed(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
            manifest = export_manifest(self.cfg, 12)
            altered = Config(self.cfg.repo, self.cfg.maintainer, self.cfg.state_dir,
                             self.cfg.workspace, IMAGE, self.cfg.worker_command,
                             (("check", "different"),), 1, 60, (), "main")
            self.assertEqual(verify_manifest(altered, 12, manifest)["status"], "stale")
            (self.cfg.state_dir / "evidence" / "7" / f"{HEAD}.json").unlink()
            with self.assertRaises(FileNotFoundError):
                export_manifest(self.cfg, 12)

    def test_recomputed_digest_does_not_authenticate_observations(self):
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
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
        gh_patch, image_patch, digest_patch = self._patches()
        with gh_patch, image_patch, digest_patch:
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
