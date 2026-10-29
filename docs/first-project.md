# First complete cycle in a new project

This trial uses **Ubuntu 24.04+**, one computer with Docker and GitHub CLI, and a new private repository. It exercises issue admission, container execution, local checks, a draft PR, review, changes in the same PR, a new review, and merge. For separate maintainer and server hosts, follow [setup.md](setup.md) after the trial.

The demo worker is deterministic: it creates one file and adds a docstring when asked for a correction. This tests the workflow, not general issue execution. A real project needs its own image, `worker_command`, and `checks`.

## 1. Prepare the host

Install Python, Git, GitHub CLI, and Docker using the [clean Ubuntu instructions](setup.md#prepare-a-clean-ubuntu-host). Run these commands as a normal user with Docker access and the intended GitHub account:

```sh
python3 --version
git --version
docker info --format '{{.ServerVersion}}'
gh auth login
gh auth setup-git
gh auth status
```

Set the commit identity for the trial repository:

```sh
git config --global user.name 'Example Project'
git config --global user.email 'project@example.invalid'
```

Replace both values with your actual project identity before using a production repository.

## 2. Create the target repository with an initial commit

```sh
export TARGET_REPO="$(gh api user --jq .login)/ermik-trial-$(date +%Y%m%d%H%M%S)"
mkdir -p "$HOME/ermik-trial"
cd "$HOME/ermik-trial"
git init -b main
printf '%s\n' '# Trial project' > README.md
git add README.md
git commit -m 'Initialize trial project'
gh repo create "$TARGET_REPO" --private --source=. --remote=origin --push
gh repo view "$TARGET_REPO" --json nameWithOwner,defaultBranchRef --jq '{repo:.nameWithOwner,base:.defaultBranchRef.name}'
printf 'Target repository: %s\n' "$TARGET_REPO"
```

Save the printed repository name. Every later command targets it. Use a disposable repository for this first run.

## 3. Build the worker and configure Ermik

```sh
cd "$HOME"
git clone https://github.com/greggubarev/ermik.git
cd "$HOME/ermik"
docker build -f examples/Dockerfile.demo -t ermik-demo:latest .
PYTHONPATH=. python3 tests/host_smoke.py ermik-demo:latest
cp examples/config.example.json config.json
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('config.json')
cfg = json.loads(path.read_text(encoding='utf-8'))
cfg['repo'] = os.environ['TARGET_REPO']
cfg['maintainer'] = os.environ['TARGET_REPO'].split('/', 1)[0]
cfg['state_dir'] = str(Path.home() / '.local' / 'state' / 'ermik-trial')
cfg['workspace'] = str(Path.home() / '.local' / 'share' / 'ermik-trial')
path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
chmod 0600 config.json
python3 -m ermik --config config.json doctor
python3 -m ermik --config config.json status
```

The source repository is private if `git clone` reports denied access; grant the account read access. `host_smoke.py` should report that the missing-file rejection, execution, correction, and local check passed. `doctor` should show the intended `repo`, `base=main`, account, and image. Resolve discrepancies before continuing.

## 4. Move the issue through review and correction

```sh
gh label create queue:worker -R "$TARGET_REPO" --color 1D76DB
gh label create state:proposed -R "$TARGET_REPO" --color C5DEF5
gh label create state:ready -R "$TARGET_REPO" --color 0E8A16
gh label create state:in-progress -R "$TARGET_REPO" --color FBCA04
gh label create state:blocked -R "$TARGET_REPO" --color D93F0B

ISSUE_URL="$(gh issue create -R "$TARGET_REPO" --title 'Create example greeting' \
  --body-file examples/issue.md --label queue:worker --label state:proposed)"
ISSUE_NUMBER="${ISSUE_URL##*/}"
python3 -m ermik --config config.json admit "$ISSUE_NUMBER"

RUN_RESULT="$(python3 -m ermik --config config.json run-once)"
printf '%s\n' "$RUN_RESULT"
PR_NUMBER="$(printf '%s' "$RUN_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["pr"])')"
gh pr view "$PR_NUMBER" -R "$TARGET_REPO" --json isDraft,headRefOid,url
gh pr diff "$PR_NUMBER" -R "$TARGET_REPO"
python3 -m ermik --config config.json review "$PR_NUMBER"

NOTE_FILE="$(mktemp)"
printf '%s\n' 'Add a short docstring to greeting() and keep its return value unchanged.' > "$NOTE_FILE"
python3 -m ermik --config config.json request-changes "$PR_NUMBER" --note-file "$NOTE_FILE"
rm "$NOTE_FILE"
python3 -m ermik --config config.json run-once
gh pr diff "$PR_NUMBER" -R "$TARGET_REPO"
python3 -m ermik --config config.json review "$PR_NUMBER"
python3 -m ermik --config config.json merge "$PR_NUMBER"
python3 -m ermik --config config.json status
gh issue view "$ISSUE_NUMBER" -R "$TARGET_REPO" --json state,url
```

Both `run-once` calls should return the same PR number with different HEADs. After correction, the file should have a docstring. Verify `main_ci.result=passed` in the merge result and that the issue is closed. If a command stops, preserve `state_dir`, logs, and the checkout for [troubleshooting](troubleshooting.md); reconcile before retrying.

## 5. Apply the workflow to a real project

1. Create a separate `config.json` for the target `owner/repo`. Set `image`, `worker_command`, and `checks` for its language and build. Install dependencies in the image ahead of time; checks have no network.
2. The worker command must read `/task/issue.md`, edit only files under `/work`, and exit nonzero on failure. The controller handles publication, labels, and merge. The demo `/worker.sh` cannot solve arbitrary issues.
3. Keep `max_parallel=1` initially. Verify the project's positive and negative cases and a full correction cycle. Then split maintainer and server roles using [setup.md](setup.md), with separate state directories and GitHub credentials.
4. Configure [local checks](local-ci.md) and manual acceptance criteria. A project without GitHub Actions still needs explicit checks.

The trial establishes behavior for its specific configuration. It does not establish safety of another worker or completeness of another project's checks.

## Publication through a separate fork

If the server account must not write upstream contents, create a fork and set `"publish_repo": "publisher/project"` and `"publish_token_env": "ERMIK_FORK_TOKEN"` in server `config.json`. `repo` remains the target `owner/project`; `publish_repo` is the fork's full `owner/name`. Set the same two fields on the maintainer computer so review verifies the PR source. Omit them for direct branch publication to the target repository.

The server uses **two separate credentials**. `GH_TOKEN` has upstream Contents read, Issues write, and Pull requests write, without Contents write. `ERMIK_FORK_TOKEN` has Contents write only on the fork. Keep values in the trusted controller environment, never JSON or the worker container. The maintainer has a separate merge credential. Before publication, the controller validates the fork's exact name, parent, and access through the respective credentials. Any failed check stops publication. It pushes to the fork and opens a draft PR against `repo` using `GH_TOKEN` and the explicit fork owner.

In a disposable repository, verify that the server credential can change an issue label, create a PR, and push to the fork. Test upstream push refusal only on a disposable branch, never `main`. Merge requires the maintainer's upstream Contents write credential. Do not broaden server credentials to pass a negative test. GitHub may report `permissions.push` based on the account role even with a narrow token; conservative preflight rejects this setup. Use a separate publisher account for a stronger boundary.
