# OpenCode execution

The controller selects a model for each attempt. It passes `ERMIK_MODEL_REF`, the public `ERMIK_MODEL_SPEC`, and only the selected provider's key variable to the container. Key values are not stored in controller JSON, SQLite, command arguments, or logs. Inside the disposable container, the key temporarily resides in a private `0600` OpenCode configuration file, removed after the attempt. The container prints one outcome code: `WORKER_OK`, `WORKER_QUOTA_EXHAUSTED`, `WORKER_AUTH_FAILURE`, `WORKER_EXECUTION_ERROR`, or `WORKER_CONFIGURATION_ERROR`. A `WORKER_USAGE` line contains aggregate token data, without session text or keys. If session export is unavailable, `complete=false` and all numeric values are `null`.

## Optional approved complexity profiles

The maintainer can assign an explicit easy, medium, or hard tier at admission. This policy is optional and lives in trusted local configuration. Each model used by a profile declares the reasoning efforts checked for that provider and model:

```json
{
  "models": [
    {"provider_id": "kimi-code", "model_id": "k3-256k", "credential_env": "KIMI_CODE_API_KEY", "supported_efforts": ["low", "high"]},
    {"provider_id": "opencode-go", "model_id": "kimi-k3", "credential_env": "OPENCODE_GO_API_KEY", "supported_efforts": ["max"]}
  ],
  "complexity_profiles": {
    "easy": [{"model_ref": "kimi-code/k3-256k", "effort": "low"}],
    "medium": [{"model_ref": "kimi-code/k3-256k", "effort": "high"}],
    "hard": [{"model_ref": "opencode-go/kimi-k3", "effort": "max"}]
  }
}
```

The example shows the format. In pinned OpenCode 1.18.35, the catalog for `opencode-go/kimi-k3` advertises `max`, but not `high`; a bounded subscription request with `--variant max` completed with HTTP 200. That confirms request acceptance, not how much internal reasoning the provider performed or a hard cost limit. Confirm each selected model and variant against the pinned worker and provider before using another combination. The worker defines the selected variant in its temporary configuration and passes it using `--variant`; syntax validation alone does not prove effective reasoning effort. The provider budget guard remains an estimate.

Create the `complexity:easy`, `complexity:medium`, and `complexity:hard` labels in the target repository before enabling this policy. With profiles enabled, run `admit NUMBER --complexity easy|medium|hard`. Approval binds the issue body and scope hashes, current route label, tier, and effective profile digest. The controller checks those values at claim, correction, and before publication. For a blocked draft PR, a changed route or profile requires a new admission followed by a fresh exact-HEAD `request-changes` comment; the PR and HEAD remain unchanged until that correction runs. Re-admission requires the same allowed paths. A blocked pre-publication job has no reviewed PR boundary and requires separate operator recovery. Issue text and worker output cannot choose the tier. Quota fallback stays inside the approved profile; exhausted candidates leave the job waiting or blocked. Installations without `complexity_profiles` retain the prior routing and admission format.

## Image and command

Build the image pinned to OpenCode 1.18.35:

```sh
docker build -f examples/Dockerfile.opencode -t ermik-opencode:1.18.35 .
```

Configure that `image` and `worker_command: ["python3", "/opt/worker/opencode-worker.py"]`. The standard run provides `/work` and `/task/issue.md`. OpenCode runs as `opencode run --model PROVIDER/MODEL --agent build --format json`. Verify the CLI version and configuration format together: this image uses OpenCode 1.18.35 and its `provider`, `options`, and `permission` fields.

Configure candidates locally:

```json
"models": [
  {"provider_id": "kimi-code", "model_id": "k3-256k", "credential_env": "KIMI_CODE_API_KEY"},
  {"provider_id": "opencode-go", "model_id": "kimi-k3", "credential_env": "OPENCODE_GO_API_KEY"}
]
```

List order is priority order. Separate models of one provider use separate entries but share that provider's slot and cooldown. The limit is 32 entries; combined capacity across providers must cover `max_parallel`. To pin issue labels to configured accounts:

