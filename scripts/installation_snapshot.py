"""Snapshot and restore a stopped controller's private state and workspace."""

import argparse
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


MAX_SCHEMA = 6
EXCLUDED_DB = {"state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm",
               "state.sqlite3-journal"}


def _digest(path):
    result = {}
    for entry in sorted(path.rglob("*")):
        name = entry.relative_to(path).as_posix()
        if entry.is_symlink():
            result[name] = ["link", os.readlink(entry)]
        elif entry.is_dir():
            result[name] = ["dir"]
        elif entry.is_file():
            h = hashlib.sha256()
            with entry.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    h.update(chunk)
            result[name] = ["file", entry.stat().st_size, h.hexdigest()]
        else:
            raise ValueError(f"unsupported snapshot entry: {name}")
    return result


def _db_check(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError("snapshot database is missing or invalid")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
    if version not in range(MAX_SCHEMA + 1) or integrity != "ok":
        raise ValueError("snapshot database has unsupported schema or failed integrity")
    return version


def _locations(config):
    data = json.loads(config.read_text(encoding="utf-8"))
    state_path = Path(data["state_dir"])
    workspace_path = Path(data["workspace"])
    if state_path.is_symlink() or workspace_path.is_symlink():
        raise ValueError("state and workspace cannot be symlinks")
    state = state_path.resolve(strict=True)
    workspace = workspace_path.resolve(strict=True)
    if (not state.is_dir() or not workspace.is_dir() or state == workspace
            or state in workspace.parents or workspace in state.parents):
        raise ValueError("state and workspace must be separate directories")
    return data["repo"], state, workspace


def _namespace(repo, state):
    return hashlib.sha256(f"{repo}\n{state}".encode()).hexdigest()[:32]


def _check_marker(directory, expected):
    marker = directory / "container-namespace"
    if marker.is_symlink() or (marker.exists() and marker.read_text(encoding="ascii").strip() != expected):
        raise ValueError("container namespace marker differs")


def backup(config, destination):
    repo, state, workspace = _locations(config)
    destination = destination.resolve()
    if (destination == state or destination == workspace or state in destination.parents
            or workspace in destination.parents or any((destination / name).exists()
            for name in ("manifest.json", "state.sqlite3", "state-files", "workspace"))):
        raise ValueError("snapshot destination is invalid or already populated")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect((state / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as source, \
                closing(sqlite3.connect(destination / "state.sqlite3")) as target:
            source.backup(target)
        version = _db_check(destination / "state.sqlite3")
        shutil.copytree(state, destination / "state-files", symlinks=True,
                        ignore=lambda root, names: EXCLUDED_DB if Path(root) == state else set())
        shutil.copytree(workspace, destination / "workspace", symlinks=True)
        expected = _namespace(repo, state)
        _check_marker(state, expected)
        _check_marker(destination / "state-files", expected)
        manifest = {"repo": repo, "state_dir": str(state), "workspace": str(workspace),
                    "schema": version, "state_files": _digest(destination / "state-files"),
                    "workspace_files": _digest(destination / "workspace"),
                    "database": _digest(destination)["state.sqlite3"]}
        (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        os.chmod(destination / "manifest.json", 0o600)
        os.chmod(destination / "state.sqlite3", 0o600)
    except BaseException:
        # An incomplete destination is deliberately left for inspection, without a valid manifest.
        (destination / "manifest.json").unlink(missing_ok=True)
        raise
    print(f"snapshot schema {version}: {destination}")


def restore(config, snapshot):
    repo, state, workspace = _locations(config)
    if snapshot == state or snapshot == workspace or state in snapshot.parents or workspace in snapshot.parents:
        raise ValueError("snapshot must be outside live directories")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("repo") != repo or manifest.get("state_dir") != str(state)
            or manifest.get("workspace") != str(workspace)):
        raise ValueError("snapshot installation identity differs")
    if (_digest(snapshot / "state-files") != manifest.get("state_files")
            or _digest(snapshot / "workspace") != manifest.get("workspace_files")
            or _digest(snapshot).get("state.sqlite3") != manifest.get("database")):
        raise ValueError("snapshot is incomplete or changed")
    version = _db_check(snapshot / "state.sqlite3")
    if version != manifest.get("schema"):
        raise ValueError("snapshot schema differs from manifest")
    expected = _namespace(repo, state)
    _check_marker(state, expected)
    _check_marker(snapshot / "state-files", expected)
    saved_state = state.with_name(f"{state.name}.post-upgrade-{snapshot.name}")
    saved_workspace = workspace.with_name(f"{workspace.name}.post-upgrade-{snapshot.name}")
    if saved_state.exists() or saved_workspace.exists():
        raise ValueError("post-upgrade preservation paths already exist")
    with tempfile.TemporaryDirectory(prefix="ermik-state-", dir=state.parent) as state_temp, \
            tempfile.TemporaryDirectory(prefix="ermik-workspace-", dir=workspace.parent) as workspace_temp:
        staged_state = Path(state_temp) / "state"
        staged_workspace = Path(workspace_temp) / "workspace"
        shutil.copytree(snapshot / "state-files", staged_state, symlinks=True)
        shutil.copy2(snapshot / "state.sqlite3", staged_state / "state.sqlite3")
        shutil.copytree(snapshot / "workspace", staged_workspace, symlinks=True)
        if (_digest(staged_state) != {**manifest["state_files"], "state.sqlite3": manifest["database"]}
                or _digest(staged_workspace) != manifest["workspace_files"]):
            raise ValueError("staged restore differs from snapshot")
        moved_state = moved_workspace = installed_state = installed_workspace = False
        try:
            state.rename(saved_state)
            moved_state = True
            workspace.rename(saved_workspace)
            moved_workspace = True
            staged_state.rename(state)
            installed_state = True
            staged_workspace.rename(workspace)
            installed_workspace = True
        except BaseException:
            if installed_workspace:
                workspace.rename(staged_workspace)
            if installed_state:
                state.rename(staged_state)
            if moved_workspace:
                saved_workspace.rename(workspace)
            if moved_state:
                saved_state.rename(state)
            raise
    print(f"restored schema {version}; prior state: {saved_state}; prior workspace: {saved_workspace}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "backup":
        backup(args.config, args.snapshot.absolute())
    else:
        restore(args.config, args.snapshot.resolve(strict=True))


if __name__ == "__main__":
    main()
