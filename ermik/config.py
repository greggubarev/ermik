"""Strict configuration with no implicit production repository or credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re


_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REQUIRED = {"repo", "maintainer", "state_dir", "workspace", "image",
             "worker_command", "checks", "max_parallel", "poll_seconds"}
_OPTIONAL = {"worker_env", "base_branch", "models", "min_free_gb",
             "publish_repo", "publish_token_env", "routes", "draft_pr_socket",
             "draft_pr_evidence_dir"}
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROUTE = re.compile(r"^route:[a-z][a-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class Config:
    repo: str
    maintainer: str
    state_dir: Path
    workspace: Path
    image: str
    worker_command: tuple[str, ...]
    checks: tuple[tuple[str, ...], ...]
    max_parallel: int
    poll_seconds: int
    worker_env: tuple[str, ...]
    base_branch: str
    models: tuple = ()
    min_free_gb: int = 2
    publish_repo: str | None = None
    publish_token_env: str | None = None
    routes: dict[str, str] = field(default_factory=dict)
    draft_pr_socket: Path | None = None
    draft_pr_evidence_dir: Path | None = None


def _command(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
            not isinstance(arg, str) or not arg or "\x00" in arg for arg in value):
        raise ValueError(f"{name} must be a nonempty string array")
    return tuple(value)


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(raw, dict) or not _REQUIRED.issubset(raw)
            or not set(raw).issubset(_REQUIRED | _OPTIONAL)):
        raise ValueError(f"config requires: {', '.join(sorted(_REQUIRED))}")
    repo, maintainer = raw["repo"], raw["maintainer"]
    if (not isinstance(repo, str) or not _REPO.fullmatch(repo)
            or repo.split("/", 1)[1] in (".", "..")):
        raise ValueError("repo must be owner/name")
    if not isinstance(maintainer, str) or not _LOGIN.fullmatch(maintainer):
        raise ValueError("maintainer must be a GitHub login")
    publish_repo = raw.get("publish_repo")
    if publish_repo is not None:
        if (not isinstance(publish_repo, str) or not _REPO.fullmatch(publish_repo)
                or publish_repo.split("/", 1)[1] in (".", "..")
                or publish_repo.casefold() == repo.casefold()):
            raise ValueError("publish_repo must be a separate owner/name fork")
    state_dir, workspace = Path(raw["state_dir"]), Path(raw["workspace"])
    if not state_dir.is_absolute() or not workspace.is_absolute():
        raise ValueError("state_dir and workspace must be absolute")
    if state_dir.resolve() != state_dir or workspace.resolve() != workspace:
        raise ValueError("state_dir and workspace must be canonical, without symlinks")
    if state_dir == workspace or state_dir in workspace.parents or workspace in state_dir.parents:
        raise ValueError("state_dir and workspace must be separate")
    image = raw["image"]
    if not isinstance(image, str) or not image or any(c.isspace() for c in image):
        raise ValueError("image must be a Docker image reference")
    worker_command = _command(raw["worker_command"], "worker_command")
    checks = raw["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError("checks must be a nonempty list")
    commands = tuple(_command(item, "check") for item in checks)
    max_parallel, poll_seconds = raw["max_parallel"], raw["poll_seconds"]
    if type(max_parallel) is not int or not 1 <= max_parallel <= 8:
        raise ValueError("max_parallel must be 1..8")
    if type(poll_seconds) is not int or not 10 <= poll_seconds <= 3600:
        raise ValueError("poll_seconds must be 10..3600")
    worker_env = raw.get("worker_env", [])
    if (not isinstance(worker_env, list) or any(
            not isinstance(name, str) or not _ENV.fullmatch(name) for name in worker_env)
            or len(worker_env) != len(set(worker_env))):
        raise ValueError("worker_env must be unique environment variable names")
    if any(any(part in name.upper() for part in ("GITHUB", "GH_", "GIT_", "SSH_"))
           for name in worker_env):
        raise ValueError("repository and SSH credentials cannot enter worker container")
    publish_token_env = raw.get("publish_token_env")
    if publish_repo is not None:
        if (not isinstance(publish_token_env, str)
                or not _ENV.fullmatch(publish_token_env)
                or publish_token_env in ("GH_TOKEN", "GITHUB_TOKEN")
                or publish_token_env in worker_env):
            raise ValueError("publish_repo requires a separate publish_token_env")
    elif publish_token_env is not None:
        raise ValueError("publish_token_env requires publish_repo")
    base_branch = raw.get("base_branch", "main")
    if (not isinstance(base_branch, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", base_branch)
            or ".." in base_branch or base_branch.endswith("/") or "//" in base_branch):
        raise ValueError("unsafe base_branch")
    model_rows = raw.get("models", [])
    if not isinstance(model_rows, list) or len(model_rows) > 32:
        raise ValueError("models must be an array with at most 32 entries")
    from .providers import candidate_from_dict
    models = tuple(candidate_from_dict(row) for row in model_rows)
    if publish_token_env and publish_token_env in {model.credential_env for model in models}:
        raise ValueError("publish_token_env cannot be a worker model credential")
    if len({model.ref for model in models}) != len(models):
        raise ValueError("model references must be unique")
    capacities = {model.provider_id: model.max_parallel for model in models}
    if any(model.max_parallel != capacities[model.provider_id] for model in models):
        raise ValueError("models on one provider must share max_parallel")
    if models and max_parallel > sum(capacities.values()):
        raise ValueError("max_parallel exceeds configured model capacity")
    routes = raw.get("routes", {})
    if (not isinstance(routes, dict) or len(routes) > 32
            or any(not isinstance(label, str) or not _ROUTE.fullmatch(label)
                   or label == "route:auto" or not isinstance(provider, str)
                   or provider not in capacities
                   for label, provider in routes.items())):
        raise ValueError("routes must map route labels to configured provider IDs")
    min_free_gb = raw.get("min_free_gb", 2)
    if type(min_free_gb) is not int or not 0 <= min_free_gb <= 1000:
        raise ValueError("min_free_gb must be 0..1000")
    draft_pr_socket = raw.get("draft_pr_socket")
    if draft_pr_socket is not None:
        if (not publish_repo or not isinstance(draft_pr_socket, str)
                or not Path(draft_pr_socket).is_absolute()
                or Path(draft_pr_socket).resolve() != Path(draft_pr_socket)):
            raise ValueError("draft_pr_socket requires a canonical absolute path and publish_repo")
        draft_pr_socket = Path(draft_pr_socket)
    draft_pr_evidence_dir = raw.get("draft_pr_evidence_dir")
    if draft_pr_evidence_dir is not None:
        if (draft_pr_socket is None or not isinstance(draft_pr_evidence_dir, str)
                or not Path(draft_pr_evidence_dir).is_absolute()
                or Path(draft_pr_evidence_dir).resolve() != Path(draft_pr_evidence_dir)
                or Path(draft_pr_evidence_dir) == state_dir
                or state_dir in Path(draft_pr_evidence_dir).parents
                or workspace in Path(draft_pr_evidence_dir).parents):
            raise ValueError("draft_pr_evidence_dir must be a separate canonical absolute path")
        draft_pr_evidence_dir = Path(draft_pr_evidence_dir)
    return Config(repo, maintainer, state_dir, workspace, image,
                  worker_command, commands, max_parallel, poll_seconds,
                  tuple(worker_env), base_branch, models, min_free_gb,
                  publish_repo, publish_token_env, dict(routes), draft_pr_socket,
                  draft_pr_evidence_dir)
