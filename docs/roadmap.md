# Roadmap

Ermik is a preview. Its immediate goal is a verifiable handoff: an admitted, path-scoped issue becomes one draft PR; a maintainer can request a correction on that PR, review its current HEAD, and explicitly merge it. The controller runs configured local checks without requiring GitHub Actions. The [release status](readiness.md) separates completed trials from open validation work, and the [security model](security.md) describes the trusted host and credential boundaries.

Phases below are ordered by evidence needed, not by release date. The v0.2 reliability gates remain open. Portable evidence reached a separate read-scoped installation in v0.3, and a cross-store image review completed its documented trial. v0.4 and v0.5 describe planned work. Completion requires the stated observations in a disposable repository and relevant automated tests. A passing test or check establishes only the behavior it exercises.

The [v0.2.0 milestone](https://github.com/greggubarev/ermik/milestone/1) tracks recovery, credential isolation, image identity, upgrades and release gates. [v0.3.0](https://github.com/greggubarev/ermik/milestone/2) tracks portable evidence and review handoff; [v0.4.0](https://github.com/greggubarev/ermik/milestone/3) tracks measured routing; [v0.5.0](https://github.com/greggubarev/ermik/milestone/4) tracks optional operator experience and adoption trials. Closed Issues remain in their milestone as evidence of completed steps. Listing work here does not mark it complete.

## 0. Verified handoff baseline

**Current evidence:** One documented trial completed admission, task execution, draft PR publication, a correction in the same PR, review of the new HEAD, explicit merge, and a check on the resulting default branch. Host smoke tests cover a failed check and container cleanup. Two configured provider routes each completed a live task. See [release status](readiness.md) and the [first-project exercise](first-project.md).

**Acceptance for another project:** Run the full exercise with that project's image, task command, and checks. Record the target repository, configuration revision, image identifier, issue, PR, reviewed HEAD, merge result, and check results. Confirm that an out-of-scope change and a failing check stop publication. The demonstration worker alone does not establish that arbitrary issues can be completed.

## 1. v0.2: Interruption and retry recovery

**Goal:** Make remote writes recoverable without creating duplicate work or silently advancing an ambiguous task.

**Acceptance:** In a disposable repository, stop the controller at each issue claim, branch push, draft PR creation, correction push, and merge response boundary. Restart it and verify the same issue, branch, and PR identities are reconciled before any retry. Repeated commands must not create a second PR, repeat a merge, or treat an unverified HEAD as reviewed. A changed issue body, base commit, or PR HEAD must block the stale decision. Run the same exercise across an execution-host reboot while a task is active.

[Execution recovery](https://github.com/greggubarev/ermik/issues/1), [explicit merge recovery](https://github.com/greggubarev/ermik/issues/27), [atomic review records](https://github.com/greggubarev/ermik/issues/29), and [orphan probe cleanup](https://github.com/greggubarev/ermik/issues/30) separate the write boundaries so each can be tested without claiming the others are complete.

## 2. v0.2: Clean-host operation and credential separation

**Goal:** Let an operator reproduce the workflow on a fresh host with an auditable publication boundary.

**Acceptance:** An operator who did not prepare the development host follows [setup](setup.md) on a clean supported Ubuntu host, runs preflight through the service environment, completes the [first-project exercise](first-project.md), and records any missing steps. In fork mode, demonstrate a successful fork push and draft PR while both server credentials fail to push to a disposable upstream branch. Confirm the task container cannot read GitHub credentials or the Docker socket. Review and correction remain tied to the exact PR HEAD; only the maintainer's explicit command initiates merge. This validation does not make the trusted controller or Docker host an untrusted boundary.

## 3. v0.2: Release and upgrade discipline

**Goal:** Make a reviewed release reproducible and recoverable without losing queue state.

**Acceptance:** Pin and inspect the task and check image by immutable identifier. Upgrade a populated local SQLite state through a documented migration, restart, and verify that pending and completed jobs retain their identities; test rollback from a saved state and reconcile remote writes before resuming. Publish a vulnerability-reporting path and check the source release for credentials, local state, and required third-party notices. Repeat the complete handoff on the release candidate after upgrade.

[Image continuity](https://github.com/greggubarev/ermik/issues/8), [review check-policy identity](https://github.com/greggubarev/ermik/issues/28), [state upgrades](https://github.com/greggubarev/ermik/issues/10), and [owned artifact retention](https://github.com/greggubarev/ermik/issues/31) are separate gates. The [image-continuity trial](image-continuity.md) covers correction and recovery in unit tests, real Docker tag replacement, and review through an independent maintainer installation. Physical cross-host delivery belongs to v0.3.

## 4. v0.3: Portable evidence and reviewer decision support

**Goal:** Let a maintainer understand and preserve the basis for a decision without relying on one controller's local SQLite state. [Portable evidence](https://github.com/greggubarev/ermik/issues/11), [cross-host delivery](https://github.com/greggubarev/ermik/issues/32), and a [review packet](https://github.com/greggubarev/ermik/issues/15) track the handoff; [version fixtures](https://github.com/greggubarev/ermik/issues/33) and a [decision trail](https://github.com/greggubarev/ermik/issues/34) protect it across upgrades and corrections.

**Implemented slices:** Version 2 export and verification, optional PR-comment delivery, a read-only review packet, protocol fixtures, and exact-HEAD decision history have passed the documented trials in [release status](readiness.md). A separate receiver configuration used a read-scoped credential to fetch two HEAD records and reject the stale first record, completing [#32](https://github.com/greggubarev/ermik/issues/32). A later [cross-store trial](image-continuity.md#independent-docker-stores) accepted matching image config digests despite different local IDs and rejected a changed config with the same layers, completing [#69](https://github.com/greggubarev/ermik/issues/69).

**Acceptance:** Export a versioned, portable evidence record for one issue and PR that identifies the repository, admission and allowed-path hashes, base and reviewed HEAD commits, changed paths, check commands and outcomes, image identifier, and private-log digests. Verify the exported record against the current GitHub issue and PR from a separate maintainer installation with restricted read access. Present the exact diff and check evidence with a clear stale or missing-evidence state; a changed HEAD or base must invalidate the prior review decision. The record must contain no credentials, private logs, or full issue body. The maintainer still decides whether to request changes or merge.

## 5. v0.4: Measured routing and cost per accepted PR (planned)

**Goal:** Compare configured routes using observed outcomes and resource use over complete PR cycles.

The [outcome ledger](https://github.com/greggubarev/ermik/issues/16) must precede any [measured routing policy](https://github.com/greggubarev/ermik/issues/17). [Quota observations](https://github.com/greggubarev/ermik/issues/35) and [context-efficiency measures](https://github.com/greggubarev/ermik/issues/36) must distinguish verified counters from estimates and missing data.

**Acceptance:** Record attempts, retries, rate limits, elapsed time, usage observations, and accepted PR outcomes against issue and PR identities without mixing separate tasks. Report cost per accepted PR for a stated period and route, including failed and retried attempts in the numerator; report the measure as unavailable when no PR was accepted. Separate estimated usage or cost from billed amounts and show missing data explicitly. Compare at least two configured routes on the same documented task set, including a failure and a retry, before using the results to change routing defaults. A routing choice must not bypass admission, scope, checks, or maintainer review.

## 6. v0.5: Optional user experience (planned)

**Goal:** Make queue state and review evidence easier to inspect while retaining the existing command workflow.

The [optional maintainer view](https://github.com/greggubarev/ermik/issues/18) depends on the evidence and review packet. [Actionable diagnostics](https://github.com/greggubarev/ermik/issues/37), [two non-demo integrations](https://github.com/greggubarev/ermik/issues/38), and [opt-in event digests](https://github.com/greggubarev/ermik/issues/39) extend the CLI and adoption path without moving the merge decision to the execution host.

**Acceptance:** Provide an optional interface that shows each issue's verified identity, current state, PR and HEAD, check result, and next available maintainer action from the same underlying records as the commands. In a trial, compare the interface and command output for an admitted task, blocked task, correction, and stale review; they must agree. An unavailable interface must leave admission, review, and explicit merge usable through the documented commands. No interface action may weaken the checks or grant the execution host merge authority.

## Outside the current claim

Multiple controller hosts sharing one queue or state database are unsupported. Project correctness beyond configured checks and maintainer review is unverified. Provider authentication, quota, and timeout behavior has not been exercised for every compatible endpoint. Comparative cost reporting and the optional maintainer view remain open work. The cross-store image trial covers the tested Docker versions and image, not every image format. These are explicit limits of the preview, not implied promises in the phases above.
