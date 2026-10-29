---
name: worker-task
description: Complete one admitted task in a bounded checkout and provide a verifiable result.
---

# Task execution

This checkout contains exactly one task. The task text below supplies the goal and permitted files. Follow stricter repository rules from `AGENTS.md` when present.

1. Read the goal, allowed paths, and acceptance criteria. Change only listed paths; do not edit `.git`, queue configuration, CI, tokens, or other tasks.
2. Make the smallest change that meets the goal. Check a positive case and the nearest negative case when the relevant environment is available.
3. Do not run `git push`, create a PR, change labels, or merge the branch. The controller validates files, runs configured CI, commits, and creates a draft PR.
4. If the task cannot be done within the allowed paths, do not bypass the boundary. Explain the blocker; the controller records it for the maintainer.
5. Review feedback applies to the same issue and PR. Fix it within the allowed scope. Earlier checks become stale after a new HEAD.

Command output alone is not proof. The changed files and independent controller and maintainer checks determine the result.
