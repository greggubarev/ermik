"""Verify a Docker image's local run reference and portable config identity."""

from __future__ import annotations

import hashlib
import gzip
import json
import re
import subprocess
import tarfile
import tempfile
import threading


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONFIG_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_BLOB_NAME = re.compile(r"blobs/sha256/[0-9a-f]{64}\Z")
_MAX_JSON = 16 * 1024 * 1024
_MAX_METADATA_TOTAL = 64 * 1024 * 1024
_MAX_MEMBERS = 100_000
_CHUNK = 1024 * 1024


class ImageIdentityError(RuntimeError):
    """Docker did not provide one verifiable image identity."""


def _inspect(image: str) -> dict:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .}}", image],
            capture_output=True, timeout=30, check=True,
        )
        if len(result.stdout) > _MAX_JSON:
            raise ImageIdentityError("Docker image inspection is too large")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not _DIGEST.fullmatch(data.get("Id", "")):
            raise ImageIdentityError("Docker image has no valid immutable ID")
        return data
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise ImageIdentityError("Docker image inspection failed") from exc


def _safe_name(name: str) -> bool:
    return (bool(name) and not name.startswith("/") and "\\" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def _read_json(member: tarfile.TarInfo, stream) -> tuple[object, str]:
    if member.size > _MAX_JSON:
        raise ImageIdentityError("Docker image metadata is too large")
    data = stream.read(member.size + 1)
    if len(data) != member.size:
        raise ImageIdentityError("Truncated Docker image metadata")
    try:
        return json.loads(data), "sha256:" + hashlib.sha256(data).hexdigest()
    except (ValueError, UnicodeError) as exc:
        raise ImageIdentityError("Invalid Docker image metadata") from exc


def _digest_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class _HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, count=-1):
        data = self.stream.read(count)
        self.digest.update(data)
        return data


class _PrefixedReader:
    def __init__(self, prefix, reader):
        self.prefix = prefix
        self.reader = reader

    def read(self, count=-1):
        if self.prefix:
            if count < 0:
                data, self.prefix = self.prefix, b""
                return data + self.reader.read()
            data, self.prefix = self.prefix[:count], self.prefix[count:]
            return data + self.reader.read(count - len(data))
        return self.reader.read(count)


def _hash_blob(stream) -> tuple[str, str]:
    """Hash stored bytes and uncompressed bytes without buffering a layer."""
    source = _HashingReader(stream)
    prefix = source.read(2)
    if prefix == b"\x1f\x8b":
        expanded = hashlib.sha256()
        try:
            with gzip.GzipFile(fileobj=_PrefixedReader(prefix, source)) as decoded:
                while chunk := decoded.read(_CHUNK):
                    expanded.update(chunk)
        except (OSError, EOFError) as exc:
            raise ImageIdentityError("Invalid compressed Docker layer") from exc
        if source.read(1):
            raise ImageIdentityError("Trailing Docker layer bytes")
        return "sha256:" + source.digest.hexdigest(), "sha256:" + expanded.hexdigest()
    expanded = hashlib.sha256(prefix)
    while chunk := source.read(_CHUNK):
        expanded.update(chunk)
    value = "sha256:" + expanded.hexdigest()
    return value, value


