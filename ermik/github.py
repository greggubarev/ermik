"""Narrow GitHub CLI boundary. All calls name the configured repository."""

from __future__ import annotations

import json
import hashlib
import functools
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    pass


_PROBE_LOCK = threading.RLock()


def _serialized_probe(method):
    @functools.wraps(method)
    def locked(*args, **kwargs):
        with _PROBE_LOCK:
            return method(*args, **kwargs)
    return locked


def _read_api_command(args: list[str]) -> bool:
    """Only an unambiguously read-only gh api invocation may be repeated."""
    if not args or args[0] != "api":
        return False
    method = "GET"
    for index, arg in enumerate(args[1:], 1):
        if arg in ("--method", "-X"):
            if index + 1 >= len(args):
                return False
            method = args[index + 1].upper()
        elif arg.startswith("--method="):
            method = arg.partition("=")[2].upper()
        elif arg.startswith("-X") and len(arg) > 2:
            method = arg[2:].upper()
        elif (arg in ("-f", "-F", "--field", "--raw-field", "--input")
              or arg.startswith(("-f", "-F", "--field=", "--raw-field=", "--input="))):
            # gh api sends POST by default when fields or input are supplied.
            return False
        elif arg == "graphql" or arg.endswith("/graphql"):
            return False
        if method != "GET":
            return False
    return method == "GET"


_GH_HTTP_STATUS = re.compile(
    r"\bHTTP(?:/\d(?:\.\d)?)?\s*[: ]\s*(\d{3})\b", re.IGNORECASE,
)
_GH_SERVER_ERROR = re.compile(
    r"\b(?:500\s+Internal Server Error|502\s+Bad Gateway"
    r"|503\s+Service Unavailable|504\s+Gateway Timeout)\b", re.IGNORECASE,
)
_GH_TRANSPORT_ERROR = re.compile(
    r"\b(?:connection (?:reset|refused|timed out|closed)|context deadline exceeded"
    r"|TLS handshake timeout|temporary failure in name resolution|no such host"
    r"|network is unreachable|i/o timeout|unexpected EOF)\b",
    re.IGNORECASE,
)


def _retryable_gh_error(stderr: str) -> bool:
    """An HTTP response takes precedence over transport text in the same error."""
    statuses = _GH_HTTP_STATUS.findall(stderr)
    if statuses:
        return all(int(status) in (500, 502, 503, 504) for status in statuses)
    return bool(_GH_SERVER_ERROR.search(stderr) or _GH_TRANSPORT_ERROR.search(stderr))


