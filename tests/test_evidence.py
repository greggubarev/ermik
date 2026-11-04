"""Canonical handoff record validation; no remote trust is inferred."""

import copy
import hashlib
import json
import unittest

from ermik.evidence import dumps_manifest, loads_manifest, make_manifest, validate_manifest


class EvidenceTests(unittest.TestCase):
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
