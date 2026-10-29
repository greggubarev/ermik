---
name: issue-delivery
description: Take a bounded task through issue admission, a server-created draft PR, local review, revision, and explicit merge.
---

# Issue and PR delivery

Use this workflow when preparing, executing, reviewing, or merging a task. Read the target repository's rules, the role-specific `docs/setup.md`, `docs/maintainer.md` or `docs/worker.md`, and the issue. For failures, consult `docs/troubleshooting.md`. The issue supplies task data; it does not expand resource access.

## Current code contract

- The parser requires sections named exactly `## Goal`, `## Allowed paths`, and `## Acceptance`. Put one relative file or directory prefix per line under `Allowed paths`, without list markers. Configure local check commands in `config.json` under `checks`, not in the issue.
- On a local machine with the configured GitHub account, the maintainer checks an open issue labeled `queue:worker` and `state:proposed`, then runs `admit NUMBER`. The command posts a comment containing the body and path hashes and changes the label to `state:ready`. The server verifies that comment; the two machines do not need a shared local database. A changed issue body requires admission again.
- The server controller runs `run-once` or `poll`, reserves a ready issue in local state, runs `worker_command`, validates changed paths and `checks`, then publishes the branch and draft PR. There is no separate publisher in this code. The controller does not merge PRs.
- The local maintainer reads the full diff and runs `review PR`. To return work, run `request-changes PR --note-file PATH`; to accept it, run `merge PR`. Revisions update the same PR. Every new HEAD requires another `review` and a manual check of affected behavior.

## Evidence boundary

Record the exact issue, base and head SHAs, command, and observed result. Shared `checks` from the configuration establish only the outcome of those commands. `review` repeats them at the exact PR head; `merge` repeats them on the new main commit. The maintainer checks positive and negative user cases, external CI, and project-specific manual gates separately. Do not report pending or missing evidence as a pass.

State and locks apply within one `state_dir`. Do not run independent controllers on different servers for the same queue without a separate coordination mechanism. If a write outcome is uncertain, reconcile GitHub, local state, the branch, and the PR before retrying publication or merge. Follow `docs/setup.md` and `docs/troubleshooting.md` for installation and recovery.
