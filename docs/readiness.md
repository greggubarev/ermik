# Release status

Ermik is a preview. The execution host prepares draft pull requests; a maintainer on another installation reviews and explicitly merges them.

## Verified

| Area | Evidence |
| --- | --- |
| State and policy | Unit tests cover admission, path reservations, provider routing, publication, review, and recovery transitions. |
| Demo runner | The Docker host smoke covers a failed check, task execution, correction, and local checks. |
| Container cleanup | A host smoke removes only a stopped run's labelled container. |
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
