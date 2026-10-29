# Troubleshooting stopped work

First identify the issue number, saved base and HEAD SHAs, branch, PR, and last confirmed action. Reconcile logs with GitHub and the checkout. An unknown write result is neither confirmed success nor confirmed failure. Do not post tokens, complete configuration files, or sensitive logs in issues or PRs.

| Situation | Action |
| --- | --- |
| No `queue:worker` + `state:ready` issue | Report an empty queue; the maintainer prepares the next issue. |
| Missing `Goal`, `Allowed paths`, or `Acceptance`, or no maintainer admission comment | Correct the body and rerun `admit` under the configured maintainer account. Keep `state:proposed` until readmission. |
| Two processes claim an issue or overlapping files | Pause new claims and inspect saved reservations and comments. Use one dispatcher without a shared lock. |
| GitHub response lost after a label, comment, or publication write | Read remote state and exact SHAs and PR. Retry only when the previous result is proven absent. A timeout must not create a second PR. |
| Network or provider failure before claim | Pause the queue and retry reads within limits. Do not block an arbitrary issue for a general outage. |
| Insufficient space, test environment, or access | Stop new execution and record the needed resource and actual state. Do not remove another checkout or substitute a test environment. |
| Check failure | Preserve the command, environment, and failure. Fix within issue scope and repeat affected checks. Do not call an unverified result passing. |
| CI missing, pending, or out of minutes | Report its actual status. Local checks are separate evidence for the exact HEAD under project rules; they do not replace required manual scenarios. |
| Maintainer requested changes | Continue the same issue, branch, and PR. Recheck the diff, CI, and comments after the new HEAD. |
| Issue closed while a local slot remains occupied | Compare exact issue, PR, HEAD, merge commit, and run log. Release the slot only through the recovery procedure; do not edit state manually. |
| Base branch, scope, or PR changed | Pause publication or merge, obtain new admission, and repeat checks on current base and HEAD. |

For each manual recovery, record initial and final states, verified identifiers, and the next responsible person. Commands depend on the installed implementation; this guide does not imply the existence of a dispatcher, broker, publisher, or automatic recovery step.
