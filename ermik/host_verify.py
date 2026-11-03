"""Run a bounded, local verification gate without a hosted CI service."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


_ROOT = Path(__file__).resolve().parent.parent
_MAX_CASES = 40
_MAX_TEXT = 240
_MAX_JSON_BYTES = 32 * 1024
_WINDOWS_SKIPS = {
    "test_evidence_store.EvidenceStoreTests.test_shared_evidence_mode": "POSIX permissions",
    "test_evidence_store.EvidenceStoreTests.test_reject_world_writable_existing_file": "POSIX permissions",
    "test_evidence_store.EvidenceStoreTests.test_read_and_replace_legacy_0644_in_private_directory": "POSIX permissions",
    "test_evidence_store.EvidenceStoreTests.test_reject_legacy_0644_in_group_accessible_directory": "POSIX permissions",
    "test_evidence_store.EvidenceStoreTests.test_reject_unsafe_parent_directory": "POSIX permissions",
    "test_review_store.ReviewStoreTests.test_legacy_0644_record_in_private_directory_upgrades_on_save": "POSIX permissions",
    "test_review_store.ReviewStoreTests.test_group_writable_review_record_is_rejected": "POSIX permissions",
    "test_fork_git.ForkGitTests.test_fork_git_uses_only_scoped_askpass":
        "fork publisher runs on the POSIX server",
    "test_fork_git.ForkGitTests.test_nonzero_push_is_ambiguous_until_remote_reconciliation":
        "fork publisher runs on the POSIX server",
    "test_evidence_store.EvidenceStoreTests.test_reject_symlink_and_unsafe_existing_path":
        "symlinks unavailable",
    "test_review_store.ReviewStoreTests.test_load_and_save_reject_symlink":
        "symlinks unavailable",
    "test_release_gate.ReleaseGateTests.test_rejects_symlink":
        "symlink creation unavailable",
    "test_workspace_recovery.WorkspaceRecoveryTests.test_symlink_issue_directory_is_not_followed":
        "directory symlinks unavailable",
}


class _DiscardOutput(io.TextIOBase):
    def write(self, value: str) -> int:
        return len(value)


class _Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_ids: list[str] = []

    def addFailure(self, test, err):  # noqa: N802 - unittest callback
        super().addFailure(test, err)
        self.failure_ids.append(test.id())

    def addError(self, test, err):  # noqa: N802 - unittest callback
        super().addError(test, err)
        self.failure_ids.append(test.id())


def _allowed_skip(test_id: str, reason: str) -> bool:
    """Keep platform exceptions explicit; missing tools remain failures."""
    return os.name == "nt" and _WINDOWS_SKIPS.get(test_id) == reason


def _unit_suite() -> dict:
    suite = unittest.TestLoader().discover(str(_ROOT / "tests"), pattern="test_*.py")
    sink = _DiscardOutput()
    with redirect_stdout(sink), redirect_stderr(sink):
        result = unittest.TextTestRunner(stream=sink, verbosity=0,
                                         resultclass=_Results).run(suite)
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    unexpected = [(name, reason) for name, reason in skipped
                  if not _allowed_skip(name, reason)]
    failures = (result.failure_ids + [test.id() for test in result.unexpectedSuccesses])
    return {
        "result": ("passed" if result.wasSuccessful() and not unexpected
                   and result.testsRun > len(skipped) else "failed"),
        "run": result.testsRun,
        "executed": result.testsRun - len(skipped),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "failure_cases": failures[:_MAX_CASES],
        "failure_cases_truncated": len(failures) > _MAX_CASES,
        "skipped": len(skipped),
        "allowed_skips": len(skipped) - len(unexpected),
        "unexpected_skips": [{"case": name, "reason": reason[:_MAX_TEXT]}
                             for name, reason in unexpected[:_MAX_CASES]],
        "unexpected_skips_truncated": len(unexpected) > _MAX_CASES,
    }


def _probe(*args: str, timeout: int = 20) -> str | None:
    try:
        completed = subprocess.run(args, cwd=_ROOT, text=True, capture_output=True,
                                   env=_git_environment(), timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value[:_MAX_TEXT] if value else None


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        environment.pop(name, None)
    return environment


def _dirty_checkout() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=_ROOT, env=_git_environment(), capture_output=True, timeout=20, check=True,
    )
    return bool(completed.stdout)


def _host_check(script: str, image: str) -> dict:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            [sys.executable, str(_ROOT / "tests" / script), image], cwd=_ROOT,
            env=environment, text=True, capture_output=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"result": "failed", "reason": "timeout"}
    except OSError:
        return {"result": "failed", "reason": "launch_failed"}
    return {"result": "passed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode}


def verify(*, docker: bool = False, image: str | None = None) -> dict:
    if docker and not image:
        raise ValueError("--image is required with --docker")
    checkout_root = _probe("git", "rev-parse", "--show-toplevel")
    commit = _probe("git", "rev-parse", "HEAD")
    if checkout_root is None or Path(checkout_root).resolve() != _ROOT or commit is None:
        raise RuntimeError("cannot identify verification commit")
    dirty_before = _dirty_checkout()
    docker_version = _probe("docker", "version", "--format", "{{.Server.Version}}")
    image_id = (_probe("docker", "image", "inspect", "--format", "{{.Id}}", image)
                if image else None)
    report = {
        "commit": commit,
        "dirty": dirty_before,
        "python": sys.version.split()[0],
        "docker": {"server_version": docker_version, "image": image,
                   "image_id": image_id},
        "unit": _unit_suite(),
        "host": {"result": "not_requested"},
    }
    if docker:
        if docker_version is None or image_id is None:
            report["host"] = {"result": "failed", "reason": "docker_or_image_unavailable"}
        else:
            report["host"] = {
                "smoke": _host_check("host_smoke.py", image_id),
                "container_recovery": _host_check("host_container_recovery.py", image_id),
            }
    host_passed = (not docker or (report["host"].get("smoke", {}).get("result") == "passed"
                                  and report["host"].get("container_recovery", {}).get("result")
                                  == "passed"))
    report["dirty"] = dirty_before or _dirty_checkout()
    same_commit = _probe("git", "rev-parse", "HEAD") == commit
    report["result"] = ("passed" if report["unit"]["result"] == "passed"
                        and host_passed and not report["dirty"] and same_commit else "failed")
    if not same_commit:
        report["reason"] = "checkout_commit_changed"
    elif report["dirty"]:
        report["reason"] = "checkout_dirty"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify this checkout on the local host")
    parser.add_argument("--docker", action="store_true",
                        help="also run Docker smoke and container recovery checks")
    parser.add_argument("--image", help="image for Docker checks")
    args = parser.parse_args(argv)
    try:
        report = verify(docker=args.docker, image=args.image)
    except (OSError, ValueError, RuntimeError) as exc:
        report = {"result": "failed", "reason": str(exc)[:_MAX_TEXT]}
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
        payload = json.dumps({"result": "failed", "reason": "report_size_limit"})
    print(payload)
    return 0 if report["result"] == "passed" and "report_size_limit" not in payload else 1


if __name__ == "__main__":
    raise SystemExit(main())
