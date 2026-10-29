"""One trusted controller, disposable worktrees, and containerized task execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .config import Config
from .github import GitHub, GitHubError
from .policy import IssueSpec, parse_issue, validate_changed_paths
from .observability import resource_guard
from .resources import recover_stale_worktrees
from .providers import (available_capacity, claim_model, choose_model, model_spec,
                        record_exhaustion, release_model)
from .recovery import (RecoveryClaim, activate_claim, claim_due, complete_claim,
                       record_failure)
from .usage import parse_worker_usage
from . import state


REWORK = re.compile(r"^SEMI_REWORK issue=(\d+) head=([0-9a-f]{40})$", re.MULTILINE)
REBASE_ONLY = re.compile(r"^SEMI_REBASE_ONLY$", re.MULTILINE)
STATUS_ACTIVE = ("running", "correcting", "awaiting_review")
MAX_CONTAINER_OUTPUT = 1024 * 1024
_CLEANED_NAMESPACES: set[str] = set()
_CONTAINER_LOCK = threading.Lock()
_PUBLISH_HELPER_LOCK = threading.Lock()
_MIRROR_LOCK = threading.RLock()


def _draft_pr(cfg: Config, gh: GitHub, *, issue: int, branch: str,
              head: str, base: str, paths: list[str], proof: dict,
              model: str | None, body_sha256: str, title: str) -> int:
    """Publish only a checked commit; the broker validates remote identity again."""
    if cfg.draft_pr_socket is None:
        body = (f"Closes #{issue}\n\nAllowed paths: {', '.join(paths)}\n"
                f"Local checks passed on `{head}`.\nReview the full diff and acceptance criteria.")
        return gh.open_pr(branch, body, title, cfg.base_branch)
    root = cfg.draft_pr_evidence_dir or cfg.state_dir / "evidence"
    if cfg.draft_pr_evidence_dir is not None:
        if os.name != "posix" or root.is_symlink() or not root.is_dir():
            raise WorkflowError("shared PR evidence directory is unavailable")
        info = root.stat()
        if (info.st_uid != os.geteuid() or not info.st_mode & stat.S_ISGID
                or info.st_mode & 0o022):
            raise WorkflowError("shared PR evidence directory has unsafe ownership")
    evidence = root / str(issue)
    evidence.mkdir(parents=True, exist_ok=True,
                   mode=0o750 if cfg.draft_pr_evidence_dir else 0o700)
    if evidence.is_symlink():
        raise WorkflowError("PR evidence directory is a symlink")
    if cfg.draft_pr_evidence_dir:
        info = evidence.stat()
        if info.st_uid != os.geteuid() or info.st_gid != root.stat().st_gid:
            raise WorkflowError("PR evidence directory has unsafe ownership")
        evidence.chmod(0o750)
    path = evidence / f"{head}.json"
    if path.is_symlink():
        raise WorkflowError("PR evidence file is a symlink")
    prepublication = {"issue": issue, "branch": branch, "head": head,
                      "base": base, "body_sha256": body_sha256,
                      "paths": sorted(paths), "ci": proof, "model": model}
    path.write_text(json.dumps(prepublication, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    path.chmod(0o640 if cfg.draft_pr_evidence_dir else 0o600)
    request = {"op": "create_draft", "issue": issue, "branch": branch,
               "head_sha": head, "base_sha": base}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(45)
            client.connect(str(cfg.draft_pr_socket))
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            with client.makefile("rb") as response:
                line = response.readline(4096)
        answer = json.loads(line)
        if (not isinstance(answer, dict) or answer.get("ok") is not True
                or type(answer.get("pr")) is not int or answer["pr"] <= 0):
            raise GitHubError("draft PR broker refused publication")
        return answer["pr"]
    except (OSError, ValueError) as exc:
        raise GitHubError("draft PR broker unavailable") from exc


def _admission_marker(issue: int, spec: IssueSpec) -> str:
    scope = hashlib.sha256(json.dumps(spec.allowed_paths, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"SEMI_ADMIT issue={issue} body={spec.body_sha256} scope={scope}"


def _approved_on_github(gh: GitHub, cfg: Config, issue: int, spec: IssueSpec) -> bool:
    marker = _admission_marker(issue, spec)
    return any((comment.get("body") or "").splitlines()[0:1] == [marker]
               and comment.get("user", {}).get("login") == cfg.maintainer
               for comment in gh.comments(issue))


class WorkflowError(RuntimeError):
    pass


class TransientFailure(WorkflowError):
    pass


class WorkerFailure(WorkflowError):
    def __init__(self, returncode: int, usage: dict | None = None):
        self.returncode = returncode
        self.usage = usage
        super().__init__(f"worker command failed (exit {returncode})")


def checked(args: list[str], *, cwd: Path | None = None,
            timeout: int = 300) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransientFailure(f"{args[0]} unavailable or timed out") from exc
    if result.returncode:
        raise WorkflowError(f"{args[0]} command failed (exit {result.returncode})")
    return result.stdout.strip()


def _run_capped(args: list[str], timeout: int) -> tuple[int, bytes]:
    """Drain container output without retaining an unbounded transcript."""
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = bytearray()
    overflow = False

    def drain() -> None:
        nonlocal overflow
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read(8192), b""):
            remaining = MAX_CONTAINER_OUTPUT - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow = True

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=5)
        raise TransientFailure("Docker command timed out")
    reader.join(timeout=5)
    if reader.is_alive():
        raise WorkflowError("Docker output pipe did not close")
    if overflow:
        raise WorkflowError("Docker output exceeded 1 MiB limit")
    return process.returncode, bytes(output)


def _container_namespace(cfg: Config) -> str:
    identity = f"{cfg.repo}\n{cfg.state_dir.resolve()}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


def _cleanup_containers(cfg: Config, *, mark: bool = False) -> None:
    """Remove only this state directory's orphan containers before new work."""
    namespace = _container_namespace(cfg)
    with _CONTAINER_LOCK:
        if namespace in _CLEANED_NAMESPACES:
            return
        marker = cfg.state_dir / "container-namespace"
        if marker.is_symlink():
            raise WorkflowError("container namespace marker changed")
        if not marker.exists():
            if not mark:
                return
            state.initialize(cfg.state_dir)
            marker.write_text(namespace + "\n", encoding="ascii")
            if os.name != "nt":
                os.chmod(marker, 0o600)
            _CLEANED_NAMESPACES.add(namespace)
            return
        if marker.is_symlink() or marker.read_text(encoding="ascii").strip() != namespace:
            raise WorkflowError("container namespace marker changed")
        listing = checked(["docker", "ps", "--all", "--quiet", "--filter",
                           f"label=ermik.namespace={namespace}"], timeout=30)
        identifiers = listing.splitlines()
        if len(identifiers) > 100 or any(not re.fullmatch(r"[0-9a-f]{12,64}", item)
                                          for item in identifiers):
            raise WorkflowError("container listing is invalid or unexpectedly large")
        for identifier in identifiers:
            checked(["docker", "rm", "--force", identifier], timeout=30)
        _CLEANED_NAMESPACES.add(namespace)


def _force_remove_container(cfg: Config, name: str) -> None:
    try:
        checked(["docker", "rm", "--force", name], timeout=30)
    except WorkflowError:
        # Docker may have removed it already. Retry the labelled cleanup next cycle.
        with _CONTAINER_LOCK:
            _CLEANED_NAMESPACES.discard(_container_namespace(cfg))


def git(path: Path, *args: str, timeout: int = 300) -> str:
    return checked(["git", "-C", str(path), *args], timeout=timeout)


