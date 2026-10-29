# Setup

This project coordinates scoped GitHub issues with an explicit concurrency limit. A trusted host process reads GitHub, checks the issue and checkout, runs the configured commands, and prepares a draft pull request. The task command runs in a Docker container with the repository checkout at `/work` and the issue body at `/task/issue.md`. The container does not receive GitHub credentials. A maintainer reviews and explicitly requests a merge; there is no automatic merge. GitHub Actions and a paid GitHub plan are not required for the local checks.

## Prerequisites

- Python 3.11 or newer, `git`, `gh` CLI, and Docker with a working daemon.
- A repository you maintain. Use a disposable private repository for the first end-to-end trial.
- A dedicated host account or service identity for the trusted controller. Access to the Docker daemon is privileged; limit this account and its filesystem access accordingly.
- A task image containing the command named by `worker_command`. The included image and script are a deterministic demonstration only. Supply an image and command suited to your project before using real tasks.

## Prepare a clean Ubuntu host

The commands below target Ubuntu 24.04 or newer, where the distribution's `python3` satisfies the Python 3.11 minimum. Run the package commands as a sudo-capable administrator. On an older Ubuntu release, install a supported Python version first and use that exact executable in the commands and service below.

```sh
sudo apt update
sudo apt install -y python3 git ca-certificates curl
python3 --version
git --version
```

