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

Version 1 is canonical UTF-8 JSON with sorted keys, compact separators, ASCII escapes, and one trailing newline. `sha256` hashes the canonical object without its own `sha256` field. The file is limited to 16 KiB. Required fields are `version`, `repo`, `head_repository`, `issue`, `pr`, `branch`, `base`, `head`, `body_sha256`, `scope_sha256`, `checks_sha256`, `paths`, `checks`, `image`, `model`, `attempts`, `usage`, `unknowns`, and `sha256`. Each check contains its index, pass result, and private-log digest; the ordered commands are represented by `checks_sha256`. Missing usage is marked `unknown`, never zero. The file excludes the issue body, credentials, raw logs, task output, and local file paths. Its digest detects accidental or unsophisticated changes, but anyone who can edit the file can recompute the digest. The controller that produced it remains trusted.

The manifest is intentionally a manual export. Automatic delivery to another installation and a consolidated review view are separate roadmap work.
