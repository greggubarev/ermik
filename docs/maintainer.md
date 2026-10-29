# Maintaining issues and PRs

The maintainer selects issues, admits them to the queue, reviews each draft PR, and decides whether to request changes or merge. The `ermik` controller executes work and opens draft PRs; it does not merge automatically. Start with [setup.md](setup.md) and a disposable repository.

## Prepare an issue

1. Compare the goal with the code, open issues, and PRs. One issue should describe one verifiable result.
2. Create an open issue with `queue:worker` and `state:proposed`. Its body must have exactly the level-two headings `Goal`, `Allowed paths`, and `Acceptance`. Put one relative path or directory prefix per line under `Allowed paths`, **without list markers**. Absolute paths, `..`, backslashes, `*?[]` patterns, and duplicates are rejected.
3. Describe the observable change in `Goal`. In `Acceptance`, state positive and negative cases, manual checks, and scope limits. Local check commands belong in `config.json` under `checks`, not in the issue. Confirm that this configuration's common checks fit the task.
4. Inspect dependencies, resources, path overlap with active jobs, and controller permissions. Run `python -m ermik --config config.json doctor` and `status`. After deciding to admit the issue, run `admit NUMBER` **on the maintainer's computer**, using their GitHub account. It publishes an approval comment bound to hashes of the body and allowed paths, and changes `state:proposed` to `state:ready`. The server reads that approval from GitHub; a shared SQLite database is unnecessary. An issue body edit invalidates admission and requires review and a new admission.

Example issue body:

```md
## Goal
Handle an empty value in the example module.

## Allowed paths
src/example.py
tests/test_example.py

## Acceptance
A normal value returns the expected result. An empty value is rejected
without changing data. Test both cases and describe the result.
```

## Review and decision

1. Read the latest full diff, linked issue, and acceptance criteria. Verify base and HEAD SHAs, changed paths, behavior, negative case, and manual checks. The controller validates scope and `checks`; the maintainer evaluates the actual change.
2. Run `python -m ermik --config config.json review PR`. It creates a separate checkout at the exact HEAD, validates the published diff, repeats configured `checks`, and records local evidence for the current base, HEAD, and issue body. Also run checks outside the common configuration when required. This is local evidence, not proof of GitHub Actions or a complete user scenario.
3. For corrections, write specific notes in a private UTF-8 file and run `python -m ermik --config config.json request-changes PR --note-file PATH`. This requires a current `review` record and posts a comment marked for the controller. The controller polls pending PRs in bounded batches, so pickup may require several cycles. Work continues in the same branch and PR. Read the new diff, run `review PR` again, and repeat affected manual checks.
4. When accepting, run `python -m ermik --config config.json merge PR` after your decision, a current `review`, and required external checks. It marks the draft ready, performs a squash merge, deletes the branch, and reruns local `checks` on the exact new base commit. Verify `merge_commit`, `main_ci`, and `issue_closed` in the result. A post-merge check failure needs separate handling because the merge has already happened.

`review` and `merge` verify PR identity but do not prove manual criteria or external CI. The code does not implement independent GitHub approval or block merge on unresolved comments; the maintainer handles those decisions.
