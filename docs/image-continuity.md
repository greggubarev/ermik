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

The maintainer and controller installations in this trial used separate state and workspaces on one Docker host. A physical cross-host evidence handoff and an authenticated portable record remain separate work in the [roadmap](roadmap.md). A PR description is a claim from the trusted publisher, not a cryptographic attestation.
