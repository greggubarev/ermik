# Container image continuity

The controller resolves the configured Docker image to an immutable local `sha256:` ID and saves it in the job ledger before starting the first container. Worker runs, provider fallback, local checks, retries, corrections, and publication recovery use that saved ID. A missing image or a legacy job that already ran without a saved ID stops for inspection. Exact-HEAD checks also verify a SHA-256 digest of the raw image config bytes. The draft PR declares both the local execution ID and config digest; the publication broker requires both before creating or reconciling the PR.

The maintainer installation verifies the PR's config digest against its own configured image, runs checks by its own local ID, and records both local and published IDs. The IDs may differ across Docker stores; the config digest must match. A changed declaration, image config, check policy, HEAD, or base invalidates the review before merge. Keep the reviewed image available until the PR is finished. If a mutable tag moves, configure the maintainer to use a local image with the published config digest while reviewing that older PR.

## Verified trial

On a Linux Docker host, the full test suite passed (278 tests). The disposable [tag replacement gate](../tests/host_image_retag.py) ran the first container by the saved image ID, changed the configured tag between two offline checks, and verified that both checks still ran by the original ID:

```sh
docker build -f examples/Dockerfile.demo -t ermik-demo:latest .
PYTHONPATH=. python3 -B tests/host_image_retag.py ermik-demo:latest
```

In a separate disposable repository, the controller published a draft PR with the execution ID. An independent maintainer state directory and workspace rejected review after the tag was moved to a second image. Configuring the maintainer with the original ID allowed review of the same exact HEAD and repeated the local check. The trial PR and Issue were closed without merging, and the test branch was removed.

The maintainer and controller installations in this trial used separate state and workspaces on one Docker host. Later independent-store image evidence is described below. A PR description is a claim from the trusted publisher, not a cryptographic attestation.

## Independent Docker stores

The earlier Linux-to-Windows evidence handoff fetched current and stale records, but the Windows installation used the Linux server's Docker daemon to inspect the image. A later [Issue #32](https://github.com/greggubarev/ermik/issues/32) trial verified read-only delivery with a repository-scoped credential and separate receiver state; see [release status](readiness.md#verified). It did not validate an image on an independent Docker host.

An earlier transfer for disposable [PR #80](https://github.com/greggubarev/semiauto-e2e-smoke/pull/80) showed that `docker image inspect .Id` can differ between Docker stores even when RootFS layer digests match. That trial did not verify a portable identity.

In a later two-daemon comparison, Windows Docker 29.8.0 reported local ID `sha256:c981ab1b09375072f6c03067c64afac22685e026b7db27b4eec3cc8dc31fff1c`; Linux Engine 28.2.2 reported local ID `sha256:752227514bc8ac7e7ca8c46193b43727d555234e0c7fa8cb5ee6203d4d4ee48b`. Both resolved the raw config digest as `sha256:752227514bc8ac7e7ca8c46193b43727d555234e0c7fa8cb5ee6203d4d4ee48b`. This establishes matching config identity for those two stores. A complete independent-store Issue-to-PR review and merge trial remains unverified; [Issue #69](https://github.com/greggubarev/ermik/issues/69) remains open.