def _publish_git(cfg: Config, path: Path, *args: str, timeout: int = 300) -> str:
    """Use only the fork credential for fork Git traffic, never an ambient helper."""
    if not cfg.publish_repo:
        return git(path, *args, timeout=timeout)
    token_name = cfg.publish_token_env
    if not token_name or not os.environ.get(token_name):
        raise WorkflowError("fork publication credential is unavailable")
    helper = cfg.state_dir / "fork-askpass"
    if os.name == "nt":
        raise WorkflowError("fork publication requires a POSIX server")
    state.initialize(cfg.state_dir)
    script = (
        "#!" + sys.executable + "\n"
        "import os, sys\n"
        "print('x-access-token' if 'Username' in sys.argv[1] "
        "else os.environ[os.environ['ERMIK_PUBLISH_TOKEN_ENV']])\n"
    )
    with _PUBLISH_HELPER_LOCK:
        if helper.is_symlink():
            raise WorkflowError("fork credential helper was replaced")
        if helper.exists():
            if helper.read_text(encoding="utf-8") != script:
                raise WorkflowError("fork credential helper changed")
        else:
            with helper.open("x", encoding="utf-8") as output:
                output.write(script)
            helper.chmod(0o700)
    env = os.environ.copy()
    env["ERMIK_PUBLISH_TOKEN_ENV"] = token_name
    env["GIT_ASKPASS"] = str(helper)
    env["GIT_TERMINAL_PROMPT"] = "0"
    url = f"https://github.com/{cfg.publish_repo}.git"
    try:
        operation, *tail = args
        flags = [part for part in tail if part.startswith("-")]
        refs = [part for part in tail if not part.startswith("-")]
        result = subprocess.run(
            ["git", "-C", str(path), "-c", "credential.helper=", operation,
             *flags, url, *refs], text=True, capture_output=True, timeout=timeout,
            check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransientFailure("fork Git command unavailable or timed out") from exc
    if result.returncode:
        if operation == "push":
            raise TransientFailure("fork Git push outcome is unconfirmed")
        raise WorkflowError(f"fork Git command failed (exit {result.returncode})")
    return result.stdout.strip()


def labels(issue: dict) -> set[str]:
    return {row["name"] for row in issue.get("labels", [])}


def _routed_models(cfg: Config, issue: dict):
    """Apply the issue's routing hint to trusted configured candidates."""
    routes = {name for name in labels(issue) if name.startswith("route:")}
    if len(routes) > 1:
        raise WorkflowError("conflicting provider route labels")
    route = next(iter(routes), "route:auto")
    if route == "route:auto":
        return cfg.models
    provider_id = cfg.routes.get(route)
    if provider_id is None:
        raise WorkflowError(f"provider route {route} is not configured")
    candidates = tuple(model for model in cfg.models
                       if model.provider_id == provider_id)
    if not candidates:
        raise WorkflowError(f"requested provider {provider_id} is not configured")
    return candidates


def _route_provider(conn: sqlite3.Connection, cfg: Config, issue: dict,
                    reserved: dict[str, int]) -> str | None:
    """Find a free provider slot for scheduling this issue in this cycle."""
    try:
        candidates = _routed_models(cfg, issue)
    except WorkflowError:
        # Admit the invalid route so the execution path records a blocked job.
        return "__invalid_route__"
    if not cfg.models:
        return ""
    seen = set()
    for candidate in candidates:
        provider_id = candidate.provider_id
        if provider_id in seen:
            continue
        seen.add(provider_id)
        if available_capacity(conn, (candidate,)) > reserved.get(provider_id, 0):
            return provider_id
    return None


def _github(cfg: Config) -> GitHub:
    return (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
            if cfg.publish_repo else GitHub(cfg.repo))


def main_sha(gh: GitHub, cfg: Config) -> str:
    ref = gh.api(f"git/ref/heads/{cfg.base_branch}")
    sha = ref["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise WorkflowError("base branch SHA invalid")
    return sha


def _require_pr(gh: GitHub, pr: dict, cfg: Config, *, branch: str,
                number: int | None = None, head_sha: str | None = None,
                state_name: str = "OPEN") -> None:
    try:
        gh.validate_pr_identity(pr, branch=branch, base_branch=cfg.base_branch,
                                number=number, head_sha=head_sha,
                                state=state_name)
    except GitHubError as exc:
        raise WorkflowError("PR identity changed") from exc


def mirror(cfg: Config) -> Path:
    """One cached clone; no per-issue full repository copy."""
    with _MIRROR_LOCK:
        return _mirror_locked(cfg)


def _mirror_locked(cfg: Config) -> Path:
    root = cfg.workspace / "source"
    if not root.exists():
        cfg.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        checked(["git", "clone", "--no-checkout",
                 f"https://github.com/{cfg.repo}.git", str(root)], timeout=300)
    actual = git(root, "remote", "get-url", "origin")
    if actual.rstrip("/").removesuffix(".git") != f"https://github.com/{cfg.repo}":
        raise WorkflowError("cached clone belongs to another repository")
    if os.name == "nt":
        # A Linux check container must see the same LF bytes as the Git index.
        git(root, "config", "core.autocrlf", "false")
        git(root, "config", "core.eol", "lf")
    git(root, "fetch", "--no-tags", "origin", cfg.base_branch, timeout=300)
    return root


def _checkout(cfg: Config, issue: int, branch: str, *, correction: bool) -> Path:
    with _MIRROR_LOCK:
        return _checkout_locked(cfg, issue, branch, correction=correction)


def _checkout_locked(cfg: Config, issue: int, branch: str, *, correction: bool) -> Path:
    source = mirror(cfg)
    path = cfg.workspace / "jobs" / f"issue-{issue}"
    if path.exists():
        raise WorkflowError("existing task checkout requires inspection")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if correction:
        if cfg.publish_repo:
            _publish_git(cfg, source, "fetch", "--no-tags", branch)
            git(source, "branch", "-f", branch, "FETCH_HEAD")
        else:
            git(source, "fetch", "--no-tags", "origin", branch)
            git(source, "branch", "-f", branch, f"origin/{branch}")
        git(source, "worktree", "add", str(path), branch)
    else:
        if git(source, "branch", "--list", branch):
            remote = (_publish_git(cfg, source, "ls-remote", "--heads", branch)
                      if cfg.publish_repo else git(source, "ls-remote", "--heads", "origin", branch))
            if remote:
                raise WorkflowError("task branch exists remotely without a verified PR")
            git(source, "branch", "-f", branch, f"origin/{cfg.base_branch}")
            git(source, "worktree", "add", str(path), branch)
        else:
            git(source, "worktree", "add", "-b", branch, str(path),
                f"origin/{cfg.base_branch}")
    return path


def _remove_checkout(cfg: Config, path: Path) -> None:
    # Never force removal: a failed cleanup leaves evidence for inspection.
    with _MIRROR_LOCK:
        git(cfg.workspace / "source", "worktree", "remove", str(path))


def _check_git_snapshot(cfg: Config, checkout: Path, directory: Path) -> tuple[Path, Path]:
    """Expose only the base branch and this checkout's commit to offline checks."""
    source = cfg.workspace / "source"
    common_git = source / ".git"
    if not common_git.is_dir() or common_git.is_symlink():
        raise WorkflowError("shared Git metadata is unavailable for local checks")
    head = git(checkout, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise WorkflowError("checkout HEAD is invalid")
    snapshot = directory / "snapshot"
    checked(["git", "clone", "--no-local", "--no-checkout", "--single-branch",
             "--no-tags", "--branch", cfg.base_branch, str(source), str(snapshot)],
            timeout=300)
    git(snapshot, "fetch", "--no-tags", str(source), head, timeout=300)
    git(snapshot, "symbolic-ref", "HEAD", "refs/heads/ci-head")
    git(snapshot, "update-ref", "refs/heads/ci-head", head)
    git(snapshot, "reset", "--mixed", "HEAD")
    git_file = directory / "gitfile"
    git_file.write_text("gitdir: /ci-git\n", encoding="ascii")
    return snapshot / ".git", git_file


def _docker(cfg: Config, checkout: Path, command: tuple[str, ...], *,
            issue_file: Path | None, network: bool, timeout: int,
            log_path: Path | None = None, selected_model=None) -> dict | None:
    _cleanup_containers(cfg, mark=True)
    namespace = _container_namespace(cfg)
    container_name = f"ermik-{namespace[:12]}-{uuid.uuid4().hex[:16]}"
    uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
    args = ["docker", "run", "--rm", "--name", container_name,
            "--label", f"ermik.namespace={namespace}",
            "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=256",
            "--memory=2g", "--cpus=2", "--user", uid,
            "--network", "bridge" if network else "none",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--mount", f"type=bind,source={checkout},target=/work",
            "--workdir", "/work", "--env", "PYTHONDONTWRITEBYTECODE=1"]
    with ExitStack() as stack:
        if issue_file is None:
            temporary = Path(stack.enter_context(
                tempfile.TemporaryDirectory(prefix="check-git-", dir=cfg.state_dir)))
            snapshot_git, git_file = _check_git_snapshot(cfg, checkout, temporary)
            args += ["--mount", f"type=bind,source={git_file},target=/work/.git,readonly",
                     "--mount", f"type=bind,source={snapshot_git},target=/ci-git,readonly",
                     "--env", "GIT_OPTIONAL_LOCKS=0"]
            if os.name == "nt":
                # Docker Desktop exposes Windows bind mounts as root-owned files.
                args += ["--env", "GIT_CONFIG_COUNT=1",
                         "--env", "GIT_CONFIG_KEY_0=safe.directory",
                         "--env", "GIT_CONFIG_VALUE_0=/work"]
        else:
            args += ["--mount", f"type=bind,source={checkout / '.git'},target=/work/.git,readonly",
                     "--mount", f"type=bind,source={issue_file.parent},target=/task,readonly"]
            env_names = (selected_model.credential_env,) if selected_model else cfg.worker_env
            for name in env_names:
                if name not in os.environ:
                    raise WorkflowError(f"required worker environment variable missing: {name}")
                args += ["--env", name]
            if selected_model:
                args += ["--env", f"ERMIK_MODEL_REF={selected_model.ref}"]
                args += ["--env", f"ERMIK_MODEL_SPEC={model_spec(selected_model)}"]
        args += [cfg.image, *command]
        try:
            returncode, output = _run_capped(args, timeout)
        except (TransientFailure, WorkflowError):
            _force_remove_container(cfg, container_name)
            raise
        except OSError as exc:
            raise TransientFailure("Docker command unavailable or timed out") from exc
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path.write_bytes(output)
        if os.name != "nt":
            os.chmod(log_path, 0o600)
    if returncode:
        if issue_file:
            raise WorkerFailure(returncode, parse_worker_usage(output))
        raise WorkflowError(f"Docker command failed (exit {returncode})")
    return parse_worker_usage(output) if issue_file else None


def local_checks(cfg: Config, checkout: Path) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    sha = git(checkout, "rev-parse", "HEAD")
    base = git(checkout, "merge-base", sha, f"origin/{cfg.base_branch}")
    git(checkout, "diff", "--check", f"{base}...{sha}")
    image_id = checked(["docker", "image", "inspect", "--format", "{{.Id}}",
                        cfg.image], timeout=20)
    logs = []
    for index, command in enumerate(cfg.checks, start=1):
        log_path = cfg.state_dir / "ci-logs" / sha / f"check-{index}.log"
        _docker(cfg, checkout, command, issue_file=None, network=False,
                timeout=900, log_path=log_path)
        logs.append({"command": list(command), "exit_code": 0,
                     "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                     "log": str(log_path)})
    if git(checkout, "rev-parse", "HEAD") != sha:
        raise WorkflowError("HEAD moved during local checks")
    if git(checkout, "status", "--porcelain"):
        raise WorkflowError("local checks modified the checkout")
    return {"head": sha, "committed_diff_base": base,
            "started": started, "finished": datetime.now(timezone.utc).isoformat(),
            "checks": logs, "image_id": image_id,
            "result": "passed"}


def _changed(checkout: Path) -> list[str]:
    tracked = git(checkout, "diff", "--name-only", "-z", "HEAD")
    untracked = git(checkout, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set((tracked + untracked).split("\x00")) - {""})


def _check_scope(checkout: Path, spec: IssueSpec) -> list[str]:
    paths = _changed(checkout)
    if not paths:
        raise WorkflowError("worker produced no changed files")
    _validate_scope_paths(checkout, spec, paths)
    return paths


def _validate_scope_paths(checkout: Path, spec: IssueSpec, paths: list[str]) -> None:
    validate_changed_paths(paths, spec.allowed_paths)
    for name in paths:
        target = checkout / name
        parent = target.parent
        while parent != checkout:
            if parent.is_symlink():
                raise WorkflowError("symlink parent in changed path")
            parent = parent.parent
        if target.is_symlink():
            raise WorkflowError("symlink changes are not accepted")
        if target.exists() and not target.is_file():
            raise WorkflowError("only regular file changes are accepted")


def _pr_diff_paths(pr: dict, checkout: Path, base: str, spec: IssueSpec) -> list[str]:
    """Bind a rebase-only request to the already published file set."""
    files = pr.get("files")
    if (not isinstance(files, list) or not files or len(files) > 100
            or pr.get("changedFiles") != len(files)
            or any(not isinstance(file, dict) or not isinstance(file.get("path"), str)
                   for file in files)):
        raise WorkflowError("published PR file list is invalid")
    paths = sorted(file["path"] for file in files)
    if len(set(paths)) != len(paths):
        raise WorkflowError("published PR file list is invalid")
    actual = sorted(set(git(checkout, "diff", "--name-only", base + "...HEAD").splitlines()) - {""})
    if actual != paths:
        raise WorkflowError("published PR paths differ from local commit")
    _validate_scope_paths(checkout, spec, paths)
    return paths


def _task_file(cfg: Config, issue: int, body: str, correction_note: str | None) -> Path:
    directory = cfg.state_dir / "tasks" / str(issue)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "issue.md"
    instruction = (Path(__file__).resolve().parent.parent / "skills" / "worker-task" / "SKILL.md")
    text = instruction.read_text(encoding="utf-8") + "\n\n# Issue\n\n" + body
    if correction_note is not None:
        text += "\n\n## Review correction\n" + correction_note
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def admit(cfg: Config, number: int) -> dict:
    gh = GitHub(cfg.repo)
    if gh.identity() != cfg.maintainer:
        raise WorkflowError("only the configured maintainer can admit an issue")
    issue = gh.issue(number)
    names = labels(issue)
    if "queue:worker" not in names or not ({"state:proposed", "state:ready"} & names):
        raise WorkflowError("issue must be open, queue:worker and state:proposed or state:ready")
    spec = parse_issue(issue.get("body") or "")
    already_ready = "state:ready" in names
    if already_ready and not _approved_on_github(gh, cfg, number, spec):
        raise WorkflowError("ready issue lacks the matching maintainer approval")
    with state.connect(cfg.state_dir) as conn:
        if state.job(conn, number):
            raise WorkflowError("issue already has a job")
        conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?) "
                     "ON CONFLICT(issue) DO UPDATE SET body_sha=excluded.body_sha, "
                     "allowed_json=excluded.allowed_json",
                     (number, spec.body_sha256, json.dumps(spec.allowed_paths)))
        state.event(conn, number, "admitted")
    if not _approved_on_github(gh, cfg, number, spec):
        gh.comment(number, _admission_marker(number, spec) +
                   "\n\nApproved for the scoped worker queue.")
    if not already_ready:
        gh.label(number, add="state:ready", remove="state:proposed")
    return {"issue": number, "allowed_paths": spec.allowed_paths, "body_sha256": spec.body_sha256}


def _correction(gh: GitHub, cfg: Config, conn: sqlite3.Connection,
                budget: list[int] | None = None,
                reserved: dict[str, int] | None = None,
                selected: dict[int, str] | None = None) -> tuple[sqlite3.Row, str, int] | None:
    # A large backlog of draft PRs must not exhaust GitHub's API allowance.
    remaining = budget if budget is not None else [10]
    if remaining[0] <= 0:
        return None
    now = int(time.time())
    rows = conn.execute("SELECT * FROM jobs WHERE status IN ('awaiting_review','blocked') "
                        "AND pr IS NOT NULL AND COALESCE(review_next_check_at,0)<=? "
                        "ORDER BY CASE status WHEN 'awaiting_review' THEN 0 ELSE 1 END,"
                        "COALESCE(review_next_check_at,0),issue LIMIT ?",
                        (now, remaining[0])).fetchall()
    for row in rows:
        remaining[0] -= 1
        conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=?",
                     (now + 600, row["issue"]))
        try:
            pr = gh.pr(row["pr"])
        except GitHubError:
            # A failed read cannot establish the PR's state. Other jobs can
            # still make progress while this one waits for the next cycle.
            conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=?",
                         (now + 60, row["issue"]))
            continue
        try:
            _require_pr(gh, pr, cfg, branch=row["branch"], number=row["pr"],
                        head_sha=row["head_sha"],
                        state_name="OPEN" if pr["state"] == "OPEN" else pr["state"])
        except (WorkflowError, KeyError, TypeError):
            conn.execute("UPDATE jobs SET status='blocked',error='PR identity changed',"
                         "updated_at=CURRENT_TIMESTAMP WHERE issue=?", (row["issue"],))
            state.event(conn, row["issue"], "blocked")
            try:
                gh.label(row["issue"], add="state:blocked", remove="state:in-progress")
            except GitHubError:
                pass
            continue
        if pr["state"] == "MERGED":
            conn.execute("UPDATE jobs SET status='merged',updated_at=CURRENT_TIMESTAMP WHERE issue=?", (row["issue"],))
            state.event(conn, row["issue"], "merged", head_sha=row["head_sha"])
            continue
        try:
            issue = gh.issue(row["issue"])
        except GitHubError:
            conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=?",
                         (now + 60, row["issue"]))
            continue
        approved = state.admission(conn, row["issue"])
        try:
            spec = parse_issue(issue.get("body") or "")
        except (TypeError, ValueError):
            spec = None
        try:
            approved_on_github = (spec is not None and
                                  _approved_on_github(gh, cfg, row["issue"], spec))
        except GitHubError:
            conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=?",
                         (now + 60, row["issue"]))
            continue
        expected_state = "state:blocked" if row["status"] == "blocked" else "state:in-progress"
        if (spec is None or approved is None or approved["body_sha"] != spec.body_sha256
                or tuple(json.loads(row["allowed_json"])) != spec.allowed_paths
                or not approved_on_github
                or not {"queue:worker", expected_state}.issubset(labels(issue))):
            conn.execute("UPDATE jobs SET status='blocked',error='Issue approval changed' "
                         "WHERE issue=?", (row["issue"],))
            state.event(conn, row["issue"], "blocked")
            try:
                gh.label(row["issue"], add="state:blocked", remove="state:in-progress")
            except GitHubError:
                pass
            continue
        if pr["state"] != "OPEN" or pr["headRefOid"] != row["head_sha"]:
            conn.execute("UPDATE jobs SET status='blocked',error='PR identity changed' WHERE issue=?", (row["issue"],))
            state.event(conn, row["issue"], "blocked")
            try:
                gh.label(row["issue"], add="state:blocked", remove="state:in-progress")
            except GitHubError:
                pass
            continue
        try:
            comments = gh.comments(row["pr"])
        except GitHubError:
            conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=?",
                         (now + 60, row["issue"]))
            continue
        for comment in comments:
            match = REWORK.search(comment.get("body") or "")
            if (comment["id"] <= row["last_request"] or not match
                    or comment.get("user", {}).get("login") != cfg.maintainer
                    or int(match.group(1)) != row["issue"]
                    or match.group(2) != row["head_sha"]):
                continue
            rebase_only = bool(REBASE_ONLY.search(comment.get("body") or ""))
            provider = None if rebase_only else _route_provider(conn, cfg, issue, reserved or {})
            if provider is None and not rebase_only:
                conn.execute("UPDATE jobs SET review_next_check_at=? WHERE issue=?",
                             (now + 60, row["issue"]))
                break
            if row["status"] == "blocked":
                # A fresh, exact-head maintainer request is the sole way to
                # reset a blocked correction's bounded retry state.
                gh.label(row["issue"], add="state:in-progress", remove="state:blocked")
                conn.execute("DELETE FROM retry_state WHERE issue=?", (row["issue"],))
            conn.execute("UPDATE jobs SET status='correcting',last_request=?,updated_at=CURRENT_TIMESTAMP WHERE issue=?",
                         (comment["id"], row["issue"]))
            state.event(conn, row["issue"], "correction_started", head_sha=row["head_sha"])
            if reserved is not None and provider:
                reserved[provider] = reserved.get(provider, 0) + 1
            if selected is not None:
                selected[row["issue"]] = provider
            return row, comment["body"], comment["id"]
    return None


