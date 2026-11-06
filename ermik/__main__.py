"""Command line entry point for the portable workflow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid

from .config import load_config
from .github import GitHub
from .review import merge, request_changes, review
from .runtime import admit, checked, main_sha, poll, run_once
from .observability import QUOTA_FILE, quota_status, status_snapshot
from .quota import probe, unknown
from .dashboard import create_server
from .retention import cleanup as cleanup_artifacts, inventory as inventory_artifacts
from .evidence import MAX_BYTES, dumps_manifest, loads_manifest
from .evidence_export import export_manifest, verify_manifest
from .evidence_transport import fetch_manifest, publish_manifest
from .delivery_loop import deliver_pending
from .review_packet import build_review_packet
from .maintainer_view import queue_snapshot, create_server as create_maintainer_server
from .decision_trail import read_decisions
from .outcomes import build_outcome_report, build_quality_snapshot


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
        gh.inspect_publish_identity()
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
            "publish_write_capabilities": "unverified",
            "docker_image": cfg.image, "state_dir_exists": cfg.state_dir.exists(),
            "workspace_exists": cfg.workspace.exists(),
            "models": [{"ref": model.ref,
                        "credential_available": bool(os.environ.get(model.credential_env))}
                       for model in cfg.models],
            "result": "preflight_passed"}


def status(cfg) -> dict:
    snapshot = status_snapshot(cfg.state_dir, max_parallel=cfg.max_parallel,
                               ready_issues=GitHub(cfg.repo).ready_issues(),
                               quota_providers=(model.provider_id for model in cfg.models),
                               quota_sources={provider: source.kind for provider, source
                                              in cfg.quota_sources.items()})
    return {"repo": cfg.repo, **snapshot}


def _write_private_output(output: Path, data: bytes) -> None:
    if output.parent.resolve() != output.parent.absolute() or output.is_symlink():
        raise ValueError("unsafe evidence output path")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current = output.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                output.unlink()
        except FileNotFoundError:
            pass
        raise


def refresh_quota(cfg) -> dict:
    """Probe only configured loopback sources and store their bounded projection."""
    if not cfg.state_dir.is_dir() or cfg.state_dir.is_symlink():
        raise ValueError("private state directory is required for quota refresh")
    if os.name == "posix":
        info = cfg.state_dir.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("quota state directory must be private")
    rows = []
    for provider, source in sorted(cfg.quota_sources.items()):
        credential = os.environ.get(source.auth_env) if source.auth_env else None
        observed = (unknown(source.kind, "unavailable")
                    if source.auth_env and not credential else
                    probe(source.kind, source.port, credential))
        rows.append({"provider": provider, **observed})
    target = cfg.state_dir / QUOTA_FILE
    if target.is_symlink():
        raise ValueError("quota observation file cannot be a symlink")
    temporary = cfg.state_dir / (QUOTA_FILE + "." + uuid.uuid4().hex + ".tmp")
    try:
        _write_private_output(temporary, (json.dumps(
            {"version": 1, "providers": rows}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8"))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"repo": cfg.repo,
            "quota_observations": quota_status(
                cfg.state_dir, (model.provider_id for model in cfg.models),
                sources={provider: source.kind for provider, source
                         in cfg.quota_sources.items()})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local issue-to-PR workflow")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "run-once", "poll", "quota-refresh", "quota-status"):
        commands.add_parser(name)
    storage_parser = commands.add_parser("storage")
    storage_parser.add_argument("--cleanup", action="store_true")
    storage_parser.add_argument("--apply", action="store_true")
    storage_parser.add_argument("--max-items", type=int, default=32)
    storage_parser.add_argument("--max-bytes", type=int, default=268435456)
    dashboard_parser = commands.add_parser("dashboard")
    dashboard_parser.add_argument("--port", type=int, default=8765)
    commands.add_parser("maintainer-queue")
    maintainer_view_parser = commands.add_parser("maintainer-view")
    maintainer_view_parser.add_argument("--port", type=int, default=8766)
    admit_parser = commands.add_parser("admit")
    admit_parser.add_argument("issue", type=int)
    review_parser = commands.add_parser("review")
    review_parser.add_argument("pr", type=int)
    packet_parser = commands.add_parser("review-packet")
    packet_parser.add_argument("pr", type=int)
    history_parser = commands.add_parser("decision-history")
    history_parser.add_argument("pr", type=int)
    outcomes_parser = commands.add_parser("outcomes")
    outcomes_parser.add_argument("--maintainer-state-dir", type=Path, required=True)
    outcomes_parser.add_argument("--period", help="UTC admission month, YYYY-MM")
    metrics_parser = commands.add_parser("route-metrics")
    metrics_parser.add_argument("--maintainer-state-dir", type=Path, required=True)
    metrics_parser.add_argument("--output", type=Path, required=True)
    correction_parser = commands.add_parser("request-changes")
    correction_parser.add_argument("pr", type=int)
    correction_parser.add_argument("--note-file", type=Path, required=True)
    merge_parser = commands.add_parser("merge")
    merge_parser.add_argument("pr", type=int)
    export_parser = commands.add_parser("export-evidence")
    export_parser.add_argument("pr", type=int)
    export_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify-evidence")
    verify_parser.add_argument("pr", type=int)
    verify_parser.add_argument("--input", type=Path, required=True)
    publish_parser = commands.add_parser("publish-evidence")
    publish_parser.add_argument("pr", type=int)
    fetch_parser = commands.add_parser("fetch-evidence")
    fetch_parser.add_argument("pr", type=int)
    fetch_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "status":
            result = status(cfg)
        elif args.command == "maintainer-queue":
            result = queue_snapshot(cfg)
        elif args.command == "quota-refresh":
            result = refresh_quota(cfg)
        elif args.command == "quota-status":
            result = {"repo": cfg.repo,
                      "quota_observations": quota_status(
                          cfg.state_dir, (model.provider_id for model in cfg.models),
                          sources={provider: source.kind for provider, source
                                   in cfg.quota_sources.items()})}
        elif args.command == "admit":
            result = admit(cfg, args.issue)
        elif args.command == "run-once":
            try:
                work = run_once(cfg)
            except Exception:
                if getattr(cfg, "evidence_publisher", None):
                    try:
                        deliver_pending(cfg)
                    except Exception:
                        pass
                raise
            result = ({"work": work, "delivery": deliver_pending(cfg)}
                      if getattr(cfg, "evidence_publisher", None) else work)
        elif args.command == "storage":
            if args.apply and not args.cleanup:
                raise ValueError("--apply requires --cleanup")
            result = (cleanup_artifacts(cfg, dry_run=not args.apply,
                                        max_items=args.max_items,
                                        max_bytes=args.max_bytes)
                      if args.cleanup else inventory_artifacts(cfg))
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
                                   port=args.port, ready_issues=ready,
                                   quota_providers=tuple(model.provider_id
                                                         for model in cfg.models),
                                   quota_sources={provider: source.kind
                                                  for provider, source in
                                                  cfg.quota_sources.items()})
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        elif args.command == "maintainer-view":
            server = create_maintainer_server(cfg, port=args.port)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        elif args.command == "review":
            result = review(cfg, args.pr)
        elif args.command == "review-packet":
            result = build_review_packet(cfg, args.pr)
        elif args.command == "decision-history":
            history = read_decisions(cfg, args.pr)
            result = {"status": "historical_only", "approval": "none",
                      **history}
        elif args.command == "outcomes":
            result = build_outcome_report(cfg, args.maintainer_state_dir,
                                          period=args.period)
        elif args.command == "route-metrics":
            snapshot = build_quality_snapshot(cfg, args.maintainer_state_dir)
            _write_private_output(args.output, (json.dumps(snapshot, sort_keys=True,
                separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8"))
            result = {"repo": cfg.repo, "output": str(args.output),
                      "providers": len(snapshot["providers"]), "result": "written"}
        elif args.command == "request-changes":
            result = request_changes(cfg, args.pr, args.note_file)
        elif args.command == "merge":
            result = merge(cfg, args.pr)
        elif args.command == "export-evidence":
            record = export_manifest(cfg, args.pr)
            _write_private_output(args.output, dumps_manifest(record))
            result = {"pr": args.pr, "status": "exported", "sha256": record["sha256"]}
        elif args.command == "publish-evidence":
            result = publish_manifest(cfg, args.pr)
        elif args.command == "fetch-evidence":
            result, record = fetch_manifest(cfg, args.pr)
            if result["status"] == "current" and record is not None:
                _write_private_output(args.output, dumps_manifest(record))
        elif args.command == "verify-evidence":
            source = args.input
            if source.is_symlink():
                raise ValueError("invalid evidence input file")
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("invalid evidence input file")
                data = stream.read(MAX_BYTES + 2)
            if len(data) > MAX_BYTES + 1:
                raise ValueError("invalid evidence input file")
            record = loads_manifest(data)
            result = verify_manifest(cfg, args.pr, record)
        else:
            raise AssertionError("unhandled command")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.command == "merge" and result.get("status") == "incomplete":
            return 2
        if args.command == "verify-evidence" and result.get("status") != "current":
            return 2
        if args.command == "fetch-evidence" and result.get("status") != "current":
            return 2
        if args.command == "review-packet" and result.get("status") != "current":
            return 2
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
