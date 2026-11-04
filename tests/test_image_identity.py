"""Tests for portable Docker config identity and archive rejection."""

import hashlib
import gzip
import io
import json
import tarfile
import unittest
from unittest.mock import patch

from ermik.image_identity import ImageIdentityError, _portable_digest, resolve_image_identity


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fixture(*, layers=(b"layer-one",), config_update=None, manifest_update=None,
            config_bytes_override=None, extra=()):
    diff_ids = [digest(layer) for layer in layers]
    config = {"architecture": "amd64", "os": "linux", "config": {"Cmd": ["true"]},
              "rootfs": {"type": "layers", "diff_ids": diff_ids}}
    if config_update:
        config_update(config)
    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_name = digest(config_bytes)[7:] + ".json"
    if config_bytes_override is not None:
        config_bytes = config_bytes_override
    layer_names = [f"layer{i}/layer.tar" for i in range(len(layers))]
    manifest = [{"Config": config_name, "RepoTags": ["demo:latest"],
                 "Layers": layer_names.copy()}]
    if manifest_update:
        manifest_update(manifest)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in [(config_name, config_bytes),
                           *zip(layer_names, layers),
                           ("manifest.json", json.dumps(manifest).encode()), *extra]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue(), digest(config_bytes)


def oci_fixture(*, extra_platform=False, command="true"):
    layer = b"layer-one"
    compressed = gzip.compress(layer, mtime=0)
    config = {"architecture": "amd64", "os": "linux", "config": {"Cmd": [command]},
              "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_digest = digest(config_bytes)
    layer_digest = digest(compressed)
    leaf = {"schemaVersion": 2, "config": {"digest": config_digest},
            "layers": [{"digest": layer_digest}]}
    leaf_bytes = json.dumps(leaf).encode()
    leaf_digest = digest(leaf_bytes)
    descriptor = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                  "digest": leaf_digest, "platform": {"os": "linux", "architecture": "amd64"}}
    descriptors = [descriptor, dict(descriptor)] if extra_platform else [descriptor]
    index = {"manifests": descriptors}
    manifest = [{"Config": "blobs/sha256/" + config_digest[7:],
                 "Layers": ["blobs/sha256/" + layer_digest[7:]]}]
    members = [("blobs/sha256/" + config_digest[7:], config_bytes),
               ("blobs/sha256/" + layer_digest[7:], compressed),
               ("blobs/sha256/" + leaf_digest[7:], leaf_bytes),
               ("index.json", json.dumps(index).encode()),
               ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
               ("manifest.json", json.dumps(manifest).encode())]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue(), config_digest


INSPECT = {"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64",
           "RootFS": {"Layers": [digest(b"layer-one")]}}


class ArchiveTests(unittest.TestCase):
    def test_valid_archive_returns_config_byte_digest(self):
        archive, expected = fixture()
        self.assertEqual(_portable_digest(io.BytesIO(archive), INSPECT), expected)

    def test_ambiguous_platforms_are_rejected(self):
        archive, _ = fixture(manifest_update=lambda m: m.append(dict(m[0])))
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(archive), INSPECT)

    def test_same_layers_with_changed_execution_config_have_distinct_identity(self):
        first, first_digest = fixture()
        second, second_digest = fixture(config_update=lambda c: c["config"].update(Cmd=["false"]))
        self.assertNotEqual(first_digest, second_digest)
        self.assertEqual(_portable_digest(io.BytesIO(first), INSPECT), first_digest)
        self.assertEqual(_portable_digest(io.BytesIO(second), INSPECT), second_digest)

    def test_config_name_and_layer_bytes_are_verified(self):
        wrong_name, _ = fixture(manifest_update=lambda m: m[0].update(Config="0" * 64 + ".json"))
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(wrong_name), INSPECT)
        altered, _ = fixture(config_update=lambda c: c["rootfs"].update(diff_ids=[digest(b"other")]))
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(altered), INSPECT)

    def test_config_bytes_and_layer_order_are_verified(self):
        tampered, _ = fixture(config_bytes_override=b'{}')
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(tampered), INSPECT)
        layers = (b"layer-one", b"layer-two")
        inspected = {**INSPECT, "RootFS": {"Layers": [digest(layer) for layer in layers]}}
        reordered, _ = fixture(layers=layers,
                               manifest_update=lambda m: m[0]["Layers"].reverse())
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(reordered), inspected)

    def test_archive_paths_and_duplicate_members_are_rejected(self):
        for extra in (("../escape", b"x"), ("manifest.json", b"[]")):
            with self.subTest(extra=extra[0]):
                archive, _ = fixture(extra=(extra,))
                with self.assertRaises(ImageIdentityError):
                    _portable_digest(io.BytesIO(archive), INSPECT)

    def test_local_inspection_must_match_ordered_layers(self):
        archive, _ = fixture()
        changed = {**INSPECT, "RootFS": {"Layers": [digest(b"other")]}}
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(archive), changed)

    def test_oci_gzip_layers_and_nested_platform_ambiguity(self):
        archive, expected = oci_fixture()
        self.assertEqual(_portable_digest(io.BytesIO(archive), INSPECT), expected)
        second, second_digest = oci_fixture(command="false")
        self.assertNotEqual(expected, second_digest)
        self.assertEqual(_portable_digest(io.BytesIO(second), INSPECT), second_digest)
        ambiguous, _ = oci_fixture(extra_platform=True)
        with self.assertRaises(ImageIdentityError):
            _portable_digest(io.BytesIO(ambiguous), INSPECT)

    def test_repeated_layer_blob_can_represent_repeated_diffid(self):
        layers = (b"layer-one", b"layer-one")
        inspected = {**INSPECT, "RootFS": {"Layers": [digest(layer) for layer in layers]}}
        archive, expected = fixture(layers=layers,
                                    manifest_update=lambda m: m[0]["Layers"].__setitem__(1, m[0]["Layers"][0]))
        self.assertEqual(_portable_digest(io.BytesIO(archive), inspected), expected)


class _Process:
    def __init__(self, archive):
        self.stdout = io.BytesIO(archive)
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class ResolverTests(unittest.TestCase):
    def test_returns_local_id_and_portable_digest(self):
        archive, expected = fixture()
        with patch("ermik.image_identity._inspect", side_effect=[INSPECT] * 3) as inspect, \
                patch("ermik.image_identity.subprocess.Popen", return_value=_Process(archive)) as popen:
            self.assertEqual(resolve_image_identity("demo:latest"), (INSPECT["Id"], expected))
        self.assertEqual([call.args[0] for call in inspect.call_args_list],
                         ["demo:latest", "demo:latest", INSPECT["Id"]])
        self.assertEqual(popen.call_args.args[0][-1], "demo:latest")

    def test_tag_replacement_after_save_fails(self):
        archive, _ = fixture()
        replaced = {**INSPECT, "Id": "sha256:" + "b" * 64}
        with patch("ermik.image_identity._inspect", side_effect=[INSPECT, replaced]), \
                patch("ermik.image_identity.subprocess.Popen", return_value=_Process(archive)):
            with self.assertRaises(ImageIdentityError):
                resolve_image_identity("demo:latest")


if __name__ == "__main__":
    unittest.main()