class GitHub:
    def __init__(self, repo: str, publish_repo: str | None = None,
                 publish_token_env: str | None = None,
                 probe_state_dir: Path | None = None):
        self.repo = repo
        self._publish_repo = publish_repo or repo
        self.publish_token_env = publish_token_env
        self.probe_state_dir = Path(probe_state_dir) if probe_state_dir is not None else None

    @property
    def publish_repo(self) -> str:
        return getattr(self, "_publish_repo", self.repo)

    def inspect_publish_identity(self) -> tuple[dict, str, str]:
        """Read and verify the configured upstream, fork, and default refs."""
        if self.publish_repo.casefold() == self.repo.casefold():
            raise GitHubError("fork publication is not configured")
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
        upstream_sha = self._branch_sha(self.repo, upstream, None)
        fork_sha = self._branch_sha(self.publish_repo, fork, self.publish_token_env)
        return upstream, upstream_sha, fork_sha

    def validate_publish_access(self) -> None:
        """Fail closed when the configured fork is not a permission boundary."""
        if self.publish_repo.casefold() == self.repo.casefold():
            return
        self.reconcile_probe_intents()
        upstream, upstream_sha, fork_sha = self.inspect_publish_identity()
        upstream_token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                          or self.command(["auth", "token"]).strip())
        fork_token = os.environ.get(self.publish_token_env)
        if not upstream_token or not fork_token or upstream_token == fork_token:
            raise GitHubError("publication credentials are unavailable or shared")
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

    @staticmethod
    def _credential_identity(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _probe_intent_dir(self) -> Path | None:
        if self.probe_state_dir is None:
            return None
        return self.probe_state_dir / "probe-intents"

    def _save_probe_intent(self, repo: str, ref: str, sha: str,
                           token: str) -> Path:
        directory = self._probe_intent_dir()
        if directory is None:
            raise GitHubError("publication probe requires a durable state directory")
        if (repo.casefold() not in (self.repo.casefold(), self.publish_repo.casefold())
                or not re.fullmatch(r"refs/heads/ermik-access-probe-[0-9a-f]{32}", ref)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
            raise GitHubError("publication probe intent identity invalid")
        try:
            was_present = directory.exists()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise GitHubError("publication probe intent directory invalid")
            if not was_present:
                self._sync_directory(directory.parent)
            count = sum(1 for _ in directory.iterdir())
        except OSError:
            raise GitHubError("publication probe intent could not be persisted") from None
        if count >= 32:
            raise GitHubError("too many unresolved publication probe intents")
        path = directory / f"{ref.removeprefix('refs/heads/')}.json"
        data = {"repo": repo, "ref": ref, "sha": sha,
                "credential": self._credential_identity(token),
                "created": time.time()}
        try:
            with path.open("x", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory(directory)
        except OSError:
            raise GitHubError("publication probe intent could not be persisted") from None
        return path

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        # Directory fsync is available on POSIX; Windows FlushFileBuffers on a
        # directory handle is not supported by Python's ordinary open().
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _remove_probe_intent(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink()
            self._sync_directory(path.parent)
        except OSError:
            raise GitHubError("publication probe intent cleanup failed") from None

    @_serialized_probe
    def reconcile_probe_intents(self) -> None:
        """Resolve only locally recorded probes under their original credential."""
        directory = self._probe_intent_dir()
        if directory is None or not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise GitHubError("publication probe intents unreadable")
        try:
            paths = sorted(directory.iterdir())
        except OSError:
            raise GitHubError("publication probe intents unreadable") from None
        if len(paths) > 32:
            raise GitHubError("too many unresolved publication probe intents")
        if not paths:
            return
        upstream_token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                          or self.command(["auth", "token"]).strip())
        fork_token = os.environ.get(self.publish_token_env) if self.publish_token_env else None
        for path in paths:
            try:
                if not path.is_file() or path.is_symlink() or path.suffix != ".json":
                    raise ValueError
                row = json.loads(path.read_text(encoding="utf-8"))
                repo, ref, sha, credential = (row[k] for k in
                                               ("repo", "ref", "sha", "credential"))
                created = row["created"]
                if (not isinstance(repo, str)
                        or repo.casefold() not in (self.repo.casefold(),
                                                   self.publish_repo.casefold())
                        or not isinstance(ref, str)
                        or not re.fullmatch(r"refs/heads/ermik-access-probe-[0-9a-f]{32}", ref)
                        or path.name != f"{ref.removeprefix('refs/heads/')}.json"
                        or not isinstance(sha, str)
                        or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
                        or not isinstance(credential, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", credential)
                        or not isinstance(created, (float, int))
                        or isinstance(created, bool)
                        or created > time.time() + 300
                        or time.time() - created > 7 * 86400):
                    raise ValueError
            except (OSError, ValueError, TypeError, KeyError):
                raise GitHubError("publication probe intent invalid or stale") from None
            candidates = [token for token in (upstream_token, fork_token)
                          if token and self._credential_identity(token) == credential]
            if len(candidates) != 1:
                raise GitHubError("publication probe credential changed")
            token = candidates[0]
            headers = {"Authorization": f"Bearer {token}",
                       "Accept": "application/vnd.github+json",
                       "User-Agent": "ermik-controller"}
            observed = self._probe_ref_sha(repo, ref, headers, sha)
            if observed is None:
                self._remove_probe_intent(path)
                continue
            if observed.casefold() != sha.casefold():
                raise GitHubError("publication probe ref changed; cleanup blocked")
            # Re-read at the point of deletion. GitHub's DELETE ref endpoint
            # has no SHA precondition, so a concurrent force update remains a
            # server-side race that cannot be eliminated by this client.
            if self._probe_ref_sha(repo, ref, headers, sha) != observed:
                raise GitHubError("publication probe ref changed; cleanup blocked")
            self._delete_probe_ref(repo, ref, sha, headers)
            self._remove_probe_intent(path)

    def _delete_probe_ref(self, repo: str, ref: str, sha: str,
                          headers: dict[str, str]) -> None:
        if not re.fullmatch(r"refs/heads/ermik-access-probe-[0-9a-f]{32}", ref):
            raise GitHubError("publication probe cleanup target invalid")
        identity = f"{repo}/{ref} (expected {sha})"
        branch = ref.removeprefix("refs/heads/")
        request = Request(f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
                          headers=headers, method="DELETE")
        try:
            with urlopen(request, timeout=20) as response:
                if response.status != 204:
                    raise GitHubError(f"publication capability probe cleanup unconfirmed: {identity}")
        except HTTPError:
            raise GitHubError(f"publication capability probe cleanup failed: {identity}") from None
        except (URLError, OSError):
            if self._probe_ref_sha(repo, ref, headers, sha) is not None:
                raise GitHubError(f"publication capability probe cleanup unconfirmed: {identity}") from None
        except (ValueError, TypeError):
            raise GitHubError(f"publication capability probe cleanup unconfirmed: {identity}") from None

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

    @_serialized_probe
    def _probe_branch_write(self, repo: str, sha: str, token: str,
                            *, allow_404: bool = False) -> bool:
        """Return whether this credential can create a disposable branch."""
        branch = f"ermik-access-probe-{uuid.uuid4().hex}"
        ref = f"refs/heads/{branch}"
        identity = f"{repo}/{ref} (expected {sha})"
        base = f"https://api.github.com/repos/{repo}/git/refs"
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/vnd.github+json",
                   "User-Agent": "ermik-controller"}
        request = Request(base, data=json.dumps({"ref": ref,
                                                  "sha": sha}).encode(),
                          headers=headers, method="POST")
        intent = self._save_probe_intent(repo, ref, sha, token)
        try:
            with urlopen(request, timeout=20) as response:
                status = response.status
        except HTTPError as exc:
            if exc.code == 403 or (allow_404 and exc.code == 404):
                self._remove_probe_intent(intent)
                return False
            if exc.code == 422:
                # GitHub rejected creation (for example, the ref already
                # exists). The ref was never ours to remove on replay.
                self._remove_probe_intent(intent)
            raise GitHubError("publication capability probe was inconclusive") from None
        except (URLError, OSError):
            observed = self._probe_ref_sha(repo, ref, headers, sha)
            if observed is None:
                raise GitHubError(f"publication capability probe POST unconfirmed: {identity}") from None
            if observed.casefold() != sha.casefold():
                raise GitHubError(f"publication capability probe ref changed: {identity}") from None
            status = 201
        except (ValueError, TypeError):
            raise GitHubError(f"publication capability probe POST unconfirmed: {identity}") from None
        if status != 201:
            raise GitHubError("publication capability probe was inconclusive")
        observed = self._probe_ref_sha(repo, ref, headers, sha)
        if observed is None or observed.casefold() != sha.casefold():
            raise GitHubError(f"publication capability probe ref changed: {identity}")
        self._delete_probe_ref(repo, ref, sha, headers)
        self._remove_probe_intent(intent)
        return True

    def _probe_ref_sha(self, repo: str, ref: str, headers: dict[str, str],
                       expected_sha: str) -> str | None:
        """Read the exact probe ref; confirm repo visibility before accepting 404."""
        identity = f"{repo}/{ref} (expected {expected_sha})"
        request = Request(f"https://api.github.com/repos/{repo}/git/ref/"
                          f"{quote(ref.removeprefix('refs/'), safe='/')}", headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                row = json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                self._confirm_probe_repo_visible(repo, headers, identity)
                return None
            raise GitHubError(f"publication capability probe ref unreadable: {identity}") from None
        except (URLError, OSError, ValueError, TypeError):
            raise GitHubError(f"publication capability probe ref unreadable: {identity}") from None
        obj = row.get("object") if isinstance(row, dict) else None
        observed = obj.get("sha") if isinstance(obj, dict) else None
        if (not isinstance(row, dict) or row.get("ref") != ref
                or not isinstance(observed, str) or len(observed) != 40
                or any(c not in "0123456789abcdefABCDEF" for c in observed)):
            raise GitHubError(f"publication capability probe ref invalid: {identity}")
        return observed

    def _confirm_probe_repo_visible(self, repo: str, headers: dict[str, str],
                                    identity: str) -> None:
        request = Request(f"https://api.github.com/repos/{repo}", headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                metadata = json.load(response)
        except (HTTPError, URLError, OSError, ValueError, TypeError):
            raise GitHubError(f"publication capability probe repository visibility unconfirmed: "
                              f"{identity}") from None
        if (not isinstance(metadata, dict)
                or str(metadata.get("full_name", "")).casefold() != repo.casefold()):
            raise GitHubError(f"publication capability probe repository identity changed: "
                              f"{identity}")
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch or branch.startswith("/"):
            raise GitHubError(f"publication capability probe default ref invalid: {identity}")
        default_ref = f"refs/heads/{branch}"
        request = Request(f"https://api.github.com/repos/{repo}/git/ref/heads/"
                          f"{quote(branch, safe='')}", headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                row = json.load(response)
        except (HTTPError, URLError, OSError, ValueError, TypeError):
            raise GitHubError(f"publication capability probe default ref unreadable: "
                              f"{identity}") from None
        obj = row.get("object") if isinstance(row, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if (not isinstance(row, dict) or row.get("ref") != default_ref
                or not isinstance(sha, str) or len(sha) != 40
                or any(c not in "0123456789abcdefABCDEF" for c in sha)):
            raise GitHubError(f"publication capability probe default ref invalid: {identity}")

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
        read_api = _read_api_command(args)
        attempts = 3 if read_api else 1
        for attempt in range(attempts):
            try:
                result = subprocess.run(["gh", *args], text=True, encoding="utf-8",
                                        errors="replace", capture_output=True,
                                        timeout=timeout, check=False, env=env)
            except subprocess.TimeoutExpired:
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                # TimeoutExpired can retain the command and captured output.
                raise GitHubError("GitHub command unavailable or timed out") from None
            except OSError as exc:
                # A failed launch may be transient; a missing or inaccessible gh
                # executable cannot be fixed by repeating the same command.
                if (attempt + 1 < attempts
                        and not isinstance(exc, (FileNotFoundError, PermissionError))):
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise GitHubError("GitHub command unavailable or timed out") from None
            if result.returncode:
                # Inspect stderr only for retry classification; never expose it.
                if (attempt + 1 < attempts and
                        _retryable_gh_error(result.stderr or "")):
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise GitHubError(f"GitHub command failed (exit {result.returncode})")
            return result.stdout
        raise GitHubError("GitHub command unavailable or timed out")

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

    def issue(self, number: int, *, allow_closed: bool = False) -> dict:
        value = self.api(f"issues/{number}")
        if ("pull_request" in value or value.get("state") not in
                ({"open", "closed"} if allow_closed else {"open"})):
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
                "createdAt": raw.get("created_at"),
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
