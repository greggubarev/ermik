# Release status

Ermik is a preview. The execution host prepares draft pull requests; a maintainer on another installation reviews and explicitly merges them.

## Verified

| Area | Evidence |
| --- | --- |
| State and policy | Unit tests cover admission, path reservations, provider routing, publication, review, and recovery transitions. |
| Demo runner | The Docker host smoke covers a failed check, task execution, correction, and local checks. |
| Container cleanup | A host smoke removes only a stopped run's labelled container. |
| Evidence file writes | Unit tests cover atomic replacement, malformed records, POSIX permissions, and a simulated failure before and after replacement. Interrupted-run recovery blocks a partial evidence file without creating another PR. The Linux suite passed; live process termination remains to be verified. |
| Maintainer review records | Unit tests cover private atomic replacement, failed writes, malformed and oversized records, and legacy permission upgrades. Real process termination during a maintainer decision remains to be verified. |
| Review policy identity | Merge checks the ordered check commands and resolved review image against the saved review before changing the PR. A policy or image change requires a new review. |
| Image use during one attempt | Tests check that the worker and local checks receive one verified image ID, including provider fallback. The Docker host smoke passed a task, correction, and checks by image ID. Binding that ID across later corrections, recovery, and separate-host review remains open in Issue #8. |
| Issue handoff | A private test repository completed admission, draft PR publication, requested changes on the same PR, local review, explicit merge, and a check on the resulting default branch. |
| Provider routes | The two documented subscription routes each completed a live task and returned usage data. |

## Not yet verified

- Recovery from a real process kill in every remote-write window.
- Reboot of the execution host while a task is active.
- Setup on a clean host by an unrelated operator.
- Every compatible provider endpoint and its authentication, quota, and timeout responses.
- More than one controller host sharing a queue or state database; this configuration is unsupported.
- Application correctness beyond each project's configured checks and maintainer review.

Run the [first-project exercise](first-project.md) in a disposable repository before using Ermik on production code. The [security guide](security.md) describes the credential boundary. Usage estimates are observations, not a provider balance.
