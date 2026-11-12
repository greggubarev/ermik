# Roadmap

Ermik is a preview. Its immediate goal is a verifiable handoff: an admitted, path-scoped issue becomes one draft PR; a maintainer can request a correction on that PR, review its current HEAD, and explicitly merge it. The controller runs configured local checks without requiring GitHub Actions. The [release status](readiness.md) separates completed trials from open validation work, and the [security model](security.md) describes the trusted host and credential boundaries.

Phases below are ordered by evidence needed, not by release date. The v0.2 through v0.5 previews are published, but preview limitations remain. Portable evidence reached a separate read-scoped installation in v0.3, a cross-store image review completed its documented trial, and two non-demo project stacks completed the v0.5 handoff. Completion requires the stated observations in a disposable repository and relevant automated tests. A passing test or check establishes only the behavior it exercises.

The [v0.2.0 milestone](https://github.com/greggubarev/ermik/milestone/1) tracks recovery, credential isolation, image identity, upgrades and release gates. [v0.3.0](https://github.com/greggubarev/ermik/milestone/2) tracks portable evidence, review handoff, and actionable recovery; [v0.4.0](https://github.com/greggubarev/ermik/milestone/3) tracks measured routing; [v0.5.0](https://github.com/greggubarev/ermik/milestone/4) tracks optional operator experience and adoption trials. Closed Issues remain in their milestone as evidence of completed steps. Listing work here does not mark it complete.

## v0.2 preview gates

[#5](https://github.com/greggubarev/ermik/issues/5) completed its clean-host installation trial. In the [#7 reboot trial](https://github.com/greggubarev/ermik/issues/7#issuecomment-6643766138), a disposable worker and broker service were active before reboot; both services restarted, the broker socket returned, installation-owned container cleanup preserved an unrelated container, and SQLite and the checkout survived. The job ended in a specific blocked state with no PR. The trial did not interrupt an in-flight broker PR POST.

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

**Goal:** Let a maintainer understand and preserve the basis for a decision without relying on one controller's local SQLite state. [Portable evidence](https://github.com/greggubarev/ermik/issues/11), [cross-host delivery](https://github.com/greggubarev/ermik/issues/32), and a [review packet](https://github.com/greggubarev/ermik/issues/15) track the handoff; [version fixtures](https://github.com/greggubarev/ermik/issues/33) and a [decision trail](https://github.com/greggubarev/ermik/issues/34) protect it across upgrades and corrections. [Actionable recovery](https://github.com/greggubarev/ermik/issues/37) is the distinct post-v0.2 outcome required before this version can ship.

**Implemented slices:** Version 2 export and verification, optional PR-comment delivery, a read-only review packet, protocol fixtures, and exact-HEAD decision history have passed the documented trials in [release status](readiness.md). A separate receiver configuration used a read-scoped credential to fetch two HEAD records and reject the stale first record, completing [#32](https://github.com/greggubarev/ermik/issues/32). A later [cross-store trial](image-continuity.md#independent-docker-stores) accepted matching image config digests despite different local IDs and rejected a changed config with the same layers, completing [#69](https://github.com/greggubarev/ermik/issues/69).

**Acceptance:** Export a versioned, portable evidence record for one issue and PR that identifies the repository, admission and allowed-path hashes, base and reviewed HEAD commits, changed paths, check commands and outcomes, image identifier, and private-log digests. Verify the exported record against the current GitHub issue and PR from a separate maintainer installation with restricted read access. Present the exact diff and check evidence with a clear stale or missing-evidence state; a changed HEAD or base must invalidate the prior review decision. The record must contain no credentials, private logs, or full issue body. The maintainer still decides whether to request changes or merge.

[#37](https://github.com/greggubarev/ermik/issues/37) adds distinct guidance in the CLI, read-only local status page, and maintainer review packet for a lost publication response, unavailable publication credential, stale review, failed check, provider authentication failure, and quota cooldown. An ambiguous write requires remote reconciliation, never an unverified retry. The optional full maintainer queue view remains separate [#18](https://github.com/greggubarev/ermik/issues/18) work for v0.5.

## 5. v0.4: Measured routing and cost per accepted PR (preview released)

**Goal:** Compare configured routes using observed outcomes and resource use over complete PR cycles.

The [outcome ledger](https://github.com/greggubarev/ermik/issues/16) links two live provider routes through draft PRs, separate review, explicit merge, and a bounded report. [Context-efficiency measures](https://github.com/greggubarev/ermik/issues/36) add aggregate cache and output shares without exposing session content. The [measured routing policy](https://github.com/greggubarev/ermik/issues/17) and [quota observations](https://github.com/greggubarev/ermik/issues/35) passed separate gates; the small two-route outcome sample does not set a routing default.

The [provider failure matrix](https://github.com/greggubarev/ermik/issues/6) covers one successful live file task per advertised route, controlled authentication, quota, timeout, and malformed-response failures for each, controller cooldown/slot/retry tests, and a remote blocked Issue with a redacted comment. Provider-origin 403/429/timeout/malformed responses and a full live cooldown/retry cycle remain unobserved; route failure handling stays experimental.

**Acceptance:** Record attempts, retries, rate limits, elapsed time, usage observations, and accepted PR outcomes against issue and PR identities without mixing separate tasks. Report cost per accepted PR for a stated period and route, including failed and retried attempts in the numerator; report the measure as unavailable when no PR was accepted. Separate estimated usage or cost from billed amounts and show missing data explicitly. Compare at least two configured routes on the same documented task set, including a failure and a retry, before using the results to change routing defaults. A routing choice must not bypass admission, scope, checks, or maintainer review.

## 6. v0.5: Optional user experience (preview released)

**Goal:** Make queue state and review evidence easier to inspect while retaining the existing command workflow.

The [optional maintainer view](https://github.com/greggubarev/ermik/issues/18) depends on the evidence and review packet. [Two non-demo integrations](https://github.com/greggubarev/ermik/issues/38) and [opt-in event digests](https://github.com/greggubarev/ermik/issues/39) extend adoption without moving the merge decision to the execution host.

**Acceptance:** Provide an optional interface that shows each issue's verified identity, current state, PR and HEAD, check result, and next available maintainer action from the same underlying records as the commands. In a trial, compare the interface and command output for an admitted task, blocked task, correction, and stale review; they must agree. An unavailable interface must leave admission, review, and explicit merge usable through the documented commands. No interface action may weaken the checks or grant the execution host merge authority.

## 7. v0.6: Approved task complexity and safe recovery (preview released)

**Goal:** Bind a maintainer's explicit effort approval to the Issue-to-PR lifecycle and recover eligible blocked work without replaying uncertain publication.

Sequence: [#106](https://github.com/greggubarev/ermik/issues/106) adds optional, validated manual complexity profiles bound at admission; claim, correction, and fallback remain within the approved profile. Then [#109](https://github.com/greggubarev/ermik/issues/109) reconciles issue, scope, local worktree, remote branch, and uncertain writes before re-admitting a blocked pre-publication job. Uncertain or changed state blocks with a specific next action. Both retain maintainer approval and explicit merge.

This milestone binds an explicit approval to the auditable Issue-to-PR handoff and its recovery state; it does not claim effort selection itself as unique. Desktop fleet control and generic orchestration are outside scope. Automatic complexity classification or effort calibration should be considered only after comparable accepted-PR and cost evidence exists.

**Acceptance:** Verify allowed and exhausted profile routing, changed-approval rejection, and legacy behavior. For recovery, verify one safely resumed disposable job and one changed-branch rejection after restart, with no duplicate PR, replayed execution, or discarded work. Do not infer automatic classification or cost/quality calibration from manually approved tiers.

The two milestone Issues are closed. [#106](https://github.com/greggubarev/ermik/issues/106) passed an isolated state upgrade and an approved Issue-to-draft-PR trial with rejection of an old approval. [#109](https://github.com/greggubarev/ermik/issues/109) passed a current-source restart and re-admission trial that published one draft PR, plus a changed-branch refusal. These trials cover provably unstarted jobs; started work and uncertain publication remain blocked. The [v0.6 preview](https://github.com/greggubarev/ermik/releases/tag/v0.6.0-preview.1) passed the source, Windows, Linux, and Docker release gates on one commit.

## Next releases

The 19 scoped Issues in the four milestones below provide a work queue, not a promise to tag four versions. Each Issue names a successful and a refusal case. Trials establish evidence; they count as a new release outcome only if they produce an accepted behavior or concrete fix after the previous tag. Keep incomplete acceptance open and record observed limits in [release status](readiness.md).

| Milestone | Product outcome | Ordered work | Release boundary |
| --- | --- | --- | --- |
| [v0.7.0](https://github.com/greggubarev/ermik/milestone/6) | Recover one proven-safe started job without replay. | [#126](https://github.com/greggubarev/ermik/issues/126) defines and implements the boundary. [#127](https://github.com/greggubarev/ermik/issues/127) verifies a populated stopped-service upgrade; [#128](https://github.com/greggubarev/ermik/issues/128) interrupts broker PR creation through a host reboot. [#129](https://github.com/greggubarev/ermik/issues/129) covers live storage pressure, [#130](https://github.com/greggubarev/ermik/issues/130) Windows cleanup ownership, and [#131](https://github.com/greggubarev/ermik/issues/131) a killed maintainer record write. | Ambiguous local or remote identity stays blocked. An upgrade or reboot trial alone cannot justify a tag. |
| [v0.8.0](https://github.com/greggubarev/ermik/milestone/7) | Make exact-HEAD review bounded and legible across installations. | [#132](https://github.com/greggubarev/ermik/issues/132) retrieves evidence beyond long PR timelines; [#133](https://github.com/greggubarev/ermik/issues/133) profiles and bounds one-PR packet latency. [#134](https://github.com/greggubarev/ermik/issues/134) exercises independent hosts and scoped credentials; [#135](https://github.com/greggubarev/ermik/issues/135) distinguishes publisher claims from maintainer checks. | Missing, stale, or partial evidence cannot appear current or authorize merge. |
| [v0.9.0](https://github.com/greggubarev/ermik/milestone/8) | Route with observed failures and comparable accepted-PR results. | [#136](https://github.com/greggubarev/ermik/issues/136) observes provider-origin failures; [#137](https://github.com/greggubarev/ermik/issues/137) verifies cooldown/retry through restart. [#138](https://github.com/greggubarev/ermik/issues/138) builds a comparable cohort; only then [#139](https://github.com/greggubarev/ermik/issues/139) may offer a read-only effort recommendation and [#140](https://github.com/greggubarev/ermik/issues/140) may audit a route-order change. | Sparse or missing evidence means no recommendation or default change. Manual admission and explicit merge remain. |
| [v1.0.0](https://github.com/greggubarev/ermik/milestone/9) | Establish a supported first-project and preview-upgrade contract. | [#141](https://github.com/greggubarev/ermik/issues/141) first determines whether onboarding failures justify a read-only `doctor` extension. [#142](https://github.com/greggubarev/ermik/issues/142) trials a third non-demo project with separate roles; [#143](https://github.com/greggubarev/ermik/issues/143) verifies upgrade from a published preview; [#144](https://github.com/greggubarev/ermik/issues/144) qualifies one exact candidate commit. | No stable tag without a distinct post-v0.9 behavior or fix, same-SHA release gates, and explicit supported limits. |

Start with #126 and #127; #128 depends on #127. #132 can proceed separately, then #133. #134 depends on #128 and precedes #135, #141, and #142. #136 begins the provider track; #137 and #138 follow it, then #139 and #140 follow #138. #143 follows #127; #144 follows #137, #142, and #143. Other Issues are independent within their stated scope. Dependencies are also recorded on each Issue.

The preflight decision in #141 stays evidence-led. If the two existing integrations and one new installation do not reveal repeated missing image, checks, or reviewer instructions, close it with that finding and omit the feature. Do not build a second policy engine, general workspace, or multi-controller queue to fill a milestone. The release qualification rule in `AGENTS.md` applies even when every milestone Issue closes.

## Outside the current claim

Multiple controller hosts sharing one queue or state database are unsupported. Project correctness beyond configured checks and maintainer review is unverified. Provider authentication, quota, and timeout behavior has not been exercised for every compatible endpoint. Automated policy choices and official quota observations remain open work. The optional maintainer view is available, but a live one-PR read currently takes about 20–26 seconds. The cross-store image trial covers the tested Docker versions and image, not every image format. These are explicit limits of the preview, not implied promises in the phases above.
