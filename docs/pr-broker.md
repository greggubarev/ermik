# Isolated draft PR publication

`ermik.pr_broker` accepts only `create_draft` over a Unix socket. It runs under a separate account and holds the token used to create PRs in the upstream repository. The controller can access the socket, not the token. The service cannot push, edit issues, merge, or delete PRs.

## Start the service

```sh
python3 -m ermik.pr_broker serve \
  --socket /run/pr-broker/create.sock \
  --socket-gid 1234 \
  --repo ORG/UPSTREAM --repo-id 123456 \
  --fork PERSON/FORK --fork-id 234567 \
  --maintainer MAINTAINER --controller-uid 1001 \
  --evidence-dir /var/lib/controller/evidence \
  --token-file /etc/pr-broker/github.token
```

`--token-env NAME` replaces `--token-file` for local use. The token file must be regular, owned by root or the broker user, and not group- or world-writable. The token must see both private repositories; read issues, comments, refs, comparisons, Git trees, and PRs; and create upstream PRs. The broker validates both full repository names and numeric IDs, plus the fork's exact parent. Obtain numeric IDs from GitHub during setup and keep them out of issue text.

For a separate broker UID, create a shared read-only evidence directory outside `state_dir`. The administrator sets the controller as owner, the shared broker group, and mode `2750` (setgid). Set `draft_pr_evidence_dir` in controller configuration and `--evidence-dir` for the broker to this path. The controller creates issue subdirectories with `0750` and files with `0640`; the broker can read but cannot edit them. Parent directories must be traversable by both users. The controller's `0700` `state_dir` cannot serve as evidence storage for another UID. If the broker runs as root, `draft_pr_evidence_dir` may be omitted and `--evidence-dir` must equal `<state_dir>/evidence`.

The administrator creates the socket directory in advance and prevents writes by unrelated users. The service does not remove an existing socket at startup. After binding, it sets `--socket-gid` and mode `0660`. Linux `SO_PEERCRED` must match the exact `--controller-uid`; group membership alone does not authorize a request.

### Private repositories under different owners

A repository-scoped token may be unable to read the other owner's private ref when creating a PR. One tested route is a GitHub App installed on **only** the upstream and fork. Give it `Contents: read`, `Issues: read`, and `Pull requests: read and write`; enable Device Flow and allow installation by both owners. Authorize an upstream maintainer through [GitHub's App user-token Device Flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app). Save the user access token in the broker's private token file. Keep the controller's upstream and fork tokens separate. An installation token for just one owner does not provide cross-owner access.

Before enabling polling, query `GET /user/installations` and `GET /user/installations/{installation_id}/repositories` under that token. Confirm exactly the intended installations and repositories. On a disposable upstream, attempt to create a **new branch ref** and merge a ready test PR under each server-side credential; both operations must be denied for every credential. Verify fork branch publication with the fork token and draft PR creation with the broker token. Never test denial against `main`.

With token expiration enabled (the GitHub App default), user access tokens expire after eight hours and Device Flow also returns a refresh token. Set up a private directory owned by the broker account and authorize the App from that account:

```sh
install -d -m 0700 "$HOME/.config/ermik-pr-broker"
python3 -m ermik.app_token_login \
  --client-id APP_CLIENT_ID --store "$HOME/.config/ermik-pr-broker/app-tokens.json"
```

The command prints a one-time code and GitHub URL; it writes both tokens to a `0600` file after confirmation. Run the broker command above with `--app-token-store "$HOME/.config/ermik-pr-broker/app-tokens.json" --app-client-id APP_CLIENT_ID` in place of `--token-file`. The broker checks the token at startup and every 60 seconds while idle. It [refreshes both tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens) an hour before access-token expiry, replacing the private pair atomically. A restart loads the durable pair and does not need another browser login after a completed refresh.

GitHub invalidates the old access and refresh tokens when refresh succeeds. If the process loses the response before saving the new pair, there is no safe automatic retry. The broker stops publication and reports that Device Flow authorization is required; queued work remains with the controller. Stop the broker, rerun the login command with `--reauthorize`, then restart it. Keep the token store and its adjacent recovery files together across restarts. Do not copy them into a checkout or container. Run only one broker for a token store; concurrent refresh or reauthorization is unsupported.

## Request and response

Send one UTF-8 JSON object per line, at most 4,096 bytes, with one request per connection:

```json
{"op":"create_draft","issue":7,"branch":"work/issue-7-delivery","head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","base_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
```

Success is `{"ok":true,"pr":19,"created":true}`. For the same existing open draft PR, `created` is `false`. Refusal is `{"ok":false,"error":"request refused"}`; it does not disclose GitHub output or secrets. After a lost response, the controller may repeat the identical request: the broker first looks for an existing PR and reconciles an uncertain POST result with GitHub. A closed PR, one converted from draft, a modified PR, or an ambiguous PR blocks creation of another.

## Check evidence and revalidation

After local checks and branch push, but before requesting a PR, the controller writes `<evidence-dir>/<issue>/<head_sha>.json` with restricted permissions. Neither the issue subdirectory nor the file may be group- or world-writable. The record contains at least:

```json
{"issue":7,"branch":"work/issue-7-delivery","head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","base":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","body_sha256":"<SHA-256 of exact issue body>","paths":["src/file.py"],"ci":{"result":"passed"}}
```

`paths` is sorted and includes old and new names for renames. The controller is the trusted source of local check results; the broker does not rerun checks or accept a result from the request or issue. Create this file before invoking the broker. After PR creation, the existing evidence storage scheme may use another record.

The prepublication record is limited to 65,536 UTF-8 bytes, including JSON formatting. The controller validates the exact serialized size before pushing a new branch and applies the same limit when writing the file. If the record is larger, reduce the configured check command data or evidence fields; the controller does not truncate proof silently. Other private controller evidence retains its separate 1 MiB limit.

For every request, the broker rereads the issue and requires it to be open with `queue:worker` and `state:in-progress`, valid `Allowed paths`, `Goal`, and `Acceptance` sections, and a current admission comment by the configured maintainer bound to the body and path hashes. It checks `main == base_sha`, the fork branch ref `== head_sha`, comparison with base and merge base, changed paths through `ermik.policy`, and regular file types in the Git tree. At most 100 changed files are accepted. It then validates check evidence and searches for an existing PR. Refs are checked again before POST. Missing or incomplete API responses cause refusal.

There is no distributed branch lock. If the branch changes between the last ref check and POST, later PR reconciliation detects the mismatch and refuses, but a newly created PR requires manual inspection. Run one broker per socket and prevent concurrent publication of the same branch.
