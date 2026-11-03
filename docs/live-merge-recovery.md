# Live merge recovery exercise

On 2026-11-03, the [merge recovery matrix](../tests/live_merge_matrix.py) completed in an existing private test repository. The tested Ermik revision was `c13827b1df0322eb018913d719627e5ae365864d`, which includes the closed-Issue fix and bounded read-only GitHub retries. The test used a local Docker image built from `examples/Dockerfile.demo` and an isolated state directory for each case. No repository credential was passed to a container.

The command sequence for each case was: create a scoped Issue and admission comment through GitHub REST, push one task branch, open a draft PR, run `review` on its exact HEAD, run `merge` with the selected process interruption, then read the PR, Issue, merge commit and default branch through GitHub REST. The script requires an explicitly named private repository and a maintainer credential in the environment; it does not print or save that credential.

| Case | Observed result |
| --- | --- |
| Same HEAD, no interruption | Squash merge completed; checks passed on the new default branch and the Issue closed. |
| Stop after saving the attempt, before the request | PR remained open. Rerun returned `incomplete`; no merge request was sent. |
| Stop after GitHub merged, before the merge command returned to `merge()` | Rerun verified the squash commit and completed without another merge request. The child CLI had returned to the fault hook; this tests loss of the result at the application boundary. |
| Stop before post-merge checks | Rerun checked the exact merge commit and completed. |
| Stop before saving the final result | Rerun repeated the checks and saved one complete result. |
| Move HEAD after review | Merge was rejected before a merge attempt. |
| Close the PR without merging | Merge was rejected without changing `main`. |
| Fail a check after squash merge | Command exited with code 2 and saved `incomplete` with no passing `main_ci`; rerun sent no merge request. |

All eight cases passed. A separate REST read confirmed each reviewed HEAD, PR and linked Issue state. For every merged case, it also confirmed the squash commit, its single parent equal to the reviewed base, and the final default-branch commit. Test Issues and PRs left open by rejection cases were closed, and their branches were removed. The merged history remains in the private test repository.

This exercise validates the maintainer merge boundary for this configuration. An attempt saved immediately before an unsent request remains pending for maintainer inspection; no automatic retry can safely distinguish that crash from a request still in flight. Other execution-host publication windows, host reboot, and separate-machine installation remain independent release checks.
