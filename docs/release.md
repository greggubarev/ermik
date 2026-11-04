# Source release gate

## Local verification on each supported host

Use a clean checkout at the same commit on Windows and Linux. Python 3.11 or newer and Git are required. Run the unit gate on both hosts; run the Docker gate on a host with a working Docker daemon. Preserve each JSON report with the release record and compare its `commit` with the commit selected for the source archive. `unit.skipped` and `unit.unexpected_skips` are reported separately; an unexpected skip or failed test makes the command exit nonzero. The Docker gate runs both host smoke scripts and records the Docker server version and exact image ID.

```sh
mkdir -p "$HOME/ermik-verification"
python3 -m ermik.host_verify > "$HOME/ermik-verification/unit-linux.json"
docker build -f examples/Dockerfile.demo -t ermik-demo:verification .
python3 -m ermik.host_verify --docker --image ermik-demo:verification > "$HOME/ermik-verification/docker-linux.json"
```

On Windows, use `py -3 -m ermik.host_verify` from the same commit and save its JSON outside the checkout. Run the Docker command there only if the Docker daemon is available. The gate permits only documented platform skips; it does not silently treat missing Git or Docker as a passing check. The `--docker` option is required to make Docker smoke part of the result. Review the recorded failure and skip counts before publishing.

For a project check that produces JUnit XML, require a minimum number of **executed** cases. Skipped cases do not count toward that minimum:

```sh
python3 -m ermik.ci /private/checks/report.xml --min-tests 1
```

The JUnit gate rejects missing, malformed, partial, inconsistent, failed, and skipped-only reports. Choose `--min-tests` for the actual project check; one is only the default example.

## Source archive

Run this gate on a reviewed release commit before publishing a source archive. Keep the output directory outside the checkout. The gate requires a clean checkout at the named full commit ID. It produces a source-only archive from tracked files, a SHA-256 file, and a manifest, and rejects unexpected content. A successful scan describes that commit and archive; it does not approve a later commit or a separately built container image.

```sh
git status --short
git rev-parse HEAD
python3 -m ermik.release_gate --commit "$(git rev-parse HEAD)" --output /private/ermik-release
```

The output files are `ermik-source-<sha12>.tar.gz`, `ermik-source-<sha12>.sha256`, and `ermik-source-<sha12>.manifest.json`, where `<sha12>` is the first 12 characters of the commit ID. The manifest records the full commit, archive digest, file count, and an inventory of bundled and referenced components. Verify the checksum and inspect the manifest and archive file list before publication. Review the tree and reachable Git history for credentials, local paths, and unrelated project references; findings identify locations without printing matching values. Confirm the archive contains only the intended tracked source, documentation, examples, license, and notices. Do not add a local config, provider credential, token, state database, log, worktree, checkout, backup, or another project's files. Preserve the manifest and checksum with the release record. Re-run the gate for every changed commit; do not reuse an earlier result.

```sh
cd /private/ermik-release
release_id=REPLACE_WITH_FIRST_12_COMMIT_CHARACTERS
sha256sum -c "ermik-source-${release_id}.sha256"
tar -tzf "ermik-source-${release_id}.tar.gz"
```

## Scope and notices

The source archive includes [`assets/workflow.svg`](../assets/workflow.svg), a repository-owned diagram. Inspect its text and metadata as well as its rendered content. The archive does not bundle a Docker image, third-party Python distribution, or operating-system package. The source is offered under [`LICENSE`](../LICENSE).

The example Dockerfiles reference external components. [`examples/Dockerfile.demo`](../examples/Dockerfile.demo) starts from `python:3.11-alpine`. [`examples/Dockerfile.opencode`](../examples/Dockerfile.opencode) starts from a pinned `ghcr.io/anomalyco/opencode` image and installs Alpine `python3` and `ca-certificates`. Before distributing a built image, inventory the actual image layers and packages, review their current licenses and redistribution terms, and include every required license and notice. A source gate result does not cover image contents, package notices, or a custom task image.

## If a secret is found

Stop publication and restrict access to the candidate archive and findings. Revoke or rotate the exposed credential through its issuer, then assess where the value was exposed, including Git history, prior archives, logs, and remote copies. Remove the value from the source and release inputs. If it entered published Git history, coordinate a history rewrite and downstream cleanup with the repository maintainer; deleting the working-tree file alone is insufficient. Run the gate again on a reviewed commit and inspect the new archive before resuming publication. Do not paste the secret into an issue or a public report.

## Output retention

Keep the reviewed archive, manifest, and checksum in the private release location until publication and verification are complete. Remove failed or superseded local candidates after confirming their exact paths and any investigation or rollback need. Do not commit generated release files or local scan data to this repository.
