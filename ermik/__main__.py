"""Command line entry point for the portable workflow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .config import load_config
from .github import GitHub
from .review import merge, request_changes, review
from .runtime import admit, checked, main_sha, poll, run_once
from .observability import status_snapshot
from .dashboard import create_server


def doctor(cfg) -> dict:
    required_env = {model.credential_env for model in cfg.models} | set(cfg.worker_env)
    if cfg.publish_repo and cfg.publish_token_env:
        required_env.add(cfg.publish_token_env)
    missing_env = sorted(name for name in required_env if not os.environ.get(name))
    if missing_env:
        raise ValueError("required worker credentials are unavailable: "
                         + ", ".join(missing_env))
    gh = (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
          if cfg.publish_repo else GitHub(cfg.repo))
    if cfg.publish_repo:
        gh.validate_publish_access()
    repo = gh.command(["repo", "view", cfg.repo, "--json", "nameWithOwner,defaultBranchRef"])
    details = json.loads(repo)
    if details["nameWithOwner"].casefold() != cfg.repo.casefold():
        raise ValueError("GitHub repository identity differs")
    if details["defaultBranchRef"]["name"] != cfg.base_branch:
        raise ValueError("configured base_branch differs from repository default")
    account = gh.identity()
    permission = gh.api(f"collaborators/{account}/permission")["permission"]
    if cfg.publish_repo:
        if permission not in ("admin", "maintain", "write", "triage", "read"):
            raise ValueError("GitHub account cannot read the target repository")
    elif permission not in ("admin", "maintain", "write"):
        raise ValueError("GitHub account cannot publish branches and pull requests")
    checked(["git", "--version"], timeout=10)
    checked(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    checked(["docker", "image", "inspect", cfg.image], timeout=20)
    for path in (cfg.state_dir, cfg.workspace):
        if path.exists() and os.name != "nt":
            info = path.stat()
            if not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError(f"path must be a private directory owned by caller: {path}")
    return {"repo": cfg.repo, "publish_repo": cfg.publish_repo,
            "base": main_sha(gh, cfg), "github_account": account,
            "github_permission": permission,
            "docker_image": cfg.image, "state_dir_exists": cfg.state_dir.exists(),
            "workspace_exists": cfg.workspace.exists(),
            "models": [{"ref": model.ref,
                        "credential_available": bool(os.environ.get(model.credential_env))}
                       for model in cfg.models],
            "result": "preflight_passed"}


def status(cfg) -> dict:
    snapshot = status_snapshot(cfg.state_dir, max_parallel=cfg.max_parallel,
                               ready_issues=GitHub(cfg.repo).ready_issues())
    return {"repo": cfg.repo, **snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local issue-to-PR workflow")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "run-once", "poll"):
        commands.add_parser(name)
    dashboard_parser = commands.add_parser("dashboard")
    dashboard_parser.add_argument("--port", type=int, default=8765)
    admit_parser = commands.add_parser("admit")
    admit_parser.add_argument("issue", type=int)
    review_parser = commands.add_parser("review")
    review_parser.add_argument("pr", type=int)
    correction_parser = commands.add_parser("request-changes")
    correction_parser.add_argument("pr", type=int)
    correction_parser.add_argument("--note-file", type=Path, required=True)
    merge_parser = commands.add_parser("merge")
    merge_parser.add_argument("pr", type=int)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "status":
            result = status(cfg)
        elif args.command == "admit":
            result = admit(cfg, args.issue)
        elif args.command == "run-once":
            result = run_once(cfg)
        elif args.command == "poll":
            poll(cfg)
            return 0
        elif args.command == "dashboard":
            last = {"at": 0.0, "issues": []}

            def ready() -> list[int]:
                if time.monotonic() - last["at"] >= 60:
                    last["issues"] = GitHub(cfg.repo).ready_issues()
                    last["at"] = time.monotonic()
                return last["issues"]

            server = create_server(cfg.state_dir, max_parallel=cfg.max_parallel,
                                   port=args.port, ready_issues=ready)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        elif args.command == "review":
            result = review(cfg, args.pr)
        elif args.command == "request-changes":
            result = request_changes(cfg, args.pr, args.note_file)
        elif args.command == "merge":
            result = merge(cfg, args.pr)
        else:
            raise AssertionError("unhandled command")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