```json
"routes": {"route:primary": "kimi-code", "route:reserve": "opencode-go"}
```

An issue with one of these labels uses only that account. If it is cooling down, the controller may take another independent issue. `route:auto` or no route label uses priority order and failover after confirmed quota exhaustion. Unknown or conflicting route labels block execution. Label mappings are project configuration, not hard-coded policy.

Additional independent OpenAI-compatible accounts can be configured without code changes:

```json
{"provider_id":"free-account-1","model_id":"coder-small","credential_env":"FREE_ONE_API_KEY","base_url":"https://api.example.net/v1","package":"@ai-sdk/openai-compatible"}
```

Give every account a distinct `provider_id` and key variable ending in `_API_KEY`, `_ACCESS_KEY`, or `_TOKEN`. Ten such entries are supported; slots and 429 cooldowns are keyed by `provider_id`. Custom entries may not use variable names resembling GitHub, SSH, service, or built-in OpenCode credentials. Only public HTTPS endpoints and `@ai-sdk/openai-compatible` or `@ai-sdk/openai` are allowed. The latter supports the Responses API; verify each provider and model's compatibility. Never put the key value in configuration.

The controller serializes the selected candidate with `model_spec(candidate)` and passes `--env ERMIK_MODEL_SPEC=<JSON>` alongside `ERMIK_MODEL_REF`. The specification excludes the key value. The container validates the entry and model again. Legacy Kimi/Go configuration may run with only `ERMIK_MODEL_REF`.

OpenCode Go access depends on the subscription and permitted models; verify model identifiers and availability before running. Kimi Code uses `https://api.kimi.ai/coding/v1`; OpenCode Go uses `https://opencode.ai/zen/go/v1`. They have separate identifiers and keys. Keep keys in `0600` files outside the repository and load them into the service environment as shown in [setup](setup.md). Do not pass both keys to one container. `worker_env` should contain only permitted provider variable names; the selected key is passed per attempt. The worker needs no GitHub credential.

## Routing and failure handling

`claim_model` reserves a SQLite slot. By default each provider has one concurrent slot, so simultaneous tasks can use different accounts. `release_model` frees it after the attempt. `provider_cooldowns` stores the next retry time for an entire account after confirmed HTTP 429. The default cooldown is 15 minutes; `provider_policies` can set it per account. Before trying another model, the controller resets the worktree to HEAD and removes untracked files. If none is available, the job stays in `retry_wait` until its local cooldown. Check external quota and recovery time in the provider account; they are not fixed here.

Optional `provider_policies` can set `estimated_budget_ceiling`, `attempt_reservation`, and `cooldown_seconds` for a configured provider. The first two values use the same estimated cost units emitted by the worker. A claim atomically reserves one attempt against current UTC-month observed estimates and active reservations. Missing estimates fail closed. This is a local scheduling guard, not a subscription balance or a hard billing cap: the actual attempt can cost more than its reservation, and a provider can bill differently. `max_parallel` on each model is the per-provider concurrency limit; all models sharing a provider must use the same value.

```json
"provider_policies": {
  "opencode-go": {"estimated_budget_ceiling": 10.0, "attempt_reservation": 0.5, "cooldown_seconds": 900}
}
```

To enable outcome-based ordering, make a consistent copy of the controller state and transfer the matching maintainer decision trail to the same trusted analysis host as described in [maintainer reporting](maintainer.md). Point the analysis config at the copied controller state, then run `python3 -m ermik --config config.json route-metrics --maintainer-state-dir /private/maintainer-state --output /private/quality.json`. Transfer only the resulting snapshot to the controller through a trusted channel. Set `quality_metrics_file` to its absolute path, `quality_min_samples` (default 20), and `quality_max_age_seconds` (default 86400). The file must be owned by the controller and mode `0600` on Linux. Refresh it by writing to a new private path and atomically renaming it over the configured path. Each provider needs at least the minimum number of confirmed, single-provider merged PRs with complete attempt histories. A missing, stale, malformed, or incomplete snapshot retains configured priority order. This metric measures first-review acceptance among accepted PRs; it does not prove one model is better on every task. A route-decision event records the policy version, eligible model references, selected reference, and bounded reason, without issue text or credentials.

