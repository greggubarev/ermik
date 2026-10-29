---
name: maintainer-review
description: Review the exact HEAD of a server-created PR, request changes, or explicitly accept the result.
---

# Local maintainer review

1. Verify the issue, file scope, open PR, base branch, exact base and head SHAs, and full diff. A label or worker report alone is insufficient evidence.
2. Run `python3 -m ermik --config config.json review PR` locally. It repeats the configured checks in a container and records their results for the exact HEAD. Perform a separate manual gate when the goal requires one.
3. For a correction, write the specific expected result to a local file and run `request-changes PR --note-file PATH`. The server resumes work on the same branch and PR. Run `review PR` again after a new HEAD.
4. Once the goal is proven, run `merge PR` explicitly. It rejects a changed HEAD or base or an open change request, and repeats local checks on the new `main` after merge.
5. After a network failure, inspect the actual GitHub state and local record before retrying merge.

The author of their own PR must separately record a self-review; running `review` is not independent approval. The worker cannot accept their own result.