def _check_oci_index(metadata, config_digest, layer_names, payload_hashes, inspected):
    if "index.json" not in metadata:
        return
    if metadata.get("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise ImageIdentityError("Invalid OCI image layout")
    leaves = []
    visited = set()

    def walk(index, depth):
        if depth > 3 or not isinstance(index, dict):
            raise ImageIdentityError("Invalid OCI image index")
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or not descriptors:
            raise ImageIdentityError("Invalid OCI image index")
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise ImageIdentityError("Invalid OCI image descriptor")
            name = "blobs/sha256/" + str(descriptor.get("digest", ""))[7:]
            if (not isinstance(descriptor.get("digest"), str)
                    or not _DIGEST.fullmatch(descriptor["digest"])
                    or name in visited or name not in metadata):
                raise ImageIdentityError("Invalid OCI image descriptor")
            visited.add(name)
            kind = descriptor.get("mediaType")
            if kind == "application/vnd.oci.image.index.v1+json":
                walk(metadata[name], depth + 1)
            elif kind == "application/vnd.oci.image.manifest.v1+json":
                annotation = descriptor.get("annotations") or {}
                if not isinstance(annotation, dict):
                    raise ImageIdentityError("Invalid OCI image descriptor")
                if annotation.get("vnd.docker.reference.type") == "attestation-manifest":
                    continue
                platform = descriptor.get("platform") or {}
                if not isinstance(platform, dict):
                    raise ImageIdentityError("Invalid OCI image descriptor")
                if platform and (platform.get("os") != inspected.get("Os")
                                 or platform.get("architecture") != inspected.get("Architecture")):
                    raise ImageIdentityError("OCI image platform does not match inspection")
                leaves.append(metadata[name])
            else:
                raise ImageIdentityError("Unsupported OCI image descriptor")

    walk(metadata["index.json"], 0)
    if len(leaves) != 1 or not isinstance(leaves[0], dict):
        raise ImageIdentityError("Docker image platform selection is ambiguous")
    leaf = leaves[0]
    config = leaf.get("config")
    layers = leaf.get("layers")
    if (not isinstance(config, dict) or config.get("digest") != config_digest
            or not isinstance(layers, list) or len(layers) != len(layer_names)
            or any(not isinstance(layer, dict) or layer.get("digest") !=
                   payload_hashes.get(name, (None, None))[0]
                   for layer, name in zip(layers, layer_names))):
        raise ImageIdentityError("OCI manifest does not match Docker image manifest")


def _portable_digest(archive, inspected: dict) -> str:
    """Read a Docker save stream once, hashing layers with bounded memory."""
    metadata: dict[str, object] = {}
    metadata_hashes: dict[str, str] = {}
    metadata_size = 0
    payload_hashes: dict[str, tuple[str, str]] = {}
    names: set[str] = set()
    try:
        with tarfile.open(fileobj=archive, mode="r|*") as saved:
            for count, member in enumerate(saved, 1):
                name = member.name.rstrip("/") if member.isdir() else member.name
                if count > _MAX_MEMBERS or not _safe_name(name):
                    raise ImageIdentityError("Unsafe Docker image archive")
                if name in names or not (member.isfile() or member.isdir()):
                    raise ImageIdentityError("Ambiguous Docker image archive")
                names.add(name)
                if member.isdir():
                    continue
                stream = saved.extractfile(member)
                if stream is None:
                    raise ImageIdentityError("Unreadable Docker image member")
                with stream:
                    if name in ("manifest.json", "index.json", "oci-layout") or _CONFIG_NAME.fullmatch(name):
                        metadata_size += member.size
                        if metadata_size > _MAX_METADATA_TOTAL:
                            raise ImageIdentityError("Docker image metadata is too large")
                        metadata[name], metadata_hashes[name] = _read_json(member, stream)
                    elif _BLOB_NAME.fullmatch(name):
                        first = stream.read(1)
                        prefixed = _PrefixedReader(first, stream)
                        if first in (b"{", b"[") and member.size <= _MAX_JSON:
                            metadata_size += member.size
                            if metadata_size > _MAX_METADATA_TOTAL:
                                raise ImageIdentityError("Docker image metadata is too large")
                            metadata[name], metadata_hashes[name] = _read_json(member, prefixed)
                        else:
                            payload_hashes[name] = _hash_blob(prefixed)
                        observed = metadata_hashes.get(name) or payload_hashes[name][0]
                        if observed != "sha256:" + name.rsplit("/", 1)[-1]:
                            raise ImageIdentityError("Docker image blob digest mismatch")
                    else:
                        if name.endswith(".json"):
                            raise ImageIdentityError("Unexpected Docker image metadata")
                        value = _digest_stream(stream)
                        payload_hashes[name] = (value, value)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ImageIdentityError("Invalid Docker image archive") from exc

    manifest = metadata.get("manifest.json")
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ImageIdentityError("Docker image platform selection is ambiguous")
    entry = manifest[0]
    if not isinstance(entry, dict):
        raise ImageIdentityError("Invalid Docker image manifest")
    config_name, layer_names = entry.get("Config"), entry.get("Layers")
    if (not isinstance(config_name, str)
            or not (_CONFIG_NAME.fullmatch(config_name) or _BLOB_NAME.fullmatch(config_name))
            or not isinstance(layer_names, list) or not all(
                isinstance(name, str) and _safe_name(name) for name in layer_names)):
        raise ImageIdentityError("Invalid Docker image manifest")
    config = metadata.get(config_name)
    if not isinstance(config, dict) or config_name not in names:
        raise ImageIdentityError("Docker image config is missing")
    # The archive member name is the content address of the exact config bytes.
    # Re-encoding parsed JSON would change that address, so hash raw bytes below.
    config_digest = metadata_hashes.get(config_name)
    expected_config = config_name.rsplit("/", 1)[-1].removesuffix(".json")
    if config_digest is None or config_digest != "sha256:" + expected_config:
        raise ImageIdentityError("Docker image config digest mismatch")
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if (not isinstance(config.get("config"), dict)
            or not isinstance(rootfs, dict) or rootfs.get("type") != "layers"
            or config.get("os") != inspected.get("Os")
            or config.get("architecture") != inspected.get("Architecture")
            or not isinstance(diff_ids, list)
            or len(diff_ids) != len(layer_names)
            or not all(isinstance(value, str) and _DIGEST.fullmatch(value)
                       for value in diff_ids)):
        raise ImageIdentityError("Docker image config does not match inspection")
    observed_layers = (inspected.get("RootFS") or {}).get("Layers")
    if diff_ids != observed_layers:
        raise ImageIdentityError("Docker image layers do not match inspection")
    if [payload_hashes.get(name, (None, None))[1] for name in layer_names] != diff_ids:
        raise ImageIdentityError("Docker image layers do not match config")
    _check_oci_index(metadata, config_digest, layer_names, payload_hashes, inspected)
    return config_digest


def _saved_portable_digest(image: str, inspected: dict) -> str:
    """Verify one saved reference, with a deadline across the archive stream."""
    with tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                ["docker", "image", "save", image], stdout=subprocess.PIPE,
                stderr=stderr,
            )
        except OSError as exc:
            raise ImageIdentityError("Docker image save failed") from exc
        watchdog = threading.Timer(300, process.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            if process.stdout is None:
                raise ImageIdentityError("Docker image save has no stream")
            with process.stdout:
                portable = _portable_digest(process.stdout, inspected)
            if process.wait(timeout=60) != 0:
                raise ImageIdentityError("Docker image save failed")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            watchdog.cancel()
    return portable


def resolve_image_identity(image: str) -> tuple[str, str]:
    """Return (local immutable Docker ID, portable config-byte SHA-256 digest)."""
    if not isinstance(image, str) or not image or "\x00" in image:
        raise ImageIdentityError("Invalid Docker image reference")
    before = _inspect(image)
    local_id = before["Id"]
    # Saving the tag detects ambiguous platform selections. Saving the pinned ID
    # separately binds the chosen config even if a tag moves away and back.
    portable = _saved_portable_digest(image, before)
    if image != local_id and _saved_portable_digest(local_id, before) != portable:
        raise ImageIdentityError("Docker image reference changed during save")
    if _inspect(image)["Id"] != local_id or _inspect(local_id)["Id"] != local_id:
        raise ImageIdentityError("Docker image reference changed during save")
    return local_id, portable
