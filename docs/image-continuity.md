# Container image continuity

The controller resolves the configured Docker image to an immutable `sha256:` ID and saves it in the job ledger before starting the first container. Worker runs, provider fallback, local checks, retries, corrections, and publication recovery use that saved ID. A missing image or a legacy job that already ran without a saved ID stops for inspection. The draft PR declares the ID from its exact-HEAD check evidence; the publication broker verifies this value before creating or reconciling the PR.

The maintainer installation compares the PR declaration to its locally configured image, runs checks by that ID, and records it in the review. A changed declaration, image, check policy, HEAD, or base invalidates the review before merge. Keep the published image available until the PR is finished. If a mutable tag moves, set the maintainer's `image` configuration to the published ID while reviewing that older PR.

## Verified trial

On a Linux Docker host, the full test suite passed (278 tests). The disposable [tag replacement gate](../tests/host_image_retag.py) ran the first container by the saved image ID, changed the configured tag between two offline checks, and verified that both checks still ran by the original ID:

```sh
docker build -f examples/Dockerfile.demo -t ermik-demo:latest .
PYTHONPATH=. python3 -B tests/host_image_retag.py ermik-demo:latest
```

In a separate disposable repository, the controller published a draft PR with the execution ID. An independent maintainer state directory and workspace rejected review after the tag was moved to a second image. Configuring the maintainer with the original ID allowed review of the same exact HEAD and repeated the local check. The trial PR and Issue were closed without merging, and the test branch was removed.

The maintainer and controller installations in this trial used separate state and workspaces on one Docker host. Later cross-host handoff evidence is described below; a portable image identity remains open. A PR description is a claim from the trusted publisher, not a cryptographic attestation.

## Cross-host image limit

The earlier Linux-to-Windows evidence handoff fetched current and stale records, but the Windows installation used the Linux server's Docker daemon to inspect the image. A later [Issue #32](https://github.com/greggubarev/ermik/issues/32) trial verified read-only delivery with a repository-scoped credential and separate receiver state; see [release status](readiness.md#verified). It did not validate an image on an independent Docker host.

For disposable [PR #80](https://github.com/greggubarev/semiauto-e2e-smoke/pull/80), the same image was transferred with `docker save` and `docker load` from Docker Desktop 29's containerd image store to a separate Docker Engine 28 host. The publisher's local `docker image inspect .Id` was `sha256:5a32cee005ac342773ce4f3cab46c8e537f4365852fa3931adba112a98abd359`; the receiving host reported a different `.Id`, beginning `sha256:f4ef9b`, while the RootFS layer digests matched. The receiving host's temporary image was removed after comparison. Ermik binds review to the publisher's host-local ID, so this comparison cannot establish a portable image identity or count as a successful independent image check. [Issue #69](https://github.com/greggubarev/ermik/issues/69) tracks that limit.
