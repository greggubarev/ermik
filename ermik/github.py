"""Narrow GitHub CLI boundary. All calls name the configured repository."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo: str, publish_repo: str | None = None,
                 publish_token_env: str | None = None):
        self.repo = repo
        self._publish_repo = publish_repo or repo
        self.publish_token_env = publish_token_env

    @property
    def publish_repo(self) -> str:
        return getattr(self, "_publish_repo", self.repo)

    def validate_publish_access(self) -> None:
        """Fail closed when the configured fork is not a permission boundary."""
        if self.publish_repo.casefold() == self.repo.casefold():
            return
        if not self.publish_token_env:
            raise GitHubError("fork publication credential is not configured")
        upstream = json.loads(self.command(["api", f"repos/{self.repo}"]))
        fork = json.loads(self.command(["api", f"repos/{self.publish_repo}"],
                                       credential_env=self.publish_token_env))
        if (not isinstance(upstream, dict) or not isinstance(fork, dict)
                or str(upstream.get("full_name", "")).casefold() != self.repo.casefold()
                or str(fork.get("full_name", "")).casefold() != self.publish_repo.casefold()
                or fork.get("fork") is not True
                or str((fork.get("parent") or {}).get("full_name", "")).casefold()
                != self.repo.casefold()):
            raise GitHubError("fork identity is unsafe")
        upstream_token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                          or self.command(["auth", "token"]).strip())
        fork_token = os.environ.get(self.publish_token_env)
        if not upstream_token or not fork_token or upstream_token == fork_token:
            raise GitHubError("publication credentials are unavailable or shared")
        upstream_sha = self._branch_sha(self.repo, upstream, None)
        fork_sha = self._branch_sha(self.publish_repo, fork, self.publish_token_env)
        if self._probe_branch_write(self.repo, upstream_sha, upstream_token):
            raise GitHubError("upstream credential can write the upstream repository")
        if self._probe_existing_branch_update(self.repo, upstream["default_branch"], upstream_token):
            raise GitHubError("upstream credential can write the upstream repository")
        # A fork-scoped token may receive 404 for a private upstream. In that
        # case the already verified fork parent establishes the target identity.
        upstream_hidden = self._fork_token_upstream_hidden(fork_token)
        if self._probe_branch_write(self.repo, upstream_sha, fork_token,
                                    allow_404=upstream_hidden):
            raise GitHubError("fork credential can write the upstream repository")
        if self._probe_existing_branch_update(self.repo, upstream["default_branch"],
                                              fork_token, allow_404=upstream_hidden):
            raise GitHubError("fork credential can write the upstream repository")
        if not self._probe_branch_write(self.publish_repo, fork_sha, fork_token):
            raise GitHubError("fork credential cannot write the publication repository")

    def _branch_sha(self, repo: str, metadata: dict, credential_env: str | None) -> str:
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch or branch.startswith("/"):
            raise GitHubError("publication repository has no usable default branch")
        args = ["api", f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}"]
        try:
            raw = (self.command(args, credential_env=credential_env) if credential_env
                   else self.command(args))
            ref = json.loads(raw)
        except (ValueError, TypeError):
            raise GitHubError("publication branch reference is invalid") from None
        obj = ref.get("object") if isinstance(ref, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if (not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{branch}"
                or not isinstance(sha, str) or len(sha) != 40
                or any(c not in "0123456789abcdefABCDEF" for c in sha)):
            raise GitHubError("publication branch reference is invalid")
        return sha

    def _fork_token_upstream_hidden(self, token: str) -> bool:
        request = Request(f"https://api.github.com/repos/{self.repo}",
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json",
                                   "User-Agent": "ermik-controller"})
        try:
            with urlopen(request, timeout=20) as response:
                upstream = json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                return True
            raise GitHubError("fork credential upstream visibility is inconclusive") from None
        except (URLError, OSError, ValueError, TypeError):
            raise GitHubError("fork credential upstream visibility is inconclusive") from None
        if (not isinstance(upstream, dict)
                or str(upstream.get("full_name", "")).casefold() != self.repo.casefold()):
            raise GitHubError("fork credential upstream identity is unsafe")
        return False

    def _probe_branch_write(self, repo: str, sha: str, token: str,
                            *, allow_404: bool = False) -> bool:
        """Return whether this credential can create a disposable branch."""
        branch = f"ermik-access-probe-{uuid.uuid4().hex}"
        base = f"https://api.github.com/repos/{repo}/git/refs"
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/vnd.github+json",
                   "User-Agent": "ermik-controller"}
        request = Request(base, data=json.dumps({"ref": f"refs/heads/{branch}",
                                                  "sha": sha}).encode(),
                          headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                status = response.status
        except HTTPError as exc:
            if exc.code == 403 or (allow_404 and exc.code == 404):
                return False
            raise GitHubError("publication capability probe was inconclusive") from None
        except (URLError, OSError, ValueError, TypeError):
            raise GitHubError("publication capability probe failed") from None
        if status != 201:
            raise GitHubError("publication capability probe was inconclusive")
        delete = Request(f"{base}/heads/{branch}", headers=headers, method="DELETE")
        try:
            with urlopen(delete, timeout=20) as response:
                if response.status != 204:
                    raise GitHubError("publication capability probe cleanup failed")
        except (HTTPError, URLError, OSError, ValueError, TypeError):
            raise GitHubError("publication capability probe cleanup failed") from None
        return True

    def _probe_existing_branch_update(self, repo: str, branch: str, token: str,
                                      *, allow_404: bool = False) -> bool:
        """Test update permission without changing an existing branch ref."""
        request = Request(
            f"https://api.github.com/repos/{repo}/git/refs/heads/{quote(branch, safe='')}",
            data=json.dumps({"sha": "0" * 40, "force": False}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": "ermik-controller"},
            method="PATCH")
        try:
            with urlopen(request, timeout=20):
                # The all-zero SHA is not an object. Any success is unsafe.
                raise GitHubError("publication capability probe was inconclusive")
        except HTTPError as exc:
            if exc.code == 403 or (allow_404 and exc.code == 404):
                return False
            if exc.code == 422:
                # GitHub checked the SHA after allowing the ref-update operation.
                return True
            raise GitHubError("publication capability probe was inconclusive") from None
        except (URLError, OSError, ValueError, TypeError):
            raise GitHubError("publication capability probe failed") from None

    def command(self, args: list[str], *, timeout: int = 90,
                credential_env: str | None = None) -> str:
        env = None
        if credential_env is not None:
            token = os.environ.get(credential_env)
            if not token:
                raise GitHubError("fork publication credential is unavailable")
            if token in (os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
                raise GitHubError("fork publication credential must differ from upstream credential")
            env = os.environ.copy()
            env["GH_TOKEN"] = token
            env.pop("GITHUB_TOKEN", None)
        try:
            result = subprocess.run(["gh", *args], text=True, encoding="utf-8",
                                    errors="replace", capture_output=True,
                                    timeout=timeout, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubError("GitHub command unavailable or timed out") from exc
        if result.returncode:
            # CLI errors can contain endpoint details; do not echo credentials.
            raise GitHubError(f"GitHub command failed (exit {result.returncode})")
        return result.stdout

    def api(self, endpoint: str) -> Any:
        return json.loads(self.command(["api", f"repos/{self.repo}/{endpoint}"]))

    def _pages(self, endpoint: str) -> list[dict]:
        """Read bounded REST pages without version-specific gh pagination flags."""
        rows: list[dict] = []
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, 1001):
            batch = self.api(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise GitHubError("GitHub page is not an array")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GitHubError("GitHub page listing reached safety limit")

    def identity(self) -> str:
        value = json.loads(self.command(["api", "user"]))
        return value["login"]

    def issue(self, number: int) -> dict:
        value = self.api(f"issues/{number}")
        if "pull_request" in value or value.get("state") != "open":
            raise GitHubError("issue is absent, closed, or a pull request")
        return value

    def ready_issues(self) -> list[int]:
        rows = self._pages("issues?state=open&labels=queue%3Aworker%2Cstate%3Aready")
        return sorted(row["number"] for row in rows
                      if "pull_request" not in row)

    def label(self, number: int, *, add: str | None = None,
              remove: str | None = None) -> None:
        args = ["issue", "edit", str(number), "-R", self.repo]
        if add:
            args += ["--add-label", add]
        if remove:
            args += ["--remove-label", remove]
        self.command(args)

    def comment(self, number: int, body: str) -> None:
        self.command(["api", "--method", "POST", f"repos/{self.repo}/issues/{number}/comments",
                      "-f", f"body={body}"])

    def comments(self, number: int) -> list[dict]:
        return self._pages(f"issues/{number}/comments")

    def pr(self, number: int) -> dict:
        raw = self.api(f"pulls/{number}")
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        head_repo = head.get("repo") or {}
        files = self._pages(f"pulls/{number}/files")
        return {"number": raw["number"],
                "state": "MERGED" if raw.get("merged_at") else raw["state"].upper(),
                "isDraft": raw["draft"],
                "baseRefName": base["ref"], "baseRefOid": base["sha"],
                "headRefName": head["ref"], "headRefOid": head["sha"],
                "headRepository": {"nameWithOwner": head_repo.get("full_name")},
                "body": raw.get("body"),
                "files": [{"path": file["filename"]} for file in files],
                "changedFiles": raw["changed_files"],
                "mergeCommit": {"oid": raw["merge_commit_sha"]}
                if raw.get("merge_commit_sha") else None,
                "mergedAt": raw.get("merged_at")}

    def validate_pr_identity(self, pr: dict, *, branch: str, base_branch: str,
                             number: int | None = None,
                             head_sha: str | None = None, base_sha: str | None = None,
                             draft: bool | None = None, state: str | None = None) -> None:
        """Require the expected source and target before acting on a PR."""
        publish_repo = getattr(self, "publish_repo", self.repo)
        head_repo = pr.get("headRepository")
        if (not isinstance(head_repo, dict)
                or not isinstance(head_repo.get("nameWithOwner"), str)
                or head_repo["nameWithOwner"].casefold() != publish_repo.casefold()
                or number is not None and pr.get("number") != number
                or pr.get("headRefName") != branch
                or pr.get("baseRefName") != base_branch
                or head_sha is not None and pr.get("headRefOid") != head_sha
                or base_sha is not None and pr.get("baseRefOid") != base_sha
                or draft is not None and pr.get("isDraft") is not draft
                or state is not None and pr.get("state") != state):
            raise GitHubError("PR identity does not match the expected repository, branch, or state")

    def open_pr(self, branch: str, body: str, title: str, base_branch: str) -> int:
        owner = self.publish_repo.split("/", 1)[0]
        head = f"{owner}:{branch}" if self.publish_repo.casefold() != self.repo.casefold() else branch
        raw = self.command(["pr", "create", "-R", self.repo, "--base", base_branch,
                            "--head", head, "--draft", "--title", title,
                            "--body", body], timeout=120)
        return int(raw.rstrip("/").split("/")[-1])

    def find_branch_pr(self, branch: str, base_branch: str | None = None) -> int | None:
        owner = self.publish_repo.split("/", 1)[0]
        rows = self._pages(f"pulls?state=all&head={owner}%3A{branch}")
        found = [row["number"] for row in rows
                 if (row.get("head") or {}).get("ref") == branch
                 and ((row.get("head") or {}).get("repo") or {}).get("full_name", "").casefold()
                 == self.publish_repo.casefold()
                 and (base_branch is None or (row.get("base") or {}).get("ref") == base_branch)]
        if len(found) > 1:
            raise GitHubError("multiple PRs for task branch")
        return found[0] if found else None