def _claim_new(gh: GitHub, cfg: Config, conn: sqlite3.Connection,
               reserved: dict[str, int] | None = None,
               selected: dict[int, str] | None = None) -> tuple[int, str, IssueSpec] | None:
    resources = resource_guard(cfg.workspace,
                               min_free_bytes=cfg.min_free_gb * 1024**3,
                               max_worktrees=cfg.max_parallel)
    if not resources["admit"]:
        return None
    for number in gh.ready_issues():
        if state.job(conn, number):
            continue
        try:
            issue = gh.issue(number)
        except GitHubError:
            continue
        if not {"queue:worker", "state:ready"}.issubset(labels(issue)):
            continue
        provider = _route_provider(conn, cfg, issue, reserved or {})
        if provider is None:
            continue
        try:
            spec = parse_issue(issue.get("body") or "")
        except (ValueError, TypeError):
            continue
        try:
            approved = _approved_on_github(gh, cfg, number, spec)
        except GitHubError:
            continue
        if not approved:
            continue
        if state.conflicting(conn, spec.allowed_paths):
            continue
        if cfg.publish_repo:
            gh.validate_publish_access()
        conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json) VALUES(?,?,?) "
                     "ON CONFLICT(issue) DO UPDATE SET body_sha=excluded.body_sha, "
                     "allowed_json=excluded.allowed_json",
                     (number, spec.body_sha256, json.dumps(spec.allowed_paths)))
        base = main_sha(gh, cfg)
        branch = f"work/issue-{number}-delivery"
        conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) VALUES(?,?,?,?,?)",
                     (number, branch, base, "running", json.dumps(spec.allowed_paths)))
        state.event(conn, number, "claimed")
        conn.commit()  # The local intent must exist before the external label write.
        gh.label(number, add="state:in-progress", remove="state:ready")
        if reserved is not None and provider:
            reserved[provider] = reserved.get(provider, 0) + 1
        if selected is not None:
            selected[number] = provider
        return number, branch, spec
    return None


