# PR evidence manifest

The controller can export a compact record for one published PR HEAD. Run this on the execution host after the draft PR appears:

```sh
python3 -m ermik --config config.json export-evidence PR_NUMBER --output /private/pr-evidence.json
```

Copy the file to the maintainer host through a channel you control. Check its identity there against the current GitHub Issue and PR, configured check policy, and immutable task image:

```sh
python3 -m ermik --config config.json verify-evidence PR_NUMBER --input /private/pr-evidence.json
```

Export checks the controller's private exact-HEAD check record before creating the manifest. Verification reports `current` only for the recorded Issue, branch, base commit, PR HEAD, admission scope, changed paths, check policy, and image identity. It reports `stale` when an identity changes; malformed or oversized input is rejected. The `observations` field makes clear that the check outcomes, attempts, and usage are controller claims, not independently verified on the maintainer host. Run verification again after every correction. A previous manifest never approves a new HEAD. The command does not merge or replace the maintainer's local review.

The version 1 manifest uses canonical JSON and a SHA-256 digest. It contains the repository and PR identity, issue body and scope hashes, changed paths, ordered check-policy hash and pass results, private check-log digests, image identifier, attempt count, and available usage observations. Missing usage is marked `unknown`. It excludes the issue body, credentials, raw logs, task output, and local file paths. The digest detects changes to the file; it is not a signature or independent attestation. The controller that produced the record remains trusted.

The manifest is intentionally a manual export. Automatic delivery to another installation and a consolidated review view are separate roadmap work.
