# Local artifact storage

The controller keeps task artifacts in its private `state_dir` and `workspace`. Use the `storage` command to measure them without contacting GitHub or changing a job:

```sh
python3 -m ermik --config config.json storage
```

The JSON inventory reports byte and age totals for controller-owned task worktrees, check logs, and publication evidence. It also reports whether their total crosses `artifact_warn_mb`. The default warning threshold is 1024 MiB; set `"artifact_warn_mb": 0` to disable the warning. `"retention_days": 30` is the default minimum age for cleanup. Both are optional settings in `config.json`. The warning is an observation, not a reason to delete an active artifact. The `min_free_gb` guard refuses to claim new work when either `workspace` or `state_dir` is low on free space, including when they use separate filesystems.

## Preview and apply cleanup

The inventory command above is read-only and can run while polling is active. Before either cleanup preview or application, stop only this installation's poll service; cleanup takes the exclusive state lock. Review the result, then restart that service.

Preview the eligible set before removing anything. This step is recommended; the command does not require a prior preview before `--apply`:

```sh
python3 -m ermik --config config.json storage --cleanup
```

Apply the same policy with explicit limits on the number of items and total bytes removed in one invocation:

```sh
python3 -m ermik --config config.json storage --cleanup --apply --max-items 10 --max-bytes 104857600
```

The command requires an exact local job identity, a verified merged Issue/PR/HEAD, and an artifact older than `retention_days`. Logs and evidence from an earlier correction HEAD additionally require the saved publication identity and that commit in the merged PR's remote commit list; an uncertain or incomplete response preserves them. It refuses symlinks and paths outside the configured roots. POSIX checks ownership and permissions. Windows requires the current user to own each path, rejects reparse points and third-party write grants, and refuses cleanup when ACL ownership cannot be established. It preserves active, waiting, blocked, dirty, unknown, and unpushed work. An age threshold alone never authorizes deletion. Cleanup is bounded and can be repeated after another inventory. Inspect skipped items before retrying after an interruption.

The cleanup scope is limited to owned task worktrees under `workspace/jobs`, check logs under `state_dir/ci-logs`, and published evidence under `state_dir/evidence`. It does not delete SQLite state, admissions, retry and usage records, the source mirror, task instructions, maintainer review records, probe intents, backups, or the separate shared `draft_pr_evidence_dir` used by a PR broker. Keep the configured `state_dir` across restarts and upgrades. Do not remove private state by hand to bypass a blocked job or a disk guard.

The inventory and cleanup operate only on the installation named by `config.json`. A second project or maintainer installation with its own roots has its own records and retention decision. If disk pressure persists after eligible cleanup, stop new claims, inspect the reported totals and free space, and expand storage or resolve the blocked or unverified work through the normal recovery procedure.