def _rebuild_published_evidence(cfg: Config, row, pr: dict) -> dict:
    """Rerun checks on an exact local commit after an acknowledged PR write was lost."""
    if (not pr.get("isDraft") or pr.get("changedFiles") != len(pr.get("files", []))
            or not pr["files"] or pr["changedFiles"] > 100
            or re.findall(r"(?im)\bCloses\s+#(\d+)\b", pr.get("body") or "") !=
            [str(row["issue"])]) :
        raise WorkflowError("published PR does not match task contract")
    source = mirror(cfg)
    local_head = git(source, "rev-parse", f"refs/heads/{row['branch']}")
    if local_head != pr["headRefOid"]:
        raise WorkflowError("published PR head lacks matching local commit")
    paths = sorted(file["path"] for file in pr["files"])
    allowed = tuple(json.loads(row["allowed_json"]))
    validate_changed_paths(paths, allowed)
    checkout = cfg.workspace / "recovery" / f"issue-{row['issue']}"
    if checkout.exists():
        raise WorkflowError("recovery checkout requires inspection")
    checkout.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(source, "worktree", "add", "--detach", str(checkout), local_head)
    try:
        base = git(checkout, "merge-base", "HEAD", f"origin/{cfg.base_branch}")
        actual = set(git(checkout, "diff", "--name-only",
                         base + "...HEAD").splitlines())
        if actual != set(paths):
            raise WorkflowError("published PR paths differ from local commit")
        proof = local_checks(cfg, checkout)
    finally:
        if checkout.exists():
            _remove_checkout(cfg, checkout)
    return {"issue": row["issue"], "pr": pr["number"], "branch": row["branch"],
            "head": local_head, "base": base, "paths": paths, "ci": proof,
            "model": row["last_model"]}