Install `gh` from the [official GitHub CLI apt repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md#debian):

```sh
sudo install -m 0755 -d /etc/apt/keyrings
gh_key="$(mktemp)"
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$gh_key"
sudo install -m 0644 "$gh_key" /etc/apt/keyrings/githubcli-archive-keyring.gpg
rm "$gh_key"
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' "$(dpkg --print-architecture)" | sudo tee /etc/apt/sources.list.d/github-cli.list
sudo apt update
sudo apt install -y gh
gh --version
```

Install Docker Engine using [Docker's official Ubuntu apt repository](https://docs.docker.com/engine/install/ubuntu/). If Docker or conflicting distribution packages are already present, inspect that guide before changing packages. On a clean host:

```sh
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl status docker --no-pager
sudo docker run --rm hello-world
```

Run the controller under a dedicated, non-root account that is **not** the account running another project's services. On a clean host, an administrator first checks that the paths are unused and creates the account and directories:

```sh
test ! -e /var/lib/ermik && test ! -e /srv/ermik
sudo adduser --disabled-password --gecos '' ermikctl
sudo install -d -o ermikctl -g ermikctl -m 0700 /var/lib/ermik /srv/ermik/workspace
sudo loginctl enable-linger ermikctl
```

Then use that account for the remaining account-level commands (`sudo -iu ermikctl` for initial setup, followed by an SSH or console login for the user service). Keep its home, checkout, GitHub credentials, state, workspace, and unit name separate from any existing deployment. Never stop, disable, rename, or replace another project's service or timer. [Rootless Docker](https://docs.docker.com/engine/security/rootless/) is preferred where supported. The default Docker daemon's `docker` group grants root-level privileges; adding the controller account to that group is a deliberate host privilege grant, not a containment measure. Do not add the untrusted container process or unrelated users to it. Verify that `docker info` works **as the controller account** before proceeding.

Authenticate `gh` as the controller account before cloning a private source repository, and enable its Git credential helper:

```sh
gh auth login
gh auth setup-git
gh auth status
```

Clone the source as that account. The repository may still be private; the account needs read access. Inspect the checkout and choose a reviewed release tag or commit:

```sh
SOURCE_URL='https://github.com/greggubarev/ermik.git'
git clone "$SOURCE_URL" "$HOME/ermik"
cd "$HOME/ermik"
git status --short
git rev-parse HEAD
```

## Configure

1. In the trusted controller account, authenticate `gh` to the intended GitHub account. Keep its credential store outside the repository and worker checkout. Confirm the selected account and target repository:

   ```sh
   gh auth status
   gh repo view example-org/example-project --json nameWithOwner --jq .nameWithOwner
   gh api repos/example-org/example-project/collaborators/example-maintainer/permission --jq .permission
   git config --global user.name 'Example Project'
   git config --global user.email 'project@example.invalid'
   ```

   Set `user.name` and `user.email` to your actual product or organization identity for commits; do not invent a person. The permission query must show the rights needed for the maintainer's planned review and merge. Use a repository-scoped credential with the least permissions supported by the chosen workflow. Never put a token in an issue, config file, Docker build context, container environment, or checkout.

2. Copy `examples/config.example.json` to `config.json`. Replace `repo` and `maintainer` with the real GitHub identities. Set `base_branch` if the target uses a branch other than `main`. Choose absolute, private paths for `state_dir` and `workspace`; they must be separate from the source checkout. For an isolated installation, use `/var/lib/ermik` and `/srv/ermik/workspace` in place of the example defaults; verify neither path is already owned by another application. Set `image`, `worker_command`, and `checks` for your project. Each command is a JSON array of arguments, not a shell string. `worker_env` is an allowlist of host environment variable names copied into the container; leave it empty for the demo. Do not put environment variable values or secrets in JSON. Start with `max_parallel: 1` and increase it only after proving that tasks have separate scopes and workspaces.

   ```sh
   cp examples/config.example.json config.json
   chmod 0600 config.json
   ```

3. Inspect the host directories created by the administrator before starting the controller; alternatively, change both paths in `config.json` to private absolute directories owned by this account:

   ```sh
   chmod 0600 config.json
   stat -c '%U %a %n' config.json /var/lib/ermik /srv/ermik/workspace
   ```

   The trusted controller account should own all three paths. `config.json` is local and ignored by Git. Restrict the `gh` credential store and any provider credential to the accounts that need them. Do not share the controller's home directory with the container.

4. For the mock demonstration, build the included image from this repository:

   ```sh
   docker build -f examples/Dockerfile.demo -t ermik-demo:latest .
   ```

   The example issue at `examples/issue.md` allows only `src/example.py`. The demo command reads `/task/issue.md` and writes that one file. On a correction request it adds a docstring to the same file. Use a disposable target repository with the issue body copied from the example. The configured check executes the function and asserts its return value; the maintainer still reviews the actual diff and result.

   Smoke-test the image without GitHub or a target repository. This command uses a temporary checkout and no network:

   ```sh
   demo_dir="$(mktemp -d)"
   mkdir "$demo_dir/work"
   cp examples/issue.md "$demo_dir/issue.md"
   docker run --rm --network none --user "$(id -u):$(id -g)" \
     --mount "type=bind,src=$demo_dir/work,dst=/work" \
     --mount "type=bind,src=$demo_dir/issue.md,dst=/task/issue.md,readonly" \
     ermik-demo:latest /bin/sh /worker.sh
   cat "$demo_dir/work/src/example.py"
   PYTHONPATH=. python3 tests/host_smoke.py ermik-demo:latest
   ```

   On Windows PowerShell, run `$env:PYTHONPATH='.'; py -3 tests/host_smoke.py ermik-demo:latest` from the repository root.

   Confirm that the file contains the expected function. The temporary directory can then be removed after inspection.

5. In the disposable target repository, create the labels used for the worker queue. Skip a `gh label create` command if the label already exists:

   ```sh
   gh label create queue:worker -R example-org/example-project --color 1D76DB --description 'Eligible for the worker queue'
   gh label create state:proposed -R example-org/example-project --color C5DEF5 --description 'Awaiting admission'
   gh label create state:ready -R example-org/example-project --color 0E8A16 --description 'Approved for admission'
   gh label create state:in-progress -R example-org/example-project --color FBCA04 --description 'Claimed task'
   gh label create state:blocked -R example-org/example-project --color D93F0B --description 'Needs maintainer action'
   ```

   Create an issue with the body from `examples/issue.md` and both labels:

   ```sh
   gh issue create -R example-org/example-project --title 'Create example greeting' \
     --body-file examples/issue.md --label queue:worker --label state:proposed
   ```

   Record its number from the returned URL. On the maintainer's computer, clone this repository, authenticate `gh` as the configured maintainer, and make a second `config.json` with the same `repo`, `maintainer`, `image`, `worker_command`, and `checks`, but separate private absolute `state_dir` and `workspace`. The `admit NUMBER` command publishes an exact-body approval comment under that account and moves the issue to `state:ready`. The server checks this GitHub approval; it does not need the maintainer's local SQLite file. Label creation and issue edits are GitHub writes.

## Configure OpenCode execution

To use OpenCode instead of the demo image, copy `examples/config.opencode.example.json` to local `config.json`, then set `repo`, `maintainer`, paths, and `checks` as described above. Build and inspect the pinned image:

```sh
docker build -f examples/Dockerfile.opencode -t ermik-opencode:1.18.35 .
docker image inspect ermik-opencode:1.18.35 --format '{{.Id}}'
cp examples/config.opencode.example.json config.json
chmod 0600 config.json
```

On the Docker host, you can test cleanup of a container left by a stopped controller. The test creates a network-disabled container with its own label and removes only that container:

```sh
PYTHONPATH=. python3 tests/host_container_recovery.py ermik-opencode:1.18.35
```

The example configuration is not ready for arbitrary projects: its `checks` validates only the demo task file. Build an image with your dependencies, configure meaningful checks, and add [JUnit XML validation](local-ci.md) if needed. See [provider routing](providers.md) for models and additional OpenAI-compatible accounts.

Create a private environment file **outside the checkout** under the controller account. It needs only keys for providers enabled in `models`. Do not put values in documentation, JSON, or command-line arguments.

```sh
install -d -m 0700 "$HOME/.config/ermik"
touch "$HOME/.config/ermik/providers.env"
chmod 0600 "$HOME/.config/ermik/providers.env"
${EDITOR:-vi} "$HOME/.config/ermik/providers.env"
```

Use `KIMI_CODE_API_KEY=...` and `OPENCODE_GO_API_KEY=...` lines without `export`. For interactive `doctor` and `run-once`, load the file into the current shell and close that shell after use:

```sh
set -a
. "$HOME/.config/ermik/providers.env"
set +a
python3 -m ermik --config config.json doctor
```

`doctor` checks only for the presence of keys; it does not call provider APIs or report remaining quota. Verify API access with a separate file task in an isolated container, then verify project suitability with a complete cycle in a disposable repository. With `models` configured, the controller passes only the selected key to each attempt's container. Do not also include it in `worker_env`.

## Read-only preflight

Run these commands before admitting work:

```sh
python -m ermik --config config.json doctor
python -m ermik --config config.json status
```

Treat this as the dry run: inspect the reported repository, account, paths, Docker availability, image, commands, and current state. It does not execute a task or create a PR. Also inspect the exact issue body and allowed paths with `gh issue view NUMBER --repo example-org/example-project`. Do not continue if a prerequisite or permission check is uncertain.

## Run a disposable task

Create an issue from `examples/issue.md` in the disposable repository. After the preflight and a maintainer's decision, run `admit` on the maintainer computer and `run-once` on the server:

```sh
# Maintainer computer, signed in as the configured maintainer:
python3 -m ermik --config config.json admit NUMBER

# Server, signed in as its trusted publisher account:
python3 -m ermik --config config.json run-once
python3 -m ermik --config config.json status
```

Inspect the draft PR, changed paths, local check output, and exact head commit. Request a correction if needed with `request-changes PR --note-file PATH`. A maintainer can record review with `review PR` and explicitly run `merge PR` after the required checks. These commands change GitHub or local state; they are outside the dry run. For regular operation, `poll` repeats queue inspection at `poll_seconds`; run only one trusted controller for a given `state_dir`.

## Run polling with systemd

The checked-in [polling unit](../examples/systemd/ermik.service) and [preflight unit](../examples/systemd/ermik-preflight.service) are **user** services for the dedicated controller account. They assume this checkout is at `$HOME/ermik` and Python is `/usr/bin/python3`; edit both copies if those paths differ. The unit name and checkout are separate from any existing project's units and timers. Run the following from a real login session of the controller account so `systemctl --user` connects to its user manager. An administrator enables linger once so the service starts at boot and stays up after logout:

```sh
install -d -m 0700 "$HOME/.config/ermik" "$HOME/.config/systemd/user"
touch "$HOME/.config/ermik/providers.env"
chmod 0600 "$HOME/.config/ermik/providers.env"
install -m 0644 examples/systemd/ermik.service "$HOME/.config/systemd/user/"
install -m 0644 examples/systemd/ermik-preflight.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemd-analyze --user verify "$HOME/.config/systemd/user/ermik.service" "$HOME/.config/systemd/user/ermik-preflight.service"
```

The required `providers.env` may be empty for the demo. For a configured provider, fill it with its `credential_env` names as `NAME=value` lines, using systemd EnvironmentFile quoting for values with spaces or special characters. Keep it outside the checkout and readable only by the controller account. The unit loads the keys into the trusted controller; the runtime passes the selected provider key to its task container. Do not place a GitHub token in this file. In fork mode, create a separate `0600` `$HOME/.config/ermik/publisher.env` containing the `publish_token_env` value for the fork only; both units load this optional file. The upstream `gh` credential stays in the controller account's private credential store and must have no Contents write on upstream. Never enable the preflight unit; it runs only when explicitly started.

For a private upstream and a private fork owned by different accounts, the upstream key may not be able to read the fork branch when creating a PR. Configure `draft_pr_socket` with the absolute Unix socket path and run the separate [draft PR broker](pr-broker.md) with a credential that can read both repositories and create PRs in upstream. For a separate broker UID, set up a shared read-only evidence directory and configure `draft_pr_evidence_dir` as described there; the controller's private `state_dir` is not traversable by another UID. The controller still uses its upstream read key and fork push key. The broker checks exact refs, admission, scope, and prepublication local check evidence. Set up and verify the broker before enabling polling; a missing broker leaves pushed branches pending for recovery.

Check identity, config, paths and ownership, then run the read-only preflight **through systemd** so it sees the same environment and Docker context as polling:

```sh
test "$(id -un)" = ermikctl
test -f "$HOME/ermik/config.json"
stat -c '%U %a %n' "$HOME/ermik/config.json" "$HOME/.config/ermik/providers.env" /var/lib/ermik /srv/ermik/workspace
gh auth status
docker info --format '{{.ServerVersion}}'
systemctl --user start ermik-preflight.service
journalctl --user -u ermik-preflight.service -n 50 --no-pager
```

Inspect the `doctor` JSON for the exact repository, account, base branch, image and `credential_available: true` for every configured model. `doctor` fails when a configured credential is missing; it does not call provider APIs. Confirm `status` succeeds and that `state_dir` and `workspace` are the private, separate paths in `config.json`. A `gh` or Docker check performed in an interactive shell alone does not establish that the service has access. If the preflight fails, inspect `journalctl --user -u ermik-preflight.service`, correct the cause and rerun it before enabling polling.

After the disposable end-to-end trial, enable only this polling unit. Verify one active process and watch its first queue inspection:

```sh
systemctl --user enable --now ermik.service
systemctl --user status ermik.service --no-pager
journalctl --user -u ermik.service -n 50 --no-pager
systemctl --user show ermik.service -p MainPID -p ActiveState -p SubState
```

`poll` checks the queue every `poll_seconds`; systemd restarts it on process failure. Verify after a logout or reboot that the unit is active and its journal has a fresh poll result. A successful process start does not prove a full task cycle. Run only one controller for the configured `state_dir` and do not add a second timer or cron job. This installation does not modify other projects' services or timers.

## Review from a local computer

The maintainer can inspect an issue and PR from a local computer using their own `gh` login:

```sh
gh issue view NUMBER -R example-org/example-project
gh pr view PR -R example-org/example-project
gh pr diff PR -R example-org/example-project
```

For independent review, clone this repository on the local computer, copy the same configuration with **local** absolute `state_dir` and `workspace` paths, build the configured check image, and authenticate `gh` as the configured maintainer. The local review record stays on that computer. Then run:

```sh
python3 -m ermik --config config.json review PR
python3 -m ermik --config config.json request-changes PR --note-file /private/review.txt
python3 -m ermik --config config.json merge PR
```

Keep the review note in a private local file. After a correction, run `review PR` again on the new HEAD before `merge PR`. Neither a successful check nor the background `poll` service merges a PR. The configured `checks` must run in the local Docker image too; a reviewer on Windows needs Docker Desktop or a Linux review host.

If `main` advances and the accepted file contents need no changes, put `SEMI_REBASE_ONLY` on its own line in the review note. The controller then rebases the existing PR without invoking a model, requires the same changed paths, runs the configured checks on the new commit, and pushes with a lease. A blocked published job can receive a **new** exact-head `request-changes` note; the controller checks the PR and admission again before retrying. Always run `review PR` on the resulting HEAD because the prior review record is stale.

## Stop, update and roll back

Stop this unit before changing its checkout, image, config or secret file. Preserve its state and workspace and reconcile any interrupted Issue, branch and PR with GitHub before retrying a write. Record the old commit and image ID so a rollback targets exact versions:

```sh
systemctl --user stop ermik.service
cd "$HOME/ermik"
backup_dir="$HOME/.local/share/ermik-backup-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup_dir"
git rev-parse HEAD > "$backup_dir/commit"
docker image inspect "$(python3 -c 'import json; print(json.load(open("config.json"))["image"])')" --format '{{.Id}}' > "$backup_dir/image-id"
cp -a config.json "$backup_dir/config.json"
cp -a "$HOME/.config/ermik/providers.env" "$backup_dir/providers.env"
if test -f "$HOME/.config/ermik/publisher.env"; then cp -a "$HOME/.config/ermik/publisher.env" "$backup_dir/publisher.env"; fi
cp -a "$HOME/.config/systemd/user/ermik.service" "$HOME/.config/systemd/user/ermik-preflight.service" "$backup_dir/"
tar -C / -czf "$backup_dir/state.tar.gz" var/lib/ermik
chmod 0600 "$backup_dir"/*
```

The backup contains the commit SHA, image ID, config and state. The archive uses the isolated example state path; adjust it to match the configured path if different. Keep the workspace in place for reconciliation instead of copying its cached clone and worktrees on every update. Use an immutable image tag or digest, and save the image separately only when it cannot be rebuilt or pulled from its pinned source. Check available disk space before a large state backup, and retain only backups still needed for rollback after verifying their exact paths. Keep backups outside the checkout and protect them as controller state. Update to a reviewed exact commit, install the matching unit copies if changed, rebuild or pull the pinned image, and run the systemd preflight above before restarting. A shell-level `doctor` is insufficient for this gate. Do not start polling while another controller uses the same state directory.

If the new version fails, stop **only** `ermik.service`, restore the recorded commit (`git checkout --detach COMMIT`), the saved config, provider file and unit copies, and verify that the recorded image ID is still available. Rebuild or pull the pinned image if necessary; if you saved an image archive separately, load it. Reload the user manager and repeat the preflight before restarting. Keep the current state and workspace for reconciliation with GitHub. Restoring an older SQLite snapshot after a remote Issue, branch or PR write can replay work, so restore the archived state only after comparing every affected remote identity and local event with the backup. Do not remove state or re-admit an Issue merely because a command timed out.

After setting `backup_dir` to the saved directory, the local rollback commands are:

```sh
systemctl --user stop ermik.service
cd "$HOME/ermik"
old_commit="$(cat "$backup_dir/commit")"
git checkout --detach "$old_commit"
install -m 0600 "$backup_dir/config.json" config.json
install -m 0600 "$backup_dir/providers.env" "$HOME/.config/ermik/providers.env"
if test -f "$backup_dir/publisher.env"; then install -m 0600 "$backup_dir/publisher.env" "$HOME/.config/ermik/publisher.env"; fi
install -m 0644 "$backup_dir/ermik.service" "$HOME/.config/systemd/user/"
install -m 0644 "$backup_dir/ermik-preflight.service" "$HOME/.config/systemd/user/"
docker image inspect "$(cat "$backup_dir/image-id")" --format '{{.Id}}'
systemctl --user daemon-reload
systemctl --user start ermik-preflight.service
```

Inspect the preflight result and reconcile remote state before `systemctl --user start ermik.service`.
