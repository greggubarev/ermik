# Troubleshooting stopped work

First identify the issue number, saved base and HEAD SHAs, branch, PR, and last confirmed action. Reconcile logs with GitHub and the checkout. An unknown write result is neither confirmed success nor confirmed failure. Do not post tokens, complete configuration files, or sensitive logs in issues or PRs.

| Situation | Action |
| --- | --- |
| No `queue:worker` + `state:ready` issue | Report an empty queue; the maintainer prepares the next issue. |
| Missing `Goal`, `Allowed paths`, or `Acceptance`, or no maintainer admission comment | Correct the body and rerun `admit` under the configured maintainer account. Keep `state:proposed` until readmission. |
| Two processes claim an issue or overlapping files | Pause new claims and inspect saved reservations and comments. Use one dispatcher without a shared lock. |
| GitHub response lost after a label, comment, or publication write | Read remote state and exact SHAs and PR. Retry only when the previous result is proven absent. A timeout must not create a second PR. |
| `broker_unavailable` before a PR request was sent | Inspect the broker service, socket, and private broker diagnostics manually. A missing socket alone does not prove a missing token. |
| `publication_unconfirmed` after a PR request was attempted | Preserve state and inspect the exact branch and PR remotely. Let controller reconciliation verify identity before any further publication; never retry the write blindly. A partial send has this outcome too. |
| `publish_credential_unavailable` before fork push | Verify the configured fork credential in the controller environment and rerun the publication capability checks after restoring it. |
| GitHub read request returns a transient server or transport error | Read-only REST calls make at most three short attempts, then stop. A rate-limit response does not trigger rapid retries; wait for the limit to reset before trying again. GitHub writes are never retried by this mechanism. |
| Network or provider failure before claim | Pause the queue and retry reads within limits. Do not block an arbitrary issue for a general outage. |
| Insufficient space, test environment, or access | Stop new execution and record the needed resource and actual state. Do not remove another checkout or substitute a test environment. |
| Check failure | Preserve the command, environment, and failure. Fix within issue scope and repeat affected checks. Do not call an unverified result passing. |
| CI missing, pending, or out of minutes | Report its actual status. Local checks are separate evidence for the exact HEAD under project rules; they do not replace required manual scenarios. |
| Maintainer requested changes | Continue the same issue, branch, and PR. Recheck the diff, CI, and comments after the new HEAD. |
| Issue closed while a local slot remains occupied | Compare exact issue, PR, HEAD, merge commit, and run log. Release the slot only through the recovery procedure; do not edit state manually. |
| Base branch, scope, or PR changed | Pause publication or merge, obtain new admission, and repeat checks on current base and HEAD. |
| Publication capability probe was interrupted | Stop the controller and preserve its private `state_dir/probe-intents` records. Use the recorded repository, exact ref and expected SHA with the same scoped credential. A restart reconciles only those records; a changed or unreadable ref, changed credential, invalid or stale record blocks publication for operator inspection. Never delete a changed ref, an unknown ref, or a task branch. |

`doctor` is read-only and reports `publish_write_capabilities: unverified`. In fork mode, `run-once` and `poll` save each probe's exact repository, generated `refs/heads/ermik-access-probe-*` ref, expected SHA, creation time and credential fingerprint in a private `state_dir/probe-intents` file before the remote POST. The file contains no token. After a hard stop, retain that state directory and the original scoped credential. On the next `run-once` or `poll` cycle, recovery reads the recorded ref. If it is absent and the credential can still read the exact repository and its default branch ref, recovery removes the local intent. If it matches the expected SHA, recovery deletes that ref and then removes the intent. An unreadable, changed, or unconfirmed ref remains for investigation and blocks the cycle. Recovery does not search for similarly named refs or target `main` for deletion.

For manual inspection, stop the controller and read only the relevant private intent file. Confirm the configured repository, ref, SHA and credential before reading that exact ref with `gh api repos/OWNER/REPO/git/ref/heads/BRANCH` under the same scoped credential. Treat a 404 as unresolved until that credential can also read the exact repository and default branch ref. Do not remove the local intent merely to restart work. If the ref has the expected SHA and is unused, restore the original credential and let the controller reconcile it on restart. If the ref differs, cannot be read, or the intent is invalid or older than seven days, leave it and investigate its owner and remote state before any manual change. Never use a prefix listing as proof that a ref belongs to this controller.

For each manual recovery, record initial and final states, verified identifiers, and the next responsible person. Follow the recovery behavior documented for the installed version; do not infer additional automatic steps from this guide.