def _recover_unacknowledged_push(gh: GitHub, cfg: Config, row,
                                 issue: dict, spec: IssueSpec, remote: str) -> dict:
    """Publish a checked local commit when push succeeded but its reply was lost."""
    match = re.fullmatch(r"([0-9a-f]{40})\s+refs/heads/" + re.escape(row["branch"]),
                         remote.strip())
    if not match or row["status"] not in ("running", "retry_wait") or row["pr"] is not None:
        raise WorkflowError("orphan branch requires inspection")
    source = mirror(cfg)
    head = git(source, "rev-parse", f"refs/heads/{row['branch']}")
    if head != match.group(1) or main_sha(gh, cfg) != row["base_sha"]:
        raise WorkflowError("orphan branch head or base changed")
    checkout = cfg.workspace / "recovery" / f"issue-{row['issue']}"
    if checkout.exists():
        raise WorkflowError("recovery checkout requires inspection")
    checkout.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(source, "worktree", "add", "--detach", str(checkout), head)
    try:
        if git(checkout, "merge-base", "HEAD", f"origin/{cfg.base_branch}") != row["base_sha"]:
            raise WorkflowError("orphan branch base differs")
        paths = sorted(set(git(checkout, "diff", "--name-only",
                               row["base_sha"] + "...HEAD").splitlines()) - {""})
        validate_changed_paths(paths, spec.allowed_paths)
        if not paths or len(paths) > 100:
            raise WorkflowError("orphan branch file list is invalid")
        for name in paths:
            target = checkout / name
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents
                                          if parent != checkout and checkout in parent.parents):
                raise WorkflowError("orphan branch contains a symlink")
        proof = local_checks(cfg, checkout)
    finally:
        if checkout.exists():
            _remove_checkout(cfg, checkout)
    if (parse_issue(gh.issue(row["issue"]).get("body") or "").body_sha256 != spec.body_sha256
            or not _approved_on_github(gh, cfg, row["issue"], spec)
            or main_sha(gh, cfg) != row["base_sha"]):
        raise WorkflowError("orphan branch approval or base changed")
    if cfg.publish_repo:
        gh.validate_publish_access()
    try:
        number = _draft_pr(cfg, gh, issue=row["issue"], branch=row["branch"],
                           head=head, base=row["base_sha"], paths=paths,
                           proof=proof, model=row["last_model"],
                           body_sha256=spec.body_sha256,
                           title=f"Issue #{row['issue']}: {issue['title']}")
    except GitHubError:
        if cfg.draft_pr_socket is not None:
            raise
        number = gh.find_branch_pr(row["branch"], cfg.base_branch)
        if number is None:
            raise
    pr = gh.pr(number)
    _require_pr(gh, pr, cfg, branch=row["branch"], number=number, head_sha=head)
    if (not pr["isDraft"] or sorted(file["path"] for file in pr["files"]) != paths
            or pr["changedFiles"] != len(paths)):
        raise WorkflowError("orphan branch PR differs from checked commit")
    return {"issue": row["issue"], "pr": number, "branch": row["branch"],
            "head": head, "base": row["base_sha"], "paths": paths,
            "ci": proof, "model": row["last_model"]}


