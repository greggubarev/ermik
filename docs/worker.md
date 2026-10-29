# Server execution

The `ermik` controller takes open issues labelled `queue:worker` and `state:ready` after the configured maintainer runs `admit` on their own computer. Admission is verified through a GitHub comment by that maintainer, bound to hashes of the issue body and allowed paths. The controller runs `worker_command` from `config.json` in a container, executes local `checks`, pushes a branch, and opens a draft PR. Run one controller per `state_dir`; the local lock does not coordinate independent servers.

## Task input

The issue body must contain the exact headings `## Goal`, `## Allowed paths`, and `## Acceptance`. List ordinary relative paths in `Allowed paths`, one per line without `-` or other list markers. A path may name a file or directory prefix. Check commands come only from `checks` in `config.json`. The worker reads issue text at `/task/issue.md`; its checkout is mounted at `/work`.

Before `run-once` or `poll`, inspect `doctor`, `status`, `worker_command`, `checks`, the container image, GitHub permissions, and the working environment. The controller uses the stored approval for the exact issue body and local reservations for overlapping paths. It does not claim other queues, and labels are not an atomic lock across servers.

## One cycle

1. `python -m ermik --config config.json run-once` performs one bounded cycle; `poll` repeats it on the configured schedule. The controller reserves the issue, moves its label to `state:in-progress`, and creates `work/issue-N-delivery` and a temporary worktree from the current base branch.
2. The worker changes files. The controller rejects an empty result, files outside `Allowed paths`, symbolic links, and non-regular files, then commits the changes. It runs every `check` in a network-disabled container and requires that checks leave the checkout and HEAD unchanged.
3. The controller pushes the branch and creates **one draft PR** containing `Closes #N`. GitHub credentials stay in the trusted controller process and are not passed through `worker_env`. Check `status`, the PR, and its exact HEAD before reporting it ready for review.
4. After `request-changes`, a later bounded PR poll passes the note to the worker, commits changes on the same branch, and updates the same PR. A large pending PR queue can take several cycles. The new HEAD requires a new review and checks.

The worker container cannot merge. A valid configuration does not establish semantic correctness, complete positive and negative coverage, or success of external CI; the maintainer assesses those separately. On failure, local state becomes `blocked` and the controller attempts to apply `state:blocked`. Reconcile `status` with GitHub before retrying. Do not remove state or a worktree to bypass the block.
