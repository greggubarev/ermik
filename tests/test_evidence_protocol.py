"""Versioned portable image identity in evidence manifests."""

from pathlib import Path
import unittest

from ermik.evidence import (UnsupportedManifestVersion, dumps_manifest,
                            loads_manifest, make_manifest, validate_manifest)


FIXTURE = Path(__file__).parent / "fixtures" / "evidence" / "h1.v1.json"
CONFIG_DIGEST = "sha256:" + "f" * 64


class EvidenceProtocolTests(unittest.TestCase):
    def v1(self):
        return loads_manifest(FIXTURE.read_bytes())

    def v2(self):
        fields = self.v1()
        fields.pop("version")
        fields.pop("sha256")
        return make_manifest(**fields, image_config_digest=CONFIG_DIGEST)

    def test_v1_fixture_remains_exactly_readable_and_writable(self):
        original = FIXTURE.read_bytes()
        record = loads_manifest(original)
        self.assertEqual(record["version"], 1)
        self.assertNotIn("image_config_digest", record)
        self.assertEqual(dumps_manifest(record), original)

    def test_v2_round_trip_binds_local_and_portable_identities(self):
        record = self.v2()
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["image_config_digest"], CONFIG_DIGEST)
        self.assertEqual(record["image"], self.v1()["image"])
        self.assertEqual(loads_manifest(dumps_manifest(record)), record)

    def test_v2_requires_valid_config_digest(self):
        fields = self.v1()
        fields.pop("version")
        fields.pop("sha256")
        with self.assertRaisesRegex(ValueError, "invalid manifest fields"):
            make_manifest(**fields)
        for value in (None, "f" * 64, "sha256:" + "F" * 64,
                      "sha256:" + "f" * 63, "sha512:" + "f" * 64):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "invalid image config digest"):
                make_manifest(**fields, image_config_digest=value)

    def test_versioned_fields_cannot_be_silently_added_or_removed(self):
        old = self.v1()
        with self.assertRaisesRegex(ValueError, "invalid manifest fields"):
            validate_manifest({**old, "image_config_digest": CONFIG_DIGEST})
        new = self.v2()
        missing = dict(new)
        del missing["image_config_digest"]
        with self.assertRaisesRegex(ValueError, "invalid manifest fields"):
            validate_manifest(missing)
        with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
            validate_manifest({**new, "image_config_digest": "sha256:" + "e" * 64})
        for version in (0, 3, "2", 2.0, True):
            with self.subTest(version=version), self.assertRaises(
                    UnsupportedManifestVersion):
                validate_manifest({**new, "version": version})

    def test_v2_rejects_duplicate_fields_in_serialized_record(self):
        raw = dumps_manifest(self.v2())
        duplicate = raw.replace(b'"version":2', b'"version":2,"version":2')
        self.assertNotEqual(raw, duplicate)
        with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
            loads_manifest(duplicate)


if __name__ == "__main__":
    unittest.main()