def _verify_leftover_checkout(cfg: Config, row, remote_head: str) -> Path | None:
    """Accept only an untouched task worktree at the published commit."""
    checkout = cfg.workspace / "jobs" / f"issue-{row['issue']}"
    if not checkout.exists() and not checkout.is_symlink():
        return None
    if checkout.is_symlink() or not checkout.is_dir():
        raise WorkflowError("interrupted checkout requires inspection")
    source = cfg.workspace / "source"
    registered = any(
        line.startswith("worktree ")
        and Path(line[len("worktree "):]).resolve() == checkout.resolve()
        for line in git(source, "worktree", "list", "--porcelain").splitlines()
    )
    if (not registered
            or git(checkout, "rev-parse", "HEAD") != remote_head
            or git(checkout, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{row['branch']}"
            or git(checkout, "status", "--porcelain=v1", "--untracked-files=all",
                   "--ignored=matching")):
        raise WorkflowError("interrupted checkout requires inspection")
    return checkout


def _reconcile_interrupted(gh: GitHub, cfg: Config, conn) -> None:
    """Resume only interruptions with a provably untouched remote branch."""
    for row in state.jobs(conn):
        if row["status"] not in ("running", "correcting", "retry_wait"):
            continue
        try:
            issue = gh.issue(row["issue"])
            spec = parse_issue(issue.get("body") or "")
            approved = state.admission(conn, row["issue"])
            if (approved is None or approved["body_sha"] != spec.body_sha256
                    or tuple(json.loads(row["allowed_json"])) != spec.allowed_paths
                    or not _approved_on_github(gh, cfg, row["issue"], spec)
                    or "queue:worker" not in labels(issue)
                    or not ({"state:ready", "state:in-progress"} & labels(issue))):
                raise WorkflowError("interrupted job identity changed")
            pr_number = gh.find_branch_pr(row["branch"], cfg.base_branch)
            if pr_number is not None:
                pr = gh.pr(pr_number)
                _require_pr(gh, pr, cfg, branch=row["branch"],
                            number=pr_number)
                leftover = _verify_leftover_checkout(cfg, row, pr["headRefOid"])
                if row["pr"] is not None and pr_number != row["pr"]:
                    raise WorkflowError("interrupted PR number changed")
                if row["pr"] is not None and pr["headRefOid"] == row["head_sha"]:
                    if row["status"] == "retry_wait":
                        if leftover is not None:
                            _remove_checkout(cfg, leftover)
                        continue
                    if row["status"] != "correcting":
                        raise WorkflowError("published PR did not advance")
                    if leftover is not None:
                        _remove_checkout(cfg, leftover)
                    retry = conn.execute("SELECT lease_token FROM retry_state WHERE issue=?",
                                         (row["issue"],)).fetchone()
                    decision = record_failure(conn, row["issue"], "interrupted",
                                              failure_id=f"interrupted-{row['issue']}-{row['updated_at']}",
                                              resume_status="correcting",
                                              lease_token=retry[0] if retry else None)
                    state.event(conn, row["issue"], "retry_scheduled"
                                if decision.status == "retry_wait" else "blocked")
                    continue
                evidence = (cfg.state_dir / "evidence" / str(row["issue"])
                            / f"{pr['headRefOid']}.json")
                if evidence.is_file():
                    proof = json.loads(evidence.read_text(encoding="utf-8"))
                if not evidence.is_file() or proof.get("pr") != pr_number:
                    proof = _rebuild_published_evidence(cfg, row, pr)
                    evidence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    evidence.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n",
                                        encoding="utf-8")
                if (proof.get("issue") != row["issue"] or proof.get("pr") != pr_number
                        or proof.get("branch") != row["branch"]
                        or proof.get("head") != pr["headRefOid"]
                        or proof.get("ci", {}).get("result") != "passed"):
                    raise WorkflowError("published branch lacks matching evidence")
                if cfg.draft_pr_socket is not None and row["pr"] is None:
                    # A lost PR response must pass the same broker gate as a new PR.
                    # The broker rechecks the live upstream, fork, scope, and evidence.
                    broker_pr = _draft_pr(
                        cfg, gh, issue=row["issue"], branch=row["branch"],
                        head=proof["head"], base=proof["base"],
                        paths=proof["paths"], proof=proof["ci"],
                        model=proof.get("model"), body_sha256=spec.body_sha256,
                        title=f"Issue #{row['issue']}: {issue['title']}")
                    if broker_pr != pr_number:
                        raise WorkflowError("broker returned a different PR")
                    # _draft_pr writes prepublication evidence for the broker.
                    # Restore the complete record used by subsequent review.
                    evidence.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n",
                                        encoding="utf-8")
                if leftover is not None:
                    _remove_checkout(cfg, leftover)
                conn.execute("UPDATE jobs SET status='awaiting_review',head_sha=?,base_sha=?,pr=?,"
                             "error=NULL,result_model=?,updated_at=CURRENT_TIMESTAMP "
                             "WHERE issue=?",
                             (pr["headRefOid"], proof.get("base", row["base_sha"]),
                              pr_number, proof.get("model"), row["issue"]))
                conn.execute("DELETE FROM retry_state WHERE issue=?", (row["issue"],))
                state.event(conn, row["issue"], "awaiting_review",
                            model=proof.get("model"), head_sha=pr["headRefOid"])
                continue
            elif row["status"] == "correcting":
                raise WorkflowError("correction PR disappeared")
            else:
                if row["status"] == "retry_wait":
                    source = cfg.workspace / "source"
                    if not source.exists():
                        checkout = cfg.workspace / "jobs" / f"issue-{row['issue']}"
                        if checkout.exists() or checkout.is_symlink():
                            raise WorkflowError("interrupted checkout requires inspection")
                        continue
                    try:
                        local = git(source, "rev-parse", f"refs/heads/{row['branch']}")
                    except WorkflowError:
                        continue
                    if local == row["base_sha"]:
                        checkout = cfg.workspace / "jobs" / f"issue-{row['issue']}"
                        if checkout.exists() or checkout.is_symlink():
                            raise WorkflowError("interrupted checkout requires inspection")
                        continue
                remote = (_publish_git(cfg, mirror(cfg), "ls-remote", "--heads",
                                       row["branch"], timeout=60)
                          if cfg.publish_repo else
                          checked(["git", "ls-remote", "--heads",
                                   f"https://github.com/{cfg.repo}.git", row["branch"]],
                                  timeout=60))
                if remote:
                    match = re.fullmatch(
                        r"([0-9a-f]{40})\s+refs/heads/" + re.escape(row["branch"]),
                        remote.strip())
                    if match is None:
                        raise WorkflowError("orphan branch requires inspection")
                    leftover = _verify_leftover_checkout(cfg, row, match.group(1))
                    proof = _recover_unacknowledged_push(gh, cfg, row, issue, spec, remote)
                    evidence = (cfg.state_dir / "evidence" / str(row["issue"])
                                / f"{proof['head']}.json")
                    evidence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    evidence.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n",
                                        encoding="utf-8")
                    if leftover is not None:
                        _remove_checkout(cfg, leftover)
                    conn.execute("UPDATE jobs SET status='awaiting_review',head_sha=?,base_sha=?,"
                                 "pr=?,error=NULL,result_model=?,updated_at=CURRENT_TIMESTAMP "
                                 "WHERE issue=?",
                                 (proof["head"], proof["base"], proof["pr"],
                                  proof["model"], row["issue"]))
                    conn.execute("DELETE FROM retry_state WHERE issue=?", (row["issue"],))
                    state.event(conn, row["issue"], "awaiting_review",
                                model=proof["model"], head_sha=proof["head"])
                    continue
                if row["status"] == "retry_wait":
                    checkout = cfg.workspace / "jobs" / f"issue-{row['issue']}"
                    if checkout.exists() or checkout.is_symlink():
                        raise WorkflowError("interrupted checkout requires inspection")
                    continue
            checkout = cfg.workspace / "jobs" / f"issue-{row['issue']}"
            if checkout.exists() or checkout.is_symlink():
                raise WorkflowError("interrupted checkout requires inspection")
            if "state:ready" in labels(issue):
                gh.label(row["issue"], add="state:in-progress", remove="state:ready")
            retry = conn.execute("SELECT lease_token FROM retry_state WHERE issue=?",
                                 (row["issue"],)).fetchone()
            decision = record_failure(conn, row["issue"], "interrupted",
                                      failure_id=f"interrupted-{row['issue']}-{row['updated_at']}",
                                      resume_status=row["status"],
                                      lease_token=retry[0] if retry else None)
            state.event(conn, row["issue"],
                        "retry_scheduled" if decision.status == "retry_wait" else "blocked")
            continue
        except (GitHubError, TransientFailure):
            # A transport failure says nothing about remote state. Try again next poll.
            continue
        except (WorkflowError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
        conn.execute("UPDATE jobs SET status='blocked',error='Interrupted run requires inspection',"
                     "updated_at=CURRENT_TIMESTAMP WHERE issue=?", (row["issue"],))
        state.event(conn, row["issue"], "blocked")
        try:
            gh.label(row["issue"], add="state:blocked", remove="state:in-progress")
        except GitHubError:
            pass


def _perform(cfg: Config, issue: int, branch: str, spec: IssueSpec,
             *, correction_note: str | None, pr_number: int | None,
             expected_base: str, expected_head: str | None,
             preferred_provider: str | None = None,
             reserved_elsewhere: frozenset[str] = frozenset()) -> dict:
    gh = _github(cfg)
    rebase_only = pr_number is not None and bool(
        REBASE_ONLY.search(correction_note or ""))
    current = gh.issue(issue)
    if spec.body_sha256 != parse_issue(current.get("body") or "").body_sha256:
        raise WorkflowError("issue changed after admission")
    routed_models = _routed_models(cfg, current)
    claim_order = routed_models
    if preferred_provider:
        if not any(model.provider_id == preferred_provider for model in routed_models):
            raise WorkflowError("provider route changed after scheduling")
        claim_order = tuple(model for model in routed_models
                            if model.provider_id == preferred_provider) + tuple(
                                model for model in routed_models
                                if model.provider_id != preferred_provider)
    claim_order = tuple(model for model in claim_order
                        if model.provider_id == preferred_provider
                        or model.provider_id not in reserved_elsewhere)
    if routed_models and not claim_order and not rebase_only:
        raise WorkflowError("no provider capacity is available")
    if pr_number is not None:
        if not expected_head or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise WorkflowError("correction has no verified prior head")
        original = gh.pr(pr_number)
        _require_pr(gh, original, cfg, branch=branch, number=pr_number,
                    head_sha=expected_head)
    base = main_sha(gh, cfg) if pr_number is not None else expected_base
    checkout = _checkout(cfg, issue, branch, correction=pr_number is not None)
    pushed = False
    try:
        if pr_number is not None:
            if git(checkout, "rev-parse", "HEAD") != expected_head:
                raise WorkflowError("correction checkout head changed")
            if git(checkout, "rev-parse", f"origin/{cfg.base_branch}") != base:
                raise TransientFailure("base branch changed during checkout")
            prior_paths = (_pr_diff_paths(original, checkout, expected_base, spec)
                           if rebase_only else None)
            if base != expected_base:
                try:
                    git(checkout, "rebase", base)
                except WorkflowError as exc:
                    git(checkout, "rebase", "--abort")
                    raise WorkflowError("task branch conflicts with advanced base") from exc
        used_model = None
        if not rebase_only:
            task = _task_file(cfg, issue, current["body"], correction_note)
            if claim_order:
                for _ in range(len(claim_order)):
                    lease_id = f"issue-{issue}-{uuid.uuid4().hex}"
                    with state.connect(cfg.state_dir) as conn:
                        candidate = claim_model(conn, claim_order, lease_id)
                    if candidate is None:
                        raise WorkflowError("no provider capacity is available")
                    try:
                        with state.connect(cfg.state_dir) as conn:
                            state.event(conn, issue, "model_selected", model=candidate.ref)
                            conn.execute("UPDATE jobs SET last_model=?,attempts_count=attempts_count+1 "
                                         "WHERE issue=?", (candidate.ref, issue))
                        usage = _docker(cfg, checkout, cfg.worker_command, issue_file=task,
                                        network=True, timeout=3600, selected_model=candidate)
                        with state.connect(cfg.state_dir) as conn:
                            state.record_usage(conn, lease_id, issue, candidate.provider_id,
                                               candidate.model_id, usage, 0)
                        used_model = candidate
                        break
                    except WorkerFailure as exc:
                        with state.connect(cfg.state_dir) as conn:
                            state.record_usage(conn, lease_id, issue, candidate.provider_id,
                                               candidate.model_id, exc.usage, exc.returncode)
                        if exc.returncode != 75:
                            raise
                        with state.connect(cfg.state_dir) as conn:
                            record_exhaustion(conn, candidate, int(time.time()) + 900)
                            state.event(conn, issue, "quota_paused", model=candidate.ref)
                        baseline = git(checkout, "rev-parse", "HEAD")
                        git(checkout, "reset", "--hard", baseline)
                        git(checkout, "clean", "-fdx")
                    except Exception:
                        with state.connect(cfg.state_dir) as conn:
                            state.record_usage(conn, lease_id, issue, candidate.provider_id,
                                               candidate.model_id, None, -1)
                        raise
                    finally:
                        with state.connect(cfg.state_dir) as conn:
                            release_model(conn, lease_id)
                if used_model is None:
                    raise WorkflowError("all configured providers reached their quota")
            else:
                with state.connect(cfg.state_dir) as conn:
                    conn.execute("UPDATE jobs SET attempts_count=attempts_count+1 WHERE issue=?",
                                 (issue,))
                _docker(cfg, checkout, cfg.worker_command, issue_file=task,
                        network=True, timeout=3600)
        if pr_number is None:
            base = git(checkout, "merge-base", "HEAD", f"origin/{cfg.base_branch}")
        if rebase_only and _changed(checkout):
            raise WorkflowError("rebase-only checkout contains file changes")
        if not rebase_only:
            paths = _check_scope(checkout, spec)
            git(checkout, "add", "-A")
            staged = git(checkout, "diff", "--cached", "--name-only", "-z").split("\x00")
            validate_changed_paths((p for p in staged if p), spec.allowed_paths)
            git(checkout, "commit", "-m", f"Deliver issue #{issue}")
        elif base != expected_base:
            paths = _pr_diff_paths({**original, "files": [{"path": path} for path in prior_paths],
                                    "changedFiles": len(prior_paths)}, checkout, base, spec)
            if git(checkout, "rev-parse", "HEAD") == expected_head:
                raise WorkflowError("rebase-only correction did not advance head")
        else:
            raise WorkflowError("worker produced no changed files")
        head = git(checkout, "rev-parse", "HEAD")
        proof = local_checks(cfg, checkout)
        with state.connect(cfg.state_dir) as conn:
            state.event(conn, issue, "checks_passed",
                        model=used_model.ref if used_model else None, head_sha=head)
        latest = gh.issue(issue)
        if (parse_issue(latest.get("body") or "").body_sha256 != spec.body_sha256
                or not {"queue:worker", "state:in-progress"}.issubset(labels(latest))
                or not _approved_on_github(gh, cfg, issue, spec)
                or _routed_models(cfg, latest) != routed_models):
            raise WorkflowError("issue approval or provider route changed during execution")
        if main_sha(gh, cfg) != base:
            raise TransientFailure("base branch moved during execution")
        if pr_number is not None:
            existing = gh.pr(pr_number)
            _require_pr(gh, existing, cfg, branch=branch, number=pr_number,
                        head_sha=expected_head)
        if cfg.publish_repo:
            gh.validate_publish_access()
        push = ["push", "origin"]
        if pr_number is not None and base != expected_base:
            push.append(f"--force-with-lease=refs/heads/{branch}:{expected_head}")
        if cfg.publish_repo:
            _publish_git(cfg, checkout, "push", *push[2:],
                         f"HEAD:refs/heads/{branch}", timeout=180)
        else:
            try:
                git(checkout, *push, f"HEAD:refs/heads/{branch}", timeout=180)
            except WorkflowError as exc:
                raise TransientFailure("Git push outcome is unconfirmed") from exc
        pushed = True
        creating_pr = pr_number is None
        if pr_number is None:
            try:
                pr_number = _draft_pr(cfg, gh, issue=issue, branch=branch,
                                      head=head, base=base, paths=paths,
                                      proof=proof,
                                      model=used_model.ref if used_model else None,
                                      body_sha256=spec.body_sha256,
                                      title=f"Issue #{issue}: {current['title']}")
            except GitHubError:
                if cfg.draft_pr_socket is not None:
                    raise
                pr_number = gh.find_branch_pr(branch, cfg.base_branch)
                if pr_number is None:
                    raise
        observed = None
        for _ in range(5):
            observed = gh.pr(pr_number)
            if observed["headRefOid"] == head:
                break
            time.sleep(2)
        _require_pr(gh, observed, cfg, branch=branch, number=pr_number,
                    head_sha=head)
        if cfg.draft_pr_socket is not None and creating_pr and observed.get("isDraft") is not True:
            raise WorkflowError("initial PR is not a draft")
        result = {"issue": issue, "pr": pr_number, "branch": branch, "head": head,
                  "base": base,
                  "paths": paths, "ci": proof,
                  "model": used_model.ref if used_model else None}
        evidence = cfg.state_dir / "evidence" / str(issue)
        evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        (evidence / f"{head}.json").write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with state.connect(cfg.state_dir) as conn:
            state.event(conn, issue, "pr_published",
                        model=used_model.ref if used_model else None, head_sha=head)
        return result
    finally:
        if checkout.exists():
            try:
                if not pushed:
                    git(checkout, "reset", "--hard", "HEAD")
                    git(checkout, "clean", "-fdx")
                _remove_checkout(cfg, checkout)
            except WorkflowError:
                pass


def run_once(cfg: Config) -> list[dict]:
    """One bounded controller cycle. A correction has priority over new work."""
    gh = _github(cfg)
    with state.exclusive(cfg.state_dir):
        _cleanup_containers(cfg)
        work: list[tuple[int, str, IssueSpec, str | None, int | None, RecoveryClaim | None]] = []
        reserved_providers: dict[str, int] = {}
        selected_providers: dict[int, str] = {}
        review_budget = [min(10, max(1, cfg.poll_seconds // 5))]
        with state.connect(cfg.state_dir) as conn:
            _reconcile_interrupted(gh, cfg, conn)
            recover_stale_worktrees(cfg.workspace, conn)
            # Reconcile merged PRs before reserving new paths.
            # An explicit rebase-only request needs no provider slot.
            cycle_capacity = (min(cfg.max_parallel, max(1, available_capacity(conn, cfg.models)))
                              if cfg.models else cfg.max_parallel)
            while len(work) < cycle_capacity:
                if not cfg.models or choose_model(conn, cfg.models) is not None:
                    due = claim_due(conn, limit=1)
                    if due:
                        claim = due[0]
                        row = state.job(conn, claim.issue)
                        try:
                            issue = gh.issue(claim.issue)
                            provider = _route_provider(conn, cfg, issue,
                                                       reserved_providers)
                            if provider is None:
                                # claim_due increments attempts. A route with no
                                # slot has not attempted execution.
                                conn.execute("UPDATE retry_state SET attempts=attempts-1,"
                                             "lease_token=NULL,lease_expires_at=NULL,"
                                             "next_retry_at=? WHERE issue=? AND lease_token=?",
                                             (int(time.time()) + 60, claim.issue,
                                              claim.token))
                                continue
                            spec = parse_issue(issue.get("body") or "")
                            approved = state.admission(conn, claim.issue)
                            if (approved is None or approved["body_sha"] != spec.body_sha256
                                    or tuple(json.loads(row["allowed_json"])) != spec.allowed_paths
                                    or not _approved_on_github(gh, cfg, claim.issue, spec)
                                    or not {"queue:worker", "state:in-progress"}.issubset(labels(issue))):
                                raise WorkflowError("retry identity changed")
                            note = None
                            if claim.resume_status == "correcting":
                                pr = gh.pr(row["pr"])
                                _require_pr(gh, pr, cfg, branch=row["branch"],
                                            number=row["pr"], head_sha=row["head_sha"])
                                notes = [comment["body"] for comment in gh.comments(row["pr"])
                                         if comment["id"] == row["last_request"]
                                         and comment.get("user", {}).get("login") == cfg.maintainer]
                                if len(notes) != 1:
                                    raise WorkflowError("retry review note missing")
                                note = notes[0]
                                match = REWORK.search(note)
                                if (match is None or int(match.group(1)) != claim.issue
                                        or match.group(2) != row["head_sha"]):
                                    raise WorkflowError("retry review note changed")
                            elif gh.find_branch_pr(row["branch"]) is not None:
                                raise WorkflowError("retry branch already has PR")
                            checkout = cfg.workspace / "jobs" / f"issue-{claim.issue}"
                            if checkout.exists():
                                raise WorkflowError("retry checkout requires inspection")
                            if not activate_claim(conn, claim):
                                raise WorkflowError("retry lease expired")
                            state.event(conn, claim.issue, "retry_started")
                            work.append((claim.issue, row["branch"], spec, note,
                                         row["pr"], claim))
                            if provider:
                                reserved_providers[provider] = (
                                    reserved_providers.get(provider, 0) + 1)
                            selected_providers[claim.issue] = provider
                            continue
                        except GitHubError:
                            record_failure(conn, claim.issue, "transient",
                                           failure_id=f"retry-transport-{claim.token}",
                                           resume_status=claim.resume_status,
                                           lease_token=claim.token)
                            state.event(conn, claim.issue, "retry_scheduled")
                            continue
                        except (WorkflowError, ValueError, KeyError, TypeError):
                            record_failure(conn, claim.issue, "identity",
                                           failure_id=f"retry-verify-{claim.token}",
                                           resume_status=claim.resume_status,
                                           lease_token=claim.token)
                            state.event(conn, claim.issue, "blocked")
                            continue
                correction = _correction(gh, cfg, conn, review_budget,
                                         reserved_providers, selected_providers)
                if correction:
                    row, note, _request = correction
                    issue = gh.issue(row["issue"])
                    spec = parse_issue(issue.get("body") or "")
                    work.append((row["issue"], row["branch"], spec, note, row["pr"], None))
                    continue
                claimed = _claim_new(gh, cfg, conn, reserved_providers,
                                     selected_providers)
                if not claimed:
                    break
                number, branch, spec = claimed
                work.append((number, branch, spec, None, None, None))
        if not work:
            return []

        def execute(item: tuple[int, str, IssueSpec, str | None, int | None, RecoveryClaim | None]) -> dict:
            number, branch, spec, note, pr, retry_claim = item
            try:
                with state.connect(cfg.state_dir) as conn:
                    current_job = state.job(conn, number)
                    expected_base = current_job["base_sha"]
                    expected_head = current_job["head_sha"]
                preferred = selected_providers.get(number)
                reserved_elsewhere = frozenset(
                    provider for other_issue, provider in selected_providers.items()
                    if other_issue != number)
                result = _perform(cfg, number, branch, spec,
                                  correction_note=note, pr_number=pr,
                                  expected_base=expected_base,
                                  expected_head=expected_head,
                                  **({"reserved_elsewhere": reserved_elsewhere}
                                     if reserved_elsewhere else {}),
                                  **({"preferred_provider": preferred}
                                     if preferred else {}))
                result_model = (current_job["result_model"]
                                if pr is not None and note and REBASE_ONLY.search(note)
                                else result.get("model"))
                result["model"] = result_model
                with state.connect(cfg.state_dir) as conn:
                    conn.execute("UPDATE jobs SET status='awaiting_review',head_sha=?,base_sha=?,pr=?,error=NULL,result_model=?,"
                                 "updated_at=CURRENT_TIMESTAMP WHERE issue=?",
                                 (result["head"], result.get("base", expected_base),
                                  result["pr"], result_model, number))
                    if retry_claim:
                        complete_claim(conn, retry_claim)
                    state.event(conn, number, "awaiting_review",
                                model=result_model, head_sha=result["head"])
                return result
            except Exception as exc:
                with state.connect(cfg.state_dir) as conn:
                    category = ("provider" if (isinstance(exc, WorkerFailure) and exc.returncode == 75
                                                 or str(exc).startswith(("no provider capacity", "all configured providers")))
                                else "identity" if isinstance(exc, WorkerFailure) and exc.returncode in (77, 78)
                                else "transient" if isinstance(exc, (WorkerFailure, TransientFailure, GitHubError))
                                else "identity")
                    decision = record_failure(conn, number, category,
                                              failure_id=f"run-{uuid.uuid4().hex}",
                                              resume_status="correcting" if pr is not None else "running",
                                              lease_token=retry_claim.token if retry_claim else None)
                    conn.execute("UPDATE jobs SET error=? WHERE issue=?",
                                 (type(exc).__name__, number))
                    state.event(conn, number,
                                "retry_scheduled" if decision.status == "retry_wait" else "blocked")
                if decision.status == "blocked":
                    try:
                        gh.comment(number, f"Execution blocked: {type(exc).__name__}. Inspect local controller state.")
                        gh.label(number, add="state:blocked", remove="state:in-progress")
                    except GitHubError:
                        pass
                return {"issue": number, "status": decision.status,
                        "error": type(exc).__name__}

        with ThreadPoolExecutor(max_workers=cfg.max_parallel) as pool:
            return list(pool.map(execute, work))


def poll(cfg: Config) -> None:
    while True:
        try:
            result = run_once(cfg)
        except (GitHubError, TransientFailure, WorkflowError) as exc:
            result = {"status": "error", "error": type(exc).__name__}
        print(json.dumps(result, sort_keys=True), flush=True)
        time.sleep(cfg.poll_seconds)
