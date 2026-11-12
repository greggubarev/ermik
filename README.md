# Ermik

Ermik is a self-hosted queue that turns approved GitHub Issues into draft pull requests. A trusted controller runs a configured task command in an isolated container, checks the result locally, and publishes the PR. A separate maintainer installation reviews the current commit and decides whether to request changes or merge. GitHub Actions and a paid GitHub plan are not required.

The name is inspired by the [ermine](https://www.nature.scot/plants-animals-and-fungi/mammals/land-mammals/other-carnivores/stoat-and-weasel), the stoat in its white winter coat. It is not an acronym.

![Issue admission, isolated execution, draft PR, and maintainer decision](assets/workflow.svg)

## Design focus

Issue-to-PR automation already exists. Ermik's distinct focus is the handoff around execution: who admitted the exact task, what paths and checks were allowed, which commit was reviewed, what happened after interruption, and who may merge.

- **An explicit handoff.** Admission binds the issue body and allowed paths. The controller publishes one draft PR per issue; a correction continues on that PR. A changed HEAD invalidates the previous review.
- **A real publication boundary.** The task container receives neither GitHub credentials nor the Docker socket. Fork mode can keep upstream Contents write and merge permission off the execution host; the [security guide](docs/security.md) states the trusted parts and remaining limits.
- **Checks without hosted CI.** Each project supplies its own task image and check commands. The controller runs checks locally before publication; the maintainer runs them again on the exact PR HEAD.
- **Recoverable, portable decisions.** The controller reconciles interrupted writes with GitHub. Versioned evidence, a review packet, and a private exact-HEAD decision trail help a separate maintainer installation detect missing or stale results. [Verified trials and limits](docs/readiness.md) are recorded separately from planned work.

Ermik is a workflow controller. The included demo command proves the handoff; a real project must provide an appropriate `worker_command`, image, and checks.

## Run it separately from your project

Install Ermik on a dedicated Ubuntu 24.04+ controller host. Keep its checkout, state, workspace, account, and service apart from the target repository and other applications. The maintainer uses a second installation with its own state and credentials. The two installations share the target repository, admission rules, and check policy; they do not share a SQLite database.

```sh
sudo apt-get update
sudo apt-get install -y git ca-certificates
git clone https://github.com/greggubarev/ermik.git
cd ermik
git switch --detach HEAD
git rev-parse HEAD
sed -n '1,180p' scripts/bootstrap-ubuntu.sh
bash scripts/bootstrap-ubuntu.sh --check
sudo bash scripts/bootstrap-ubuntu.sh --apply
cd ..
test ! -e /home/ermikctl/ermik && test ! -L /home/ermikctl/ermik
sudo mv ermik /home/ermikctl/ermik
sudo chown -R ermikctl:ermikctl /home/ermikctl/ermik
```

The bootstrap prepares host packages and a dedicated `ermikctl` account; the move puts this checkout at the path expected by the systemd unit. Stop if `/home/ermikctl/ermik` already exists. The script does not authenticate to GitHub, configure a project, grant Docker access, or start polling. Continue with the [standalone server and maintainer setup](docs/setup.md), then run the [first complete cycle](docs/first-project.md) in a disposable repository before enabling the service. For a real project, replace the demo image, command, and checks with project-specific ones. The [configuration example](examples/config.example.json) is a starting point, not a production policy.

The minimum read-only preflight, after configuring each installation, is:

```sh
python3 -m ermik --config config.json doctor
python3 -m ermik --config config.json status
```

The maintainer admits an issue; the server's `run-once` or `poll` takes only admitted `queue:worker` and `state:ready` issues. The maintainer reviews the draft PR, sends corrections to the same PR if needed, and explicitly merges after reviewing the new HEAD. [Maintainer commands](docs/maintainer.md) and [worker operation](docs/worker.md) give the exact steps.

## Roadmap

| Milestone | Focus | Closed issues | What remains |
| --- | --- | ---: | --- |
| [v0.2.0](https://github.com/greggubarev/ermik/milestone/1) | Reliable preview operation | **20 / 20** | No open milestone issues; the [reboot trial](https://github.com/greggubarev/ermik/issues/7#issuecomment-6643766138) ended in a specific blocked state. |
| [v0.3.0](https://github.com/greggubarev/ermik/milestone/2) | Portable handoff and actionable recovery | **7 / 7** | [#37](https://github.com/greggubarev/ermik/issues/37) adds distinct failure guidance after v0.2; release host gates remain. |
| [v0.4.0](https://github.com/greggubarev/ermik/milestone/3) | Measured routing | **5 / 5** | All milestone issues are closed; provider-origin failure handling remains experimental. |
| [v0.5.0 preview](https://github.com/greggubarev/ermik/releases/tag/v0.5.0-preview.1) | Operator view and adoption | **3 / 3** | Two scoped integrations, the optional maintainer view, and bounded event digests are verified. The view currently takes 20–26 seconds per live one-PR read; see [release status](docs/readiness.md). |
| [v0.6.0 preview](https://github.com/greggubarev/ermik/releases/tag/v0.6.0-preview.1) | Approved task complexity and safe recovery | **2 / 2** | [#106](https://github.com/greggubarev/ermik/issues/106) binds approved effort profiles; [#109](https://github.com/greggubarev/ermik/issues/109) recovers provably unstarted blocked jobs. |

The six earlier v0.3 evidence tasks were already present in the v0.2 preview source tag. Actionable recovery in #37 is the new v0.3 release outcome. Counts do not indicate production readiness or a schedule. Provider failure handling remains experimental. After v0.6, a read-only project preflight is a candidate only if more installation evidence shows that missing task checks or reviewer instructions block adoption. See [acceptance criteria](docs/roadmap.md) and [verified trials and limits](docs/readiness.md).

## Guides

| Start here | For |
| --- | --- |
| [Setup](docs/setup.md) · [first project](docs/first-project.md) | Dedicated controller and maintainer installations; full disposable trial |
| [Security](docs/security.md) · [PR broker](docs/pr-broker.md) | Credential split, fork publication, and trust limits |
| [Local checks](docs/local-ci.md) · [providers](docs/providers.md) | Project check policy and optional task-command routes |
| [Status](docs/status.md) · [storage](docs/storage.md) | Queue visibility, local dashboard, and bounded artifact cleanup |
| [Evidence](docs/evidence.md) · [troubleshooting](docs/troubleshooting.md) | Review handoff, stale results, and recovery |
| [Release status](docs/readiness.md) · [roadmap](docs/roadmap.md) | Verified behavior and open gates |

Before publishing a source release, run the [release gate](docs/release.md) against the reviewed commit. Ermik is distributed under the [MIT License](LICENSE); custom task images retain their own dependency licenses.