Quota observations are separate from routing. To read a running local Kimi Code or OpenCode server, set optional loopback-only sources in the controller config:

```json
"quota_sources": {
  "kimi-code": {"kind": "kimi-code", "port": 58627, "auth_env": "KIMI_LOCAL_SERVER_TOKEN"},
  "opencode-go": {"kind": "opencode-go", "port": 4096, "auth_env": "OPENCODE_SERVER_PASSWORD"}
}
```

The named variables belong only in the trusted controller environment, never `worker_env`. The ports must match your local CLI instances; they are not discovered automatically. `quota-refresh` performs fixed GET requests to `127.0.0.1`, writes only selected percentages and UTC reset times to a private snapshot, and never changes route eligibility. `quota-status` and the local dashboard read that file; providers without a configured source report unknown. The local REST APIs are experimental, so a missing or changed response reports unknown. Current verification uses a local HTTP fixture, not live signed-in CLI instances. See the [Kimi Code server API](https://moonshotai.github.io/kimi-code/en/reference/server-api.html) and [OpenCode usage API](https://opencode.ai/docs/api#usage).

Exit code 75 means confirmed quota exhaustion. Code 77 is authentication failure, 1 is another execution failure, and 78 is configuration failure. Only code 75 permits failover to another key; other outcomes stop the task for investigation. Commit, CI, scope validation, and publication remain controller responsibilities.

## Route verification status

On 5 November 2026, separate disposable file tasks completed through the pinned OpenCode 1.18.35 image on Docker Engine 29.8.0. Both configured routes returned `WORKER_OK` (exit 0) and wrote the requested exact file content. On 7 November, one fresh no-network file task per route repeated that success through image `sha256:3cbfb5b5e24366c97c2018e77ec4ebb8702d3c553f7ca050f690f2f990feff15`; both returned bounded usage, exact content, and no leaked key or remaining container. An invalid credential on Kimi Code previously returned `WORKER_AUTH_FAILURE` (exit 77). An invalid-credential OpenCode Go attempt was stopped after about 35 seconds without a provider response; its exit 137 does not establish an authentication result.

The following checks used controlled local responses, not provider endpoints: for both configured routes, structured 401 and 429 and stderr 403 mapped to authentication or quota failure; malformed events, text mentioning 429, and timeout stopped as execution errors. The worker emitted only a bounded outcome marker and aggregate or unavailable usage, without raw error text or credentials. Controller tests cover cooldown, partial-edit reset, slot release after quota fallback, and bounded transient retries. A disposable [Issue #96](https://github.com/greggubarev/semiauto-e2e-smoke/issues/96) exercised the remote blocked path with a deterministic exit 77; its comment reported only `WorkerFailure`, with no key, raw response, or PR. These are controlled failure tests, not live provider-origin failures.

Both built-in routes remain **experimental for failure handling** until provider-origin 403, 429, timeout, and malformed responses and a complete controller retry/cooldown cycle are observed. OpenCode Go authentication rejection also remains unverified. Additional compatible endpoints have no live coverage under this trial and remain experimental until tested separately. See [Issue #6](https://github.com/greggubarev/ermik/issues/6) for the remaining matrix.

OpenCode may read and change worktree files. Shell commands, subagents, external directories, and other tools are disabled, and the repository is mounted as an isolated checkout. These tool limits supplement container isolation and the controller's final file validation. The selected provider key is still available to the networked worker process. Use a key with bounded cost and assess provider exposure before processing confidential repositories.

References: [OpenCode CLI](https://docs.opencode.ai/docs/cli/), [providers](https://docs.opencode.ai/docs/providers/), [permissions](https://docs.opencode.ai/docs/permissions/), [OpenCode Go](https://docs.opencode.ai/docs/go/).
