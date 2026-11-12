"""Bounded, identity-checked delivery across independent installations."""

import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.evidence import make_manifest
from ermik.evidence_transport import (MARKER, UnsupportedEvidenceVersion,
                                      _body, _published_records, _record,
                                      fetch_manifest, publish_manifest)
from ermik.github import GitHubError


HEAD = "b" * 40
BASE = "a" * 40
IMAGE = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "9" * 64


def manifest(head=HEAD, repo="owner/project", model="provider/model"):
    return make_manifest(repo=repo, head_repository="fork/project", issue=7, pr=12,
                         branch="work/issue-7-delivery", base=BASE, head=head,
                         body_sha256="c" * 64, scope_sha256="d" * 64,
                         checks_sha256="f" * 64, paths=["src/one.py"],
                         checks=[{"index": 1, "result": "passed",
                                  "log_sha256": "1" * 64}], image=IMAGE,
                         image_config_digest=CONFIG_DIGEST,
                         model=model, attempts=1,
                         usage={"observation": "unknown"},
                         unknowns=["usage"] if model else ["model", "usage"])


def legacy_manifest():
    record = manifest(model=None)
    del record["image_config_digest"]
    del record["sha256"]
    record["version"] = 1
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    record["sha256"] = hashlib.sha256(payload).hexdigest()
    return record


class FakeGitHub:
    def __init__(self):
        self.rows = []
        self.head = HEAD
        self.fail_after_post = False
        self.fail_before_post = False
        self.actor = "publisher"
        self.actor_id = 42
        self.body = "Closes #7\n"
        self.requests = []
        self.deny_pointer = False
        self.incomplete_page = False
        self.move_head_on_pointer = False

    def api(self, endpoint, *, timeout=90):
        self.requests.append(endpoint)
        if endpoint.startswith("issues/12/comments?per_page=100&page="):
            page = int(endpoint.rsplit("=", 1)[1])
            if self.incomplete_page and page == 2:
                return [{"id": 999, "body": "late"}]
            return self.rows[(page - 1) * 100:page * 100]
        if endpoint.startswith("issues/comments/"):
            comment_id = int(endpoint.rsplit("/", 1)[1])
            return next(row for row in self.rows if row["id"] == comment_id)
        raise AssertionError(endpoint)

    def command(self, args):
        if args == ["api", "user"]:
            return json.dumps({"login": self.actor, "id": self.actor_id})
        if args[:3] == ["api", "--method", "PATCH"]:
            if self.move_head_on_pointer:
                self.head = "2" * 40
                raise GitHubError("response lost")
            if self.deny_pointer:
                raise GitHubError("permission denied")
            self.body = args[-1][5:]
            return json.dumps({"body": self.body, "head": {"sha": self.head}})
        if self.fail_before_post:
            raise GitHubError("response lost")
        row = {"id": len(self.rows) + 1, "body": args[-1][5:],
               "user": {"login": self.actor, "id": self.actor_id},
               "issue_url": "https://api.github.com/repos/owner/project/issues/12"}
        self.rows.append(row)
        if self.fail_after_post:
            raise GitHubError("response lost")
        return json.dumps(row)

    def pr(self, number):
        return {"headRefOid": self.head, "body": self.body}


class EvidenceTransportTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config("owner/project", "keeper", Path("/tmp/transport-state"),
                          Path("/tmp/transport-work"), IMAGE, ("run",),
                          (("check",),), 1, 60, (), "main",
                          evidence_publisher="publisher", evidence_publisher_id=42)
        self.gh = FakeGitHub()
        self.exported = manifest()
        self.gh_patch = patch("ermik.evidence_transport.GitHub", return_value=self.gh)
        self.export_patch = patch("ermik.evidence_transport.export_manifest",
                                  return_value=self.exported)
        self.verify_patch = patch("ermik.evidence_transport.verify_manifest",
                                  side_effect=lambda cfg, pr, record: {
                                      "status": "current" if record["head"] == self.gh.head
                                      else "stale", "reason": "identity_changed"})
        self.gh_patch.start()
        self.export_patch.start()
        self.verify_patch.start()
        self.addCleanup(self.gh_patch.stop)
        self.addCleanup(self.export_patch.stop)
        self.addCleanup(self.verify_patch.stop)

    def test_post_fetch_and_idempotent_retry_redacts_model(self):
        first = publish_manifest(self.cfg, 12)
        self.assertEqual(first["status"], "published")
        self.assertEqual(publish_manifest(self.cfg, 12)["status"], "existing")
        self.assertEqual(len(self.gh.rows), 1)
        self.assertNotIn("provider/model", self.gh.rows[0]["body"])
        self.assertIsNone(_record(self.gh.rows[0]["body"])["model"])
        result, record = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "current")
        self.assertEqual(record["sha256"], first["sha256"])
        self.assertTrue(self.gh.rows[0]["body"].startswith(MARKER))
        self.assertEqual(record["image_config_digest"], CONFIG_DIGEST)

    def test_v3_delivery_keeps_tier_and_redacts_model_and_effort(self):
        fields = {key: value for key, value in manifest().items()
                  if key not in ("version", "sha256")}
        self.exported = make_manifest(**fields, complexity="easy",
                                      profile_sha256="a" * 64, effort="low")
        with patch("ermik.evidence_transport.export_manifest",
                   return_value=self.exported):
            self.assertEqual(publish_manifest(self.cfg, 12)["status"], "published")
        record = _record(self.gh.rows[0]["body"])
        self.assertEqual(record["version"], 3)
        self.assertEqual(record["complexity"], "easy")
        self.assertIsNone(record["model"])
        self.assertIsNone(record["effort"])
        self.assertEqual(record["unknowns"], ["effort", "model", "usage"])

    def test_v1_comment_remains_parseable_as_history(self):
        legacy = legacy_manifest()
        body = _body(legacy)
        self.assertTrue(body.startswith("Ermik evidence v1\n"))
        self.assertEqual(_record(body), legacy)
        self.gh.rows.append({"id": 1, "body": body,
                             "user": {"login": "publisher", "id": 42}})
        found = _published_records(self.cfg, self.gh, 12)
        self.assertEqual(found[0][1], legacy)
        self.assertEqual(found[0][1]["version"], 1)
        with patch("ermik.evidence_transport.verify_manifest",
                   return_value={"status": "stale", "reason": "image_identity_missing"}):
            status, historical = fetch_manifest(self.cfg, 12)
        self.assertEqual(status["reason"], "image_identity_missing")
        self.assertEqual(historical, legacy)

    def test_missing_wrong_publisher_and_correction(self):
        self.assertEqual(fetch_manifest(self.cfg, 12), ({"status": "missing"}, None))
        self.gh.rows.append({"id": 1, "body": _body(manifest(model=None)),
                             "user": {"login": "other", "id": 99}})
        self.assertEqual(fetch_manifest(self.cfg, 12), ({"status": "missing"}, None))
        publish_manifest(self.cfg, 12)
        self.gh.head = "2" * 40
        result, old = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["head"], HEAD)
        self.assertIsNone(old)
        self.exported = manifest(head=self.gh.head)
        with patch("ermik.evidence_transport.export_manifest", return_value=self.exported):
            publish_manifest(self.cfg, 12)
        result, new = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "current")
        self.assertEqual(new["head"], self.gh.head)
        self.assertEqual(len(self.gh.rows), 3)

    def test_duplicate_exact_head_is_explicit(self):
        publish_manifest(self.cfg, 12)
        self.gh.rows.append({**self.gh.rows[-1], "id": 2})
        result, record = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "duplicate")
        self.assertIsNone(record)
        with self.assertRaises(GitHubError):
            publish_manifest(self.cfg, 12)

    def test_tampered_wrong_repo_and_private_path_rejected(self):
        publish_manifest(self.cfg, 12)
        original = self.gh.rows[0]["body"]
        self.gh.rows[0]["body"] = original.replace('"attempts":1', '"attempts":2')
        self.assertEqual(fetch_manifest(self.cfg, 12)[0],
                         {"status": "stale", "reason": "invalid_record"})
        self.gh.rows[0]["body"] = _body(manifest(repo="other/project", model=None))
        self.assertEqual(fetch_manifest(self.cfg, 12)[0],
                         {"status": "stale", "reason": "invalid_record"})
        self.gh.rows[0]["body"] = original.replace('"src/one.py"', '"/private/secret"')
        self.assertEqual(fetch_manifest(self.cfg, 12)[0],
                         {"status": "stale", "reason": "invalid_record"})

    def test_oversized_comment_and_too_many_comments_rejected(self):
        self.gh.rows.append({"id": 1, "body": MARKER + "x" * 17000 + "```",
                             "user": {"login": "publisher", "id": 42}})
        self.assertEqual(fetch_manifest(self.cfg, 12)[0],
                         {"status": "stale", "reason": "invalid_record"})
        self.gh.rows = [{"id": n, "body": "ordinary", "user": {"login": "other", "id": 99}}
                        for n in range(1001)]
        with self.assertRaises(GitHubError):
            fetch_manifest(self.cfg, 12)

    def test_pointed_exact_head_beyond_thousand_mixed_comments(self):
        published = publish_manifest(self.cfg, 12)
        self.gh.rows.extend({"id": n, "body": "ordinary",
                             "user": {"login": "other", "id": 99}}
                            for n in range(2, 1103))
        self.gh.requests.clear()
        result, record = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "current")
        self.assertEqual(record["sha256"], published["sha256"])
        self.assertEqual(len(self.gh.requests), 11)
        self.assertEqual(self.gh.requests[-1], "issues/comments/1")
        self.assertEqual(publish_manifest(self.cfg, 12)["status"], "existing")
        self.assertEqual(len(self.gh.rows), 1102)

    def test_pointed_comment_after_first_thousand_is_fetched_directly(self):
        record = manifest(model=None)
        self.gh.rows = [{"id": n, "body": "ordinary",
                         "user": {"login": "other", "id": 99}}
                        for n in range(1, 1002)]
        self.gh.rows.append({"id": 1002, "body": _body(record),
                             "user": {"login": "publisher", "id": 42},
                             "issue_url": "https://api.github.com/repos/owner/project/issues/12"})
        self.gh.body += (f"\n<!-- Ermik evidence comment head={HEAD} "
                         f"id=1002 sha256={record['sha256']} -->")
        result, fetched = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "current")
        self.assertEqual(fetched, record)
        self.assertEqual(self.gh.requests[-1], "issues/comments/1002")
        self.assertEqual(len(self.gh.requests), 11)

    def test_issues_write_only_keeps_comment_transport(self):
        self.gh.deny_pointer = True
        published = publish_manifest(self.cfg, 12)
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["pointer_status"], "unavailable")
        self.assertEqual(len(self.gh.rows), 1)
        self.assertEqual(fetch_manifest(self.cfg, 12)[0]["status"], "current")
        self.gh.deny_pointer = False
        retried = publish_manifest(self.cfg, 12)
        self.assertEqual(retried["status"], "existing")
        self.assertEqual(retried["pointer_status"], "confirmed")
        self.assertEqual(len(self.gh.rows), 1)

    def test_head_move_during_pointer_write_is_not_confirmed(self):
        self.gh.move_head_on_pointer = True
        with self.assertRaises(GitHubError):
            publish_manifest(self.cfg, 12)
        self.assertEqual(len(self.gh.rows), 1)

    def test_incomplete_terminal_page_never_current(self):
        publish_manifest(self.cfg, 12)
        self.gh.incomplete_page = True
        with self.assertRaisesRegex(GitHubError, "pagination is incomplete"):
            fetch_manifest(self.cfg, 12)

    def test_pointed_record_refusals_beyond_thousand_comments(self):
        publish_manifest(self.cfg, 12)
        self.gh.rows.extend({"id": n, "body": "ordinary",
                             "user": {"login": "other", "id": 99}}
                            for n in range(2, 1103))
        original_body = self.gh.rows[0]["body"]
        original_pr = self.gh.body
        cases = (
            ("older HEAD", lambda: setattr(self.gh, "head", "2" * 40)),
            ("wrong author", lambda: self.gh.rows[0]["user"].update(id=99)),
            ("oversized", lambda: self.gh.rows[0].update(body=MARKER + "x" * 17000 + "```")),
            ("wrong PR", lambda: self.gh.rows[0].update(issue_url="https://api.github.com/repos/owner/project/issues/13")),
            ("conflicting pointer", lambda: setattr(self.gh, "body", original_pr + "\n" +
                                                     original_pr.splitlines()[-1])),
            ("conflicting digest", lambda: setattr(self.gh, "body", original_pr.replace(
                "sha256=", "sha256=" + "0" * 64 + " "))),
        )
        for name, alter in cases:
            with self.subTest(name=name):
                self.gh.head = HEAD
                self.gh.body = original_pr
                self.gh.rows[0]["body"] = original_body
                self.gh.rows[0]["user"]["id"] = 42
                self.gh.rows[0]["issue_url"] = "https://api.github.com/repos/owner/project/issues/12"
                alter()
                result, record = fetch_manifest(self.cfg, 12)
                self.assertEqual(result["status"], "stale")
                self.assertIsNone(record)

    def test_duplicate_publisher_record_in_scanned_window_refused_at_cap(self):
        publish_manifest(self.cfg, 12)
        self.gh.rows.append({**self.gh.rows[0], "id": 2})
        self.gh.rows.extend({"id": n, "body": "ordinary",
                             "user": {"login": "other", "id": 99}}
                            for n in range(3, 1003))
        result, record = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "duplicate")
        self.assertIsNone(record)

    def test_future_marker_from_pinned_publisher_is_unsupported(self):
        body = "Ermik evidence v4\n\n```json\n{}\n```"
        self.gh.rows.append({"id": 1, "body": body,
                             "user": {"login": "publisher", "id": 99}})
        self.assertEqual(fetch_manifest(self.cfg, 12), ({"status": "missing"}, None))
        self.gh.rows[0]["user"]["id"] = 42
        with self.assertRaises(UnsupportedEvidenceVersion):
            _published_records(self.cfg, self.gh, 12)
        self.assertEqual(fetch_manifest(self.cfg, 12),
                         ({"status": "unsupported_version"}, None))
        for marker in ("Ermik evidence v4", "Ermik evidence v01"):
            with self.subTest(marker=marker):
                self.gh.rows[0]["body"] = marker
                self.assertEqual(fetch_manifest(self.cfg, 12),
                                 ({"status": "unsupported_version"}, None))
        self.gh.rows[0]["body"] = "Ermik evidence v3.0"
        self.assertEqual(fetch_manifest(self.cfg, 12),
                         ({"status": "stale", "reason": "invalid_record"}, None))
        self.gh.rows[0]["body"] = body + "x" * 17000
        self.assertEqual(fetch_manifest(self.cfg, 12),
                         ({"status": "stale", "reason": "invalid_record"}, None))

    def test_v2_payload_under_v1_marker_is_invalid(self):
        body = _body(manifest(model=None)).replace("Ermik evidence v2", "Ermik evidence v1")
        self.gh.rows.append({"id": 1, "body": body,
                             "user": {"login": "publisher", "id": 42}})
        self.assertEqual(fetch_manifest(self.cfg, 12),
                         ({"status": "stale", "reason": "invalid_record"}, None))

    def test_lost_post_response_reconciles_without_second_comment(self):
        self.gh.fail_after_post = True
        self.assertEqual(publish_manifest(self.cfg, 12)["status"], "existing")
        self.assertEqual(len(self.gh.rows), 1)
        self.gh.rows.clear()
        self.gh.fail_after_post = False
        self.gh.fail_before_post = True
        with self.assertRaises(GitHubError):
            publish_manifest(self.cfg, 12)
        self.assertEqual(self.gh.rows, [])

    def test_wrong_actor_refused_before_post(self):
        self.gh.actor_id = 99
        with self.assertRaises(GitHubError):
            publish_manifest(self.cfg, 12)
        self.assertEqual(self.gh.rows, [])

    def test_renamed_publisher_id_remains_trusted_reused_login_is_not(self):
        self.gh.actor = "new-name"
        publish_manifest(self.cfg, 12)
        self.assertEqual(fetch_manifest(self.cfg, 12)[0]["status"], "current")
        self.gh.rows.append({"id": 2, "body": self.gh.rows[0]["body"],
                             "user": {"login": "publisher", "id": 99}})
        self.assertEqual(fetch_manifest(self.cfg, 12)[0]["status"], "current")
        self.gh.actor = "publisher"
        self.gh.actor_id = 99
        with self.assertRaises(GitHubError):
            publish_manifest(self.cfg, 12)


if __name__ == "__main__":
    unittest.main()
