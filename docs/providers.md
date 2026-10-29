# OpenCode execution

The controller selects a model for each attempt. It passes `ERMIK_MODEL_REF`, the public `ERMIK_MODEL_SPEC`, and only the selected provider's key variable to the container. Key values are not stored in controller JSON, SQLite, command arguments, or logs. Inside the disposable container, the key temporarily resides in a private `0600` OpenCode configuration file, removed after the attempt. The container prints one outcome code: `WORKER_OK`, `WORKER_QUOTA_EXHAUSTED`, `WORKER_AUTH_FAILURE`, `WORKER_EXECUTION_ERROR`, or `WORKER_CONFIGURATION_ERROR`. A `WORKER_USAGE` line contains aggregate token data, without session text or keys. If session export is unavailable, `complete=false` and all numeric values are `null`.

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

`claim_model` reserves a SQLite slot. By default each provider has one concurrent slot, so simultaneous tasks can use different accounts. `release_model` frees it after the attempt. `provider_cooldowns` stores the next retry time for an entire account after confirmed HTTP 429. The current controller cooldown is 15 minutes; the record API permits at most 24 hours. Before trying another model, the controller resets the worktree to HEAD and removes untracked files. If none is available, the job stays in `retry_wait` until its local cooldown. Check external quota and recovery time in the provider account; they are not fixed here.

Exit code 75 means confirmed quota exhaustion. Code 77 is authentication failure, 1 is another execution failure, and 78 is configuration failure. Only code 75 permits failover to another key; other outcomes stop the task for investigation. Commit, CI, scope validation, and publication remain controller responsibilities.

OpenCode may read and change worktree files. Shell commands, subagents, external directories, and other tools are disabled, and the repository is mounted as an isolated checkout. These tool limits supplement container isolation and the controller's final file validation. The selected provider key is still available to the networked worker process. Use a key with bounded cost and assess provider exposure before processing confidential repositories.

References: [OpenCode CLI](https://docs.opencode.ai/docs/cli/), [providers](https://docs.opencode.ai/docs/providers/), [permissions](https://docs.opencode.ai/docs/permissions/), [OpenCode Go](https://docs.opencode.ai/docs/go/).
