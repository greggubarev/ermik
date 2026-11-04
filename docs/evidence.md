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

## GitHub delivery

Set `"evidence_publisher": "PUBLISHER_LOGIN"` and `"evidence_publisher_id": PUBLISHER_NUMERIC_ID` in both installations' private configuration. Get both values from `gh api user --jq '{login,id}'` under the controller's upstream comment credential. The numeric ID binds the publisher even if its login changes. With this option enabled, each `run-once` or `poll` cycle publishes a bounded manifest as a PR timeline comment after a draft PR or correction is saved. A failed comment write leaves the PR awaiting review and is retried on the next cycle; the same HEAD is reconciled before another POST. A correction waits until the preceding HEAD's comment is confirmed. A private cursor rotates the bounded delivery queue so repeated failures for one PR do not prevent attempts for later PRs. The command output adds a `delivery` list alongside `work`.

The publisher needs upstream `Issues: write` **or** `Pull requests: write`; the maintainer installation needs only repository read access to fetch the comment. Neither transport command needs upstream Contents write. Fetch the current record on the separate installation:

```sh
python3 -m ermik --config config.json fetch-evidence PR_NUMBER --output /private/pr-evidence.json
```

`fetch-evidence` checks the pinned numeric comment author ID, canonical digest, current GitHub identities, ordered check policy, and image ID. It reports `missing`, `duplicate`, or `stale` explicitly and writes the file only when status is `current`. A correction creates a new comment for its HEAD; the old comment remains identifiable but cannot validate the new HEAD. If automatic delivery needs a retry before the next cycle, run `python3 -m ermik --config config.json publish-evidence PR_NUMBER` on the controller. A comment is editable or removable by a sufficiently privileged GitHub account, so the comment author and transport do not independently attest to the check outcomes. The maintainer still reruns local checks before merge.

Comment scanning stops after 1,000 timeline comments and reports an error rather than silently overlooking an older record. For a PR beyond that limit, use the manual file export and verification commands above.

The comment carries a canonical version 1 record with the execution-model identifier withheld; the corresponding field is `null` and listed in `unknowns`. The controller keeps the full local record. The comment contains no raw logs, issue text, credentials, or local paths. Manual `export-evidence` and `verify-evidence` remain available when comment delivery is not configured.

## Review packet

On the maintainer installation, inspect a draft PR before deciding whether to run a local review:

```sh
python3 -m ermik --config config.json review-packet PR_NUMBER
```

The read-only command checks the current Issue admission, PR base and HEAD, changed paths, evidence comment, configured checks, and image identity. Its `diff_url` opens GitHub's comparison of the two exact commits; inspect the full diff there. It separates the controller's reported check results from a local exact-HEAD review. A missing or stale record is reported explicitly with a nonzero exit status. The packet is a review aid; it does not run checks, write a review record, or authorize merge. Run `review PR_NUMBER` on the current HEAD before merging.
