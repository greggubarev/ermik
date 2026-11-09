"""Focused checks for the read-only review packet boundary."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.evidence import make_manifest
from ermik.evidence_export import _scope_sha256
from ermik.github import GitHubError
from ermik.policy import parse_issue
from ermik.providers import ModelCandidate
from ermik.review import _checks_sha256
from ermik.review_packet import build_review_packet
from ermik.runtime import WorkflowError, _profile_sha


HEAD = "b" * 40
BASE = "a" * 40
OLD = "c" * 40
IMAGE = "sha256:" + "d" * 64
IMAGE_CONFIG_DIGEST = "sha256:" + "e" * 64
BODY = "## Allowed paths\nsrc/one.py\n\n## Goal\nFix the parser\n\n## Acceptance\nChecks pass"


class ReviewPacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cfg = Config("owner/project", "keeper", root / "state", root / "work",
                          "image:stable", ("run",), (("check",),), 1, 60, (), "main")
        self.pr = {"headRefName": "work/issue-7-parser", "baseRefOid": BASE,
                   "headRefOid": HEAD, "body": "Closes #7"}
        self.spec = parse_issue(BODY)
        self.live = patch("ermik.review_packet._live",
                          return_value=(self.pr, 7, self.spec, ["src/one.py"]))
        self.image = patch("ermik.review_packet.published_image_id", return_value=IMAGE)
        self.image_config = patch("ermik.review_packet.published_image_config_digest",
                                  return_value=IMAGE_CONFIG_DIGEST)
        self.live.start()
        self.image.start()
        self.image_config.start()
        self.addCleanup(self.live.stop)
        self.addCleanup(self.image.stop)
        self.addCleanup(self.image_config.stop)

    def record(self, head=HEAD):
        return {"version": 2, "repo": self.cfg.repo, "issue": 7, "pr": 12,
                "head_repository": self.cfg.repo,
                "branch": self.pr["headRefName"], "base": BASE, "head": head,
                "body_sha256": self.spec.body_sha256, "paths": ["src/one.py"],
                "checks_sha256": _checks_sha256(self.cfg), "image": IMAGE,
                "image_config_digest": IMAGE_CONFIG_DIGEST,
                "sha256": "f" * 64,
                "checks": [{"index": 1, "result": "passed",
                            "log_sha256": "1" * 64}],
                "model": "provider/model", "usage": {"estimated_cost": 1}}

    def test_current_evidence_has_bounded_claims_and_missing_review(self):
        record = self.record()
        with patch("ermik.review_packet.fetch_manifest",
                   return_value=({"status": "current"}, record)):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "current")
        self.assertEqual(packet["review"], {"status": "missing"})
        self.assertEqual(packet["diff_url"],
                         f"https://github.com/owner/project/compare/{BASE}...{HEAD}")
        self.assertEqual(packet["goal"], "Fix the parser")
        self.assertEqual(packet["acceptance"], "Checks pass")
        self.assertEqual(packet["evidence"]["observation"], "publisher_claim")
        self.assertEqual(packet["evidence"]["model_ref"], "provider/model")
        self.assertNotIn("estimated_cost", str(packet))
        self.assertNotIn("log_sha256", str(packet))
        self.assertNotIn(str(self.cfg.state_dir), str(packet))

    def test_profiled_v3_evidence_is_current_in_review_packet(self):
        from dataclasses import replace

        model = ModelCandidate("provider", "model", "FREE_API_KEY",
                               "https://api.example.net/v1",
                               supported_efforts=("low",))
        selected = replace(model, effort="low")
        self.cfg = replace(self.cfg, models=(model,),
                           complexity_profiles={"easy": (selected,),
                                                "medium": (selected,),
                                                "hard": (selected,)})
        fields = {key: value for key, value in self.record().items()
                  if key not in ("version", "sha256")}
        fields["scope_sha256"] = _scope_sha256(self.spec.allowed_paths)
        fields["attempts"] = 1
        fields["usage"] = {"observation": "unknown"}
        fields["unknowns"] = ["usage"]
        record = make_manifest(**fields, complexity="easy",
                               profile_sha256=_profile_sha(self.cfg, "easy"),
                               effort="low")
        with patch("ermik.review_packet.fetch_manifest",
                   return_value=({"status": "current"}, record)):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "current")
        self.assertTrue(packet["head_verified"])
        self.assertEqual(packet["evidence"]["head"], HEAD)

    def test_profiled_packet_rejects_legacy_version_claim(self):
        from dataclasses import replace

        self.cfg = replace(self.cfg, complexity_profiles={"easy": (),
                                                          "medium": (),
                                                          "hard": ()})
        with patch("ermik.review_packet.fetch_manifest",
                   return_value=({"status": "current"}, self.record())):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertEqual(packet["evidence"]["reason"], "identity_changed")

    def test_current_correction_keeps_previous_head_as_history(self):
        record = self.record()
        def fetched(_cfg, _number, *, _records_out, **_kwargs):
            _records_out.extend([({"id": 1}, {"head": OLD}),
                                 ({"id": 2}, record)])
            return {"status": "current"}, record
        with patch("ermik.review_packet.fetch_manifest", side_effect=fetched):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "current")
        self.assertEqual(packet["previous_head"], OLD)
        self.assertEqual(packet["head"], HEAD)
        self.assertEqual(packet["evidence"]["head"], HEAD)

    def test_packet_reuses_first_verified_snapshot_and_comment_list(self):
        record = self.record()
        first = (self.pr, 7, self.spec, ["src/one.py"])
        def fetched(_cfg, _number, *, _live_result, _records_out):
            self.assertEqual(_live_result, first)
            _records_out.extend([({"id": 1}, {"head": OLD}),
                                 ({"id": 2}, record)])
            return {"status": "current"}, record
        with (patch("ermik.review_packet._live", side_effect=[first, first]) as live,
              patch("ermik.review_packet.fetch_manifest", side_effect=fetched) as fetch):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(live.call_count, 2)
        fetch.assert_called_once()
        self.assertEqual(packet["previous_head"], OLD)
        self.assertEqual(packet["status"], "current")

    def test_reused_snapshot_cannot_hide_head_change_on_final_read(self):
        record = self.record()
        first = (self.pr, 7, self.spec, ["src/one.py"])
        moved = ({**self.pr, "headRefOid": "e" * 40}, 7, self.spec,
                 ["src/one.py"])
        def fetched(_cfg, _number, *, _live_result, _records_out):
            self.assertEqual(_live_result, first)
            _records_out.append(({"id": 2}, record))
            return {"status": "current"}, record
        with (patch("ermik.review_packet._live", side_effect=[first, moved]),
              patch("ermik.review_packet.fetch_manifest", side_effect=fetched)):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertFalse(packet["head_verified"])
        self.assertIsNone(packet["head"])
        self.assertNotIn("checks", packet["evidence"])

    def test_current_local_review_is_separate_from_publisher_claim(self):
        (self.cfg.state_dir / "reviews").mkdir(parents=True)
        (self.cfg.state_dir / "reviews" / "pr-12.json").write_text("{}")
        local = {"repo": self.cfg.repo, "issue": 7, "pr": 12,
                 "head_repository": self.cfg.repo,
                 "branch": self.pr["headRefName"], "base": BASE, "head": HEAD,
                 "body_sha256": self.spec.body_sha256, "paths": ["src/one.py"],
                 "checks_sha256": _checks_sha256(self.cfg),
                 "image_id": "sha256:" + "9" * 64,
                 "published_image_id": IMAGE,
                 "image_config_digest": IMAGE_CONFIG_DIGEST,
                 "decision": "reviewed", "ci": {"result": "passed",
                                                 "private_log": "/secret/log"}}
        with (patch("ermik.review_packet._load", return_value=local),
              patch("ermik.review_packet._review_policy", return_value=IMAGE),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "current"}, self.record()))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "current")
        self.assertEqual(packet["review"], {"status": "current", "head": HEAD})
        self.assertNotIn("/secret/log", str(packet))

    def test_local_review_without_config_digest_is_stale(self):
        (self.cfg.state_dir / "reviews").mkdir(parents=True)
        (self.cfg.state_dir / "reviews" / "pr-12.json").write_text("{}")
        local = {"repo": self.cfg.repo, "issue": 7, "pr": 12,
                 "head_repository": self.cfg.repo,
                 "branch": self.pr["headRefName"], "base": BASE, "head": HEAD,
                 "body_sha256": self.spec.body_sha256, "paths": ["src/one.py"],
                 "checks_sha256": _checks_sha256(self.cfg),
                 "published_image_id": IMAGE,
                 "decision": "reviewed", "ci": {"result": "passed"}}
        with (patch("ermik.review_packet._load", return_value=local),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "current"}, self.record()))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "current")
        self.assertEqual(packet["review"], {
            "status": "stale", "head": HEAD,
            "reason_code": "stale_local_review",
            "next_action": "review_current_head"})
        self.assertTrue(packet["head_verified"])

    def test_manifest_without_config_digest_is_stale(self):
        legacy = self.record()
        legacy.pop("image_config_digest")
        with patch("ermik.review_packet.fetch_manifest",
                   return_value=({"status": "current"}, legacy)):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertEqual(packet["evidence"], {"status": "stale",
                                              "reason": "identity_changed"})

    def test_final_pr_config_change_marks_packet_stale(self):
        with (patch("ermik.review_packet.published_image_config_digest",
                    side_effect=[IMAGE_CONFIG_DIGEST, "sha256:" + "0" * 64]),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "current"}, self.record()))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertEqual(packet["evidence"], {"status": "stale",
                                              "reason": "identity_changed"})

    def test_head_moves_between_live_snapshot_and_current_fetch(self):
        second_head = "e" * 40
        with patch("ermik.review_packet.fetch_manifest",
                   return_value=({"status": "current"}, self.record(second_head))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertEqual(packet["evidence"], {"status": "stale",
                                              "reason": "identity_changed"})
        self.assertEqual(packet["head"], HEAD)
        self.assertNotIn("checks", packet["evidence"])

    def test_head_moves_during_comment_history_read(self):
        moved = {**self.pr, "headRefOid": "e" * 40}
        with (patch("ermik.review_packet._live", side_effect=[
                  (self.pr, 7, self.spec, ["src/one.py"]),
                  (moved, 7, self.spec, ["src/one.py"])]),
              patch("ermik.review_packet._review_summary",
                    return_value={"status": "current", "head": HEAD}),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "current"}, self.record()))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertEqual(packet["evidence"], {"status": "stale",
                                              "reason": "identity_changed"})
        self.assertNotIn("checks", packet["evidence"])
        self.assertIsNone(packet["head"])
        self.assertFalse(packet["head_verified"])
        self.assertEqual(packet["review"], {"status": "stale"})

    def test_correction_marks_old_review_stale_and_shows_both_heads(self):
        (self.cfg.state_dir / "reviews").mkdir(parents=True)
        (self.cfg.state_dir / "reviews" / "pr-12.json").write_text("{}")
        with (patch("ermik.review_packet._load", return_value={"head": OLD,
                                                                  "private_log": "/secret/log"}),
              patch("ermik.review_packet.fetch_manifest", return_value=(
                  {"status": "stale", "reason": "head_changed", "head": OLD}, None))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertEqual(packet["previous_head"], OLD)
        self.assertEqual(packet["head"], HEAD)
        self.assertEqual(packet["review"], {"status": "stale", "head": OLD})
        self.assertNotIn("/secret/log", str(packet))

    def test_invalid_remote_issue_does_not_echo_untrusted_path(self):
        with patch("ermik.review_packet._live",
                   side_effect=ValueError("unsafe path: '/private/password'")):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet, {"status": "stale", "reason": "remote_identity"})

    def test_remote_read_failure_is_unavailable_not_stale(self):
        with patch("ermik.review_packet._live",
                   side_effect=GitHubError("private transport details")):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet, {"status": "incomplete", "reason": "remote_unavailable"})
        self.assertNotIn("private transport details", str(packet))

    def test_missing_evidence_cannot_bypass_final_head_check(self):
        moved = {**self.pr, "headRefOid": "e" * 40}
        with (patch("ermik.review_packet._live", side_effect=[
                  (self.pr, 7, self.spec, ["src/one.py"]),
                  (moved, 7, self.spec, ["src/one.py"])]),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "missing"}, None))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")
        self.assertIsNone(packet["head"])
        self.assertIsNone(packet["diff_url"])
        self.assertFalse(packet["head_verified"])

    def test_final_read_failure_cannot_present_head_as_verified(self):
        with (patch("ermik.review_packet._live", side_effect=[
                  (self.pr, 7, self.spec, ["src/one.py"]),
                  GitHubError("private transport details")]),
              patch("ermik.review_packet._review_summary",
                    return_value={"status": "current", "head": HEAD}),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "missing"}, None))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "incomplete")
        self.assertEqual(packet["reason"], "remote_unavailable")
        self.assertIsNone(packet["head"])
        self.assertFalse(packet["head_verified"])
        self.assertEqual(packet["review"], {"status": "unavailable"})
        self.assertNotIn("private transport details", str(packet))

    def test_suspicious_issue_text_falls_back_to_issue_reference(self):
        spec = parse_issue(BODY.replace("Fix the parser", "token=ghp_private_value"))
        with (patch("ermik.review_packet._live", return_value=(self.pr, 7, spec,
                                                                ["src/one.py"])),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "missing"}, None))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["goal"], "See Issue #7")
        self.assertEqual(packet["status"], "missing")
        self.assertNotIn("ghp_private_value", str(packet))

    def test_bidi_and_non_ascii_issue_text_stay_in_issue(self):
        spec = parse_issue(BODY.replace("Fix the parser", "Fix \u202e parser"))
        with (patch("ermik.review_packet._live", return_value=(self.pr, 7, spec,
                                                                ["src/one.py"])),
              patch("ermik.review_packet.fetch_manifest",
                    return_value=({"status": "missing"}, None))):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["goal"], "See Issue #7")
        self.assertNotIn("\u202e", str(packet))

    def test_live_identity_failure_cannot_be_current(self):
        with patch("ermik.review_packet._live", side_effect=WorkflowError("base stale")):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "stale")

    def test_unsupported_evidence_version_stays_explicit(self):
        with patch("ermik.review_packet.fetch_manifest", return_value=(
                {"status": "unsupported_version"}, None)):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["status"], "unsupported_version")
        self.assertEqual(packet["evidence"], {"status": "unsupported_version"})

    def test_legacy_manifest_reason_stays_explicit(self):
        with patch("ermik.review_packet.fetch_manifest", return_value=(
                {"status": "stale", "reason": "image_identity_missing"}, None)):
            packet = build_review_packet(self.cfg, 12)
        self.assertEqual(packet["evidence"], {"status": "stale",
                                              "reason": "image_identity_missing"})


if __name__ == "__main__":
    unittest.main()
