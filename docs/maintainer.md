# Maintaining issues and PRs

The maintainer selects issues, admits them to the queue, reviews each draft PR, and decides whether to request changes or merge. The `ermik` controller executes work and opens draft PRs; it does not merge automatically. Start with [setup.md](setup.md) and a disposable repository.

## Prepare an issue

1. Compare the goal with the code, open issues, and PRs. One issue should describe one verifiable result.
2. Create an open issue with `queue:worker` and `state:proposed`. Start from the [issue template](../templates/worker-issue.md). Its body must have exactly the level-two headings `Goal`, `Allowed paths`, and `Acceptance`. Put one relative path or directory prefix per line under `Allowed paths`, **without list markers**. Absolute paths, `..`, backslashes, `*?[]` patterns, and duplicates are rejected.
3. Describe the observable change in `Goal`. In `Acceptance`, state positive and negative cases, manual checks, and scope limits. Local check commands belong in `config.json` under `checks`, not in the issue. Confirm that this configuration's common checks fit the task.
4. Inspect dependencies, resources, path overlap with active jobs, and controller permissions. Run `python -m ermik --config config.json doctor` and `status`. After deciding to admit the issue, run `admit NUMBER` **on the maintainer's computer**, using their GitHub account. It publishes an approval comment bound to hashes of the body and allowed paths, and changes `state:proposed` to `state:ready`. The server reads that approval from GitHub; a shared SQLite database is unnecessary. An issue body edit invalidates admission and requires review and a new admission.

For comparable work, create the repository label `task-set:<slug>` first, then use `admit NUMBER --task-set <slug>`. The slug starts with a lowercase letter and has at most 40 lowercase letters, digits, or hyphens. Admission binds exactly one such Issue label to the maintainer approval and freezes it in the job. A changed or conflicting label blocks execution; a blocked job cannot switch task sets. Existing jobs with no task set remain unknown in outcome reports.

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
   The [PR description template](../templates/pull-request.md) is available for manually prepared PRs. The controller writes its own PR description; this template does not replace the `review` command or its exact-HEAD record.
2. Run `python -m ermik --config config.json review PR`. It creates a separate checkout at the exact HEAD, validates the published diff, repeats configured `checks`, and records local evidence for the current base, HEAD, issue body, ordered check commands, and resolved container image ID. Also run checks outside the common configuration when required. This is local evidence, not proof of GitHub Actions or a complete user scenario.
   Review and post-merge records are private JSON files under the maintainer's `state_dir/reviews`. Updates replace a complete record atomically. If a record is missing, malformed, or unexpectedly large, stop and inspect the exact PR and local state; do not delete the record to bypass the gate.
3. For corrections, write specific notes in a private UTF-8 file and run `python -m ermik --config config.json request-changes PR --note-file PATH`. This requires a current `review` record and posts a comment marked for the controller. The controller polls pending PRs in bounded batches, so pickup may require several cycles. Work continues in the same branch and PR. If a correction fails and the Issue becomes blocked, inspect the controller's blocked receipt on the Issue, then rerun the same command with the current note file. The retry is tied to that receipt and the consumed exact-HEAD request; an absent or uncertain receipt or comment blocks another request. Read the new diff, run `review PR` again, and repeat affected manual checks.
   If the controller has advanced the draft PR HEAD but another PR moves `main` before you can review it, run `python -m ermik --config config.json request-rebase PR`. This sends only a base correction for the exact current HEAD and target base. If `main` advances again before pickup, rerun the command; a confirmed earlier request can be superseded by one for the new target. An uncertain earlier comment must be reconciled first. The command does not record a new review or authorize merge. Inspect the new HEAD and run the ordinary `review PR` after the controller finishes.
4. When accepting, run `python -m ermik --config config.json merge PR` after your decision, a current `review`, and required external checks. A changed check command, command order, or resolved image requires another `review` before merge. A missing or malformed policy record also requires another review. It marks the draft ready, records a merge attempt, performs a squash merge, and reruns local `checks` on the exact new base commit using the reviewed image ID. Verify `status: complete`, `merge_commit`, `main_ci`, and `issue_closed` in the result. An incomplete result exits with code 2. Inspect the PR and default branch. A retry verifies an already merged PR without another merge request; an attempted merge that still appears open stays pending for maintainer inspection.

`review` and `merge` verify PR identity but do not prove manual criteria or external CI. The code does not implement independent GitHub approval or block merge on unresolved comments; the maintainer handles those decisions.

## Compare route outcomes

After making a consistent SQLite backup of the controller's private v5 state and transferring the matching maintainer `decision-trail` directory to one trusted analysis host, run `python -m ermik --config config.json outcomes --maintainer-state-dir PATH --period YYYY-MM`. Point `config.json` at the backed-up controller state and `PATH` at the separate maintainer state directory. The command reads both stores without changing them and emits bounded JSON. Keep the copies private; the report contains repository, Issue and PR identifiers, but no task body, credentials, or local paths.

The period is the UTC month of admission, so attempts for failed jobs and later corrections remain in the original cohort. A PR counts as accepted only when the validated maintainer journal records `merged` for its current HEAD. The report separates observed tokens and estimated cost from unknown usage; billed cost and provider balance remain unknown. Cost per accepted PR is unavailable when any attempt in that route cohort lacks an estimate, when attempt history is missing, or when no PR was accepted. `retry_scheduled_events` and `quota_paused_events` come from the controller event log; correction attempts are counted separately. This report supports comparison; it does not change routing defaults or authorize a merge.

The report sums input, output, reasoning, cache-read, and cache-write tokens across all recorded attempts, including failures and corrections. `cache_read_share` is cache-read divided by input plus cache-read tokens; `output_share` is output divided by reported total tokens. Both fields state their denominators and are `null` if any attempt has missing usage or the denominator is zero. Observed totals remain visible alongside the count of unknown attempts. The [two-route trial](https://github.com/greggubarev/ermik/issues/16#issuecomment-6696904294) used identical file-content checks in separate scoped issues; its tiny sample does not establish a routing preference.
