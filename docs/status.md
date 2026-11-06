# Local status and resource limits

`python3 -m ermik --config config.json status` fetches ready issue numbers from the configured GitHub repository and combines them with a local SQLite snapshot. It requires `gh` authentication. The snapshot does not change state; the command fails if GitHub is unavailable.

`status_snapshot(state_dir, max_parallel=..., ready_issues=...)` can be called without network access. When verified external queue numbers are not supplied, `queue.count` and `queue.ready_issues` are `null`, not zero. Output includes issue and PR numbers, job state and update time, occupied slots, selected and result models, attempt counts, retry time, provider cooldowns, and recent events. Raw errors, task content, branches, SHAs, logs, and local paths are omitted. The job list is capped at 1,000; `jobs_truncated` marks truncation, while aggregate counts cover every row. The limits are 1,000 ready issue numbers, 100 events, and 32 cooldowns.

Events and attempt counters persist in SQLite. `attempts.total` counts retry state records, not every worker invocation ever made; use `jobs[].attempts` for a particular job. For a pending PR, `jobs[].next_review_at` is the next poll time and `next_retry_at` is the next execution retry. `usage` sums token counts and indicative `estimated_cost` by provider and model, and separately counts attempts without complete usage data. These are neither subscription balances nor billed totals. A provider cooldown is a local retry time, not a guarantee that external quota has recovered.

Each job also has bounded `reason_code`, `last_recorded_action`, and `next_action` fields. Confirmed worker authentication exit 77, exhaustion of all attempted provider routes after quota exit 75, and a failed local check command persist distinct reason codes. A blocked job with an unknown cause calls for manual reconciliation of the Issue, branch, and PR; the status command never recommends retrying an ambiguous write. A scheduled retry waits for the controller's reconciliation, including provider-capacity waits. A local `awaiting_review` record with a PR number asks the maintainer to verify the current remote HEAD before review. These fields come from local state only: a PR number is not a live identity check, and no HEAD is displayed without remote verification. Unknown failure causes stay `requires_inspection`; raw errors and provider responses are never classified by matching their text. The optional dashboard displays the same fields through a strict allowlist of reason, status, and action combinations.

`resource_guard(workspace, state_dir=..., min_free_bytes=..., max_worktrees=...)` checks free space under both configured roots and the active checkout count. The controller calls it before claiming a new issue, using `min_free_gb` and `max_parallel` from configuration. Clean, registered worktrees for published or merged jobs remain stored until [explicit bounded cleanup](storage.md); they do not consume an active slot. Interrupted, modified, unregistered, or unexpected-path worktrees remain for investigation and count against the worktree limit. Low free space under either root keeps the new issue queued. This check does not reserve disk space.

`log_window_metadata(path, max_bytes=...)` returns the size, truncation flag, and SHA-256 of up to the last 64 KiB of a private log without exposing its text. Captured container output is limited to 1 MiB; output beyond that limit is not retained in full. Raw check logs remain under the private `state_dir/ci-logs/` directory.

## Optional local dashboard

Run from the source checkout:

```sh
python3 -m ermik --config config.json dashboard --port 8765
```

Open `http://127.0.0.1:8765/`. JSON is available at `/api/status`. The page is read-only and refreshes every 10 seconds. GitHub queue data is fetched at most once every 60 seconds; a read failure returns 503. It shows queue and job states, occupied slots, selected and result routes, attempts, usage observations, cooldowns, and recent events. It does not show a verified remaining subscription balance. Issue text, errors, branches, SHAs, logs, local paths, and secrets are omitted. Responses are bounded and use `Cache-Control: no-store`.

The server binds only to `127.0.0.1` and accepts `Host: 127.0.0.1` or `localhost`. It has no authentication, so use it only on a trusted host; other local processes may reach it. Remote viewing needs a separately configured secure channel. Closing the command stops the page without affecting the controller.
