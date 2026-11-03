# Release status

Ermik is a preview. The execution host prepares draft pull requests; a maintainer on another installation reviews and explicitly merges them.

## Verified

| Area | Evidence |
| --- | --- |
| State and policy | Unit tests cover admission, path reservations, provider routing, publication, review, and recovery transitions. |
| SQLite state upgrade | Six migration tests use populated, synthetic preview state. They cover an idempotent upgrade to schema version 1, preserved admissions, jobs, retry and usage rows and review evidence files, refusal of an unknown newer version, an interrupted transaction, and backup/restore. Run `python3 -m unittest tests.test_state_migrations -v`. The [setup guide](setup.md#stop-update-and-roll-back) gives the stopped-service backup and rollback procedure. |
| Demo runner | The Docker host smoke covers a failed check, task execution, correction, and local checks. |
| Container cleanup | A host smoke removes only a stopped run's labelled container. |
| Artifact retention | The `storage` command inventories controller-owned worktrees, check logs, and publication evidence; cleanup requires a preview, explicit bounds, and a verified merged record. Unit tests cover its eligibility and refusal rules. A live disk-pressure and interrupted-cleanup trial remains open. See [local artifact storage](storage.md). |
| Publication probe recovery | Issue #30 adds an exact repository, ref, and expected SHA intent for a fork capability probe, synced before POST on the documented Linux controller. Restart reconciliation is limited to that recorded ref and the same scoped credential; a changed or unreadable ref blocks automatic cleanup. Unit tests cover the crash windows and a second controller's ref. The [live disposable-fork matrix](../tests/live_probe_matrix.py) stopped the process before POST, after POST, and after DELETE, and simulated lost POST and DELETE responses; replay left no probe refs. Windows power-loss durability of the intent directory is unverified. GitHub's ref DELETE has no SHA precondition, so a concurrent ref update between the final read and DELETE remains a limit. |
| Evidence file writes | Unit tests cover atomic replacement, malformed records, POSIX permissions, and a simulated failure before and after replacement. Interrupted-run recovery blocks a partial evidence file without creating another PR. The Linux suite passed; live process termination remains to be verified. |
| Maintainer review records | Unit tests cover private atomic replacement, failed writes, malformed and oversized records, and legacy permission upgrades. Real process termination during a maintainer decision remains to be verified. |
| Review policy identity | Merge checks the ordered check commands and resolved review image against the saved review before changing the PR. A policy or image change requires a new review. |
| Explicit merge recovery | Unit tests cover a lost merge response, interrupted post-merge verification, a saved incomplete result, wrong PR or commit identity, and refusal to repeat an uncertain merge request. The [live matrix](live-merge-recovery.md) passed eight cases in a private test repository, including four process interruptions and independent REST verification. |
| Image continuity | Unit tests cover durable image binding before the first container, provider fallback, retry, correction, interrupted publication recovery, broker evidence, and maintainer review against the published ID. A changed or missing ID is rejected. The [Linux trial](image-continuity.md) replaced a mutable tag between real Docker checks, then verified rejection and acceptance of the same PR in an independent maintainer installation. The two installations shared a host; physical cross-host evidence delivery remains open. |
| Source release gate | At commit `7d84f5a`, the Linux suite passed 298 tests. The gate scanned that reviewed commit's reachable history, packaged 101 tracked files, verified archive members against Git blobs, and produced a matching SHA-256 checksum and dependency inventory. Negative tests reject historical credentials, unsafe paths, symlinks, and dirty or mismatched checkouts; Git replacement objects cannot change the selected commit. See the [release guide](release.md); built container images require a separate license and content review. |
| Issue handoff | A private test repository completed admission, draft PR publication, requested changes on the same PR, local review, explicit merge, and a check on the resulting default branch. |
| Provider routes | The two documented subscription routes each completed a live task and returned usage data. |

## Not yet verified

- Recovery from a real process kill in every remote-write window.
- Reboot of the execution host while a task is active.
- Live disk-pressure, interrupted artifact cleanup, and repeated cleanup on a real long-running installation.
- Windows ACL ownership parity for artifact retention; Windows path, link, and file-type checks are present, while ownership and permission validation has been verified on POSIX/Linux.
- Setup on a clean host by an unrelated operator.
- A live upgrade and rollback of an existing controller installation; the SQLite migration and backup/restore evidence above uses synthetic preview-shaped state.
- Every compatible provider endpoint and its authentication, quota, and timeout responses.
- More than one controller host sharing a queue or state database; this configuration is unsupported.
- Application correctness beyond each project's configured checks and maintainer review.

Run the [first-project exercise](first-project.md) in a disposable repository before using Ermik on production code. The [security guide](security.md) describes the credential boundary. Usage estimates are observations, not a provider balance.
