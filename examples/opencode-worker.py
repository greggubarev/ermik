#!/usr/bin/env python3
"""Disposable OpenCode worker. The controller owns repository checks and publication."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from ermik.providers import (ModelCandidate, PROVIDERS, candidate_from_dict,
                                classify_failure, opencode_command)


TASK = Path("/task/issue.md")
WORK = Path("/work")
USAGE_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write",
                "total", "estimated_cost")
EXPORT_LIMIT = 2 * 1024 * 1024
EXPORT_TIMEOUT = 20
SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{4,128}\Z")


def _selected() -> ModelCandidate:
    ref = os.environ.get("ERMIK_MODEL_REF", "")
    raw_spec = os.environ.get("ERMIK_MODEL_SPEC")
    if raw_spec:
        try:
            candidate = candidate_from_dict(json.loads(raw_spec))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid selected model specification") from exc
        if ref and ref != candidate.ref:
            raise ValueError("selected model reference differs from specification")
        return candidate
    provider, separator, model = ref.partition("/")
    if not separator or provider not in PROVIDERS:
        raise ValueError("invalid selected model")
    return ModelCandidate(provider, model, PROVIDERS[provider][0])


def _config(candidate: ModelCandidate) -> dict:
    api_key = os.environ[candidate.credential_env]
    if candidate.provider_id == "opencode-go":
        provider = {"options": {"apiKey": api_key}}
    else:
        provider = {
            "npm": candidate.runtime_package,
            "name": candidate.provider_id,
            "options": {
                "baseURL": candidate.endpoint,
                "apiKey": api_key,
            },
            "models": {candidate.model_id: {"name": candidate.model_id}},
        }
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {candidate.provider_id: provider},
        "permission": {
            "*": "deny",
            "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny"},
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "edit": "allow",
            "bash": "deny",
            "task": "deny",
            "external_directory": "deny",
        },
        "share": "disabled",
        "autoupdate": False,
        "agent": {"build": {"steps": 24}},
    }


def _unavailable_usage() -> dict:
    return {**{key: None for key in USAGE_FIELDS}, "complete": False}


def _number(value: object, *, integer: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid usage value")
    if not math.isfinite(value) or value < 0 or (integer and not isinstance(value, int)):
        raise ValueError("invalid usage value")
    return value


def _parse_usage(document: object, session_id: str) -> dict:
    if not isinstance(document, dict) or not isinstance(document.get("info"), dict):
        raise ValueError("invalid session export")
    if document["info"].get("id") != session_id:
        raise ValueError("unexpected session export")
    messages = document.get("messages")
    if not isinstance(messages, list):
        raise ValueError("missing messages")
    usage = {key: 0 for key in USAGE_FIELDS}
    count = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise ValueError("invalid message")
        info = message["info"]
        if info.get("role") != "assistant":
            continue
        tokens = info.get("tokens")
        if not isinstance(tokens, dict) or not isinstance(tokens.get("cache"), dict):
            raise ValueError("missing token usage")
        cache = tokens["cache"]
        values = {
            "input": tokens.get("input"), "output": tokens.get("output"),
            "reasoning": tokens.get("reasoning"), "cache_read": cache.get("read"),
            "cache_write": cache.get("write"), "total": tokens.get("total"),
            "estimated_cost": info.get("cost"),
        }
        for key, value in values.items():
            usage[key] += _number(value, integer=key != "estimated_cost")
        count += 1
    if count == 0:
        raise ValueError("no assistant usage")
    return {**usage, "complete": True}


def _bounded_cli(command: list[str], environment: dict[str, str]) -> bytes:
    # The CLI output contains transcript text. Keep it in a private temporary file,
    # never in logs, and stop the process if its output exceeds the small usage budget.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=WORK, env=environment,
                                   stdout=output, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + EXPORT_TIMEOUT
        try:
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > EXPORT_LIMIT or time.monotonic() >= deadline:
                    raise ValueError("session command limit exceeded")
                time.sleep(0.05)
            if process.returncode != 0 or os.fstat(output.fileno()).st_size > EXPORT_LIMIT:
                raise ValueError("session command failed")
            output.seek(0)
            return output.read(EXPORT_LIMIT + 1)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise


def _session_usage(environment: dict[str, str]) -> dict:
    try:
        listing = json.loads(_bounded_cli(
            ["opencode", "session", "list", "--max-count", "2", "--format", "json"],
            environment))
        if not isinstance(listing, list) or len(listing) != 1:
            raise ValueError("ambiguous session")
        session_id = listing[0].get("id") if isinstance(listing[0], dict) else None
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session ID")
        exported = json.loads(_bounded_cli(["opencode", "export", session_id], environment))
        return _parse_usage(exported, session_id)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return _unavailable_usage()


def _print_usage(environment: dict[str, str]) -> None:
    print("WORKER_USAGE " + json.dumps(_session_usage(environment),
                                       separators=(",", ":"), allow_nan=False))


def _child_environment(candidate: ModelCandidate, config_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (details[0] for details in PROVIDERS.values()):
        if name != candidate.credential_env:
            environment.pop(name, None)
    for name in tuple(environment):
        if (name != candidate.credential_env and any(
                fragment in name.upper() for fragment in
                ("API_KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL"))):
            environment.pop(name, None)
    environment.pop("ERMIK_MODEL_SPEC", None)
    environment["OPENCODE_CONFIG"] = str(config_path)
    environment.pop("OPENCODE_CONFIG_CONTENT", None)
    environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    environment["HOME"] = str(config_path.parent)
    environment["XDG_CONFIG_HOME"] = str(config_path.parent)
    environment["XDG_DATA_HOME"] = str(config_path.parent)
    environment["XDG_CACHE_HOME"] = str(config_path.parent)
    return environment


def main() -> int:
    try:
        candidate = _selected()
        if not os.environ.get(candidate.credential_env):
            raise ValueError("selected provider credential missing")
        if not TASK.is_file() or not WORK.is_dir():
            raise ValueError("task mount or worktree missing")
        prompt = TASK.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("empty task")
    except (OSError, ValueError):
        print("WORKER_CONFIGURATION_ERROR")
        return 78

    with tempfile.TemporaryDirectory(prefix="opencode-", dir="/tmp") as temp:
        config_path = Path(temp) / "opencode.json"
        config_path.write_text(json.dumps(_config(candidate)), encoding="utf-8")
        config_path.chmod(0o600)
        environment = _child_environment(candidate, config_path)
        try:
            command = opencode_command(candidate, prompt)
            result = subprocess.run(command, cwd=WORK,
                                    env=environment, capture_output=True, text=True,
                                    timeout=3300, check=False)
        except (OSError, subprocess.TimeoutExpired):
            _print_usage(environment)
            print("WORKER_EXECUTION_ERROR")
            return 1
        _print_usage(environment)
    if result.returncode == 0:
        print("WORKER_OK")
        return 0
    failure = classify_failure(result.stdout, result.stderr, result.returncode)
    if failure == "quota":
        print("WORKER_QUOTA_EXHAUSTED")
        return 75
    if failure == "auth":
        print("WORKER_AUTH_FAILURE")
        return 77
    print("WORKER_EXECUTION_ERROR")
    return 1


if __name__ == "__main__":
    sys.exit(main())
