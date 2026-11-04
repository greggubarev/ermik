"""Canonical handoff record validation; no remote trust is inferred."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ermik.config import Config
from ermik.evidence import (UnsupportedManifestVersion, dumps_manifest,
                            loads_manifest, make_manifest, validate_manifest)
from ermik.evidence_export import verify_manifest
from ermik.policy import parse_issue


FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
BODY = ("## Goal\nUpdate the example.\n\n## Allowed paths\nsrc\n\n"
        "## Acceptance\nThe check passes.\n")


class EvidenceTests(unittest.TestCase):
    def fixture(self, head):
        return (FIXTURES / f"{head}.v1.json").read_bytes()

    def test_fixture_correction_and_stale_head(self):
        h1 = loads_manifest(self.fixture("h1"))
        h2 = loads_manifest(self.fixture("h2"))
        self.assertEqual(dumps_manifest(h1), self.fixture("h1"))
        self.assertEqual(dumps_manifest(h2), self.fixture("h2"))
        self.assertEqual((h1["head"], h2["head"]), ("1" * 40, "2" * 40))
        self.assertEqual((h1["sha256"], h2["sha256"]), (
            "4a0019e844b5377f98f62c1131459054431ae59cf362692db8829f8c50e4f81b",
            "7c9018f0aae721c3f2b85b0ab5f93021ff90a188f484877f3c50397a7df3cf3d"))
        cfg = Config("owner/example", "maintainer", Path("state"), Path("work"),
                     h2["image"], ("run",), (("check", "example"),), 1, 60, (), "main")
        pr = {"headRefName": h2["branch"], "baseRefOid": h2["base"],
              "headRefOid": h2["head"], "body": "synthetic"}
        spec = parse_issue(BODY)
        with (patch("ermik.evidence_export._github"),
              patch("ermik.evidence_export._live", return_value=(pr, 1, spec, h2["paths"])),
              patch("ermik.evidence_export.published_image_id", return_value=h2["image"]),
              patch("ermik.evidence_export._resolve_image_id", return_value=h2["image"])):
            self.assertEqual(verify_manifest(cfg, 2, h2)["status"], "current")
            self.assertEqual(verify_manifest(cfg, 2, h1),
                             {"status": "stale", "reason": "identity_changed"})
            changed_checks = Config("owner/example", "maintainer", Path("state"),
                                    Path("work"), h2["image"], ("run",),
                                    (("check", "different"),), 1, 60, (), "main")
            self.assertEqual(verify_manifest(changed_checks, 2, h2),
                             {"status": "stale", "reason": "identity_changed"})

    def test_fixture_version_dispatch_and_rejections(self):
        raw = self.fixture("h1")
        record = loads_manifest(raw)
        for version in (2, 0, 1.0, "1", True):
            changed = {**record, "version": version, "future_required": "value"}
            with self.subTest(version=version), self.assertRaisesRegex(
                    UnsupportedManifestVersion, "unsupported manifest version"):
                validate_manifest(changed)
            with self.subTest(version=version, input="bytes"), self.assertRaisesRegex(
                    UnsupportedManifestVersion, "unsupported manifest version"):
                loads_manifest(json.dumps(changed).encode() + b"\n")
        for changed in ({**record, "future_required": "value"},
                        {**record, "raw_log": "private output"},
                        {**record, "paths": ["C:/private/file"]},
                        {**record, "sha256": "0" * 64}):
            with self.subTest(keys=set(changed)), self.assertRaises(ValueError):
                validate_manifest(changed)
        with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
            loads_manifest(raw.replace(b'"pr":2', b'"pr":2,"pr":2'))
        with self.assertRaisesRegex(ValueError, "manifest size"):
            loads_manifest(raw + b" " * 16384)

    def fields(self):
        scope = hashlib.sha256(json.dumps(
            ["src", "tests"], separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        checks = hashlib.sha256(json.dumps(
            (("python3", "-m", "unittest"),), separators=(",", ":"),
            ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        return {"repo": "owner/project", "head_repository": "fork/project",
                "issue": 7, "pr": 12, "branch": "work/issue-7-delivery",
                "base": "a" * 40, "head": "b" * 40,
                "body_sha256": "c" * 64, "scope_sha256": scope,
                "checks_sha256": checks,
                "paths": ["src/one.py", "tests/test_one.py"],
                "checks": [{"index": 1, "result": "passed", "log_sha256": "d" * 64}],
                "image": "sha256:" + "e" * 64, "model": "provider/model",
                "attempts": 2,
                "usage": {"observation": "measured", "input": 100, "output": 20,
                          "reasoning": 0, "cache_read": 5, "cache_write": 0,
                          "total": 120, "estimated_cost": 0.01},
                "unknowns": []}

    def test_round_trip_and_order_independence(self):
        fields = self.fields()
        record = make_manifest(**fields)
        self.assertEqual(loads_manifest(dumps_manifest(record)), record)
        self.assertEqual(make_manifest(**dict(reversed(list(fields.items())))), record)
        self.assertEqual(record["sha256"], hashlib.sha256(
            json.dumps({key: value for key, value in record.items() if key != "sha256"},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest())

    def test_explicit_unknown_observations(self):
        fields = self.fields()
        fields.update(model=None, usage={"observation": "unknown"},
                      unknowns=["model", "usage"])
        self.assertEqual(loads_manifest(dumps_manifest(make_manifest(**fields)))["unknowns"],
                         ["model", "usage"])

    def test_reject_tampering_and_missing_fields(self):
        record = make_manifest(**self.fields())
        for mutation in (lambda r: r.update(head="f" * 40),
                         lambda r: r.pop("pr"),
                         lambda r: r.pop("branch"),
                         lambda r: r.pop("head_repository"),
                         lambda r: r.pop("scope_sha256"),
                         lambda r: r.pop("checks_sha256"),
                         lambda r: r.update(secret="token"),
                         lambda r: r.update(commands=[["private", "command"]]),
                         lambda r: r["checks"][0].update(log="/private/ci.log")):
            changed = copy.deepcopy(record)
            mutation(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_manifest(changed)

    def test_reject_invalid_identities_and_private_data(self):
        for key, value in (("repo", "../project"),
                           ("head_repository", "../fork"),
                           ("branch", "work/issue-8-delivery"),
                           ("branch", "work/issue-7-../secret"),
                           ("scope_sha256", "A" * 64),
                           ("checks_sha256", ["python3", "-m", "unittest"]),
                           ("issue", True),
                           ("pr", 0), ("base", "ABC"),
                           ("image", "image:latest"),
                           ("paths", ["C:/private/file"]),
                           ("paths", ["src/../secret"]),
                           ("paths", ["tests/x", "src/x"]),
                           ("model", "provider/model token"),
                           ("attempts", True),
                           ("unknowns", ["log"]),
                           ("usage", {"observation": "unknown"})):
            fields = self.fields()
            fields[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                make_manifest(**fields)

    def test_reject_noncanonical_and_duplicate_json(self):
        data = dumps_manifest(make_manifest(**self.fields()))
        for invalid in (data[:-1], b" " + data, data.replace(b'"pr":12', b'"pr":12,"pr":12')):
            with self.subTest(invalid=invalid[:24]), self.assertRaises(ValueError):
                loads_manifest(invalid)

    def test_reject_oversized_record(self):
        fields = self.fields()
        fields["paths"] = [f"src/{n:03d}-" + "x" * 500 for n in range(100)]
        with self.assertRaises(ValueError):
            make_manifest(**fields)


if __name__ == "__main__":
    unittest.main()
