"""Bounded, identity-checked delivery across independent installations."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ermik.config import Config
from ermik.evidence import make_manifest
from ermik.evidence_transport import (MARKER, _body, _record, fetch_manifest,
                                      publish_manifest)
from ermik.github import GitHubError


HEAD = "b" * 40
BASE = "a" * 40
IMAGE = "sha256:" + "e" * 64


def manifest(head=HEAD, repo="owner/project", model="provider/model"):
    return make_manifest(repo=repo, head_repository="fork/project", issue=7, pr=12,
                         branch="work/issue-7-delivery", base=BASE, head=head,
                         body_sha256="c" * 64, scope_sha256="d" * 64,
                         checks_sha256="f" * 64, paths=["src/one.py"],
                         checks=[{"index": 1, "result": "passed",
                                  "log_sha256": "1" * 64}], image=IMAGE,
                         model=model, attempts=1,
                         usage={"observation": "unknown"},
                         unknowns=["usage"] if model else ["model", "usage"])


class FakeGitHub:
    def __init__(self):
        self.rows = []
        self.head = HEAD
        self.fail_after_post = False
        self.fail_before_post = False
        self.actor = "publisher"
        self.actor_id = 42

    def api(self, endpoint):
        if endpoint.startswith("issues/12/comments?per_page=100&page="):
            page = int(endpoint.rsplit("=", 1)[1])
            return self.rows[(page - 1) * 100:page * 100]
        raise AssertionError(endpoint)

    def command(self, args):
        if args == ["api", "user"]:
            return json.dumps({"login": self.actor, "id": self.actor_id})
        if self.fail_before_post:
            raise GitHubError("response lost")
        row = {"id": len(self.rows) + 1, "body": args[-1][5:],
               "user": {"login": self.actor, "id": self.actor_id}}
        self.rows.append(row)
        if self.fail_after_post:
            raise GitHubError("response lost")
        return json.dumps(row)

    def pr(self, number):
        return {"headRefOid": self.head}


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

    def test_missing_wrong_publisher_and_correction(self):
        self.assertEqual(fetch_manifest(self.cfg, 12), ({"status": "missing"}, None))
        self.gh.rows.append({"id": 1, "body": _body(manifest(model=None)),
                             "user": {"login": "other", "id": 99}})
        self.assertEqual(fetch_manifest(self.cfg, 12), ({"status": "missing"}, None))
        publish_manifest(self.cfg, 12)
        self.gh.head = "2" * 40
        result, old = fetch_manifest(self.cfg, 12)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(old["head"], HEAD)
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
