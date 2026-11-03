"""Opt-in Linux host exercise for the maintainer merge recovery boundary.

Run with an existing disposable private repository and a scoped GH_TOKEN::

    python3 tests/live_merge_matrix.py --repo OWNER/REPO \
        --image ermik-demo:latest --report /private/merge-matrix.json

The named repository must have an initial commit, allow squash merges, and have
the queue:worker, state:proposed, state:ready and state:in-progress labels.
Each case creates an issue, branch and PR. Remote history and negative-case PRs
are intentionally left for inspection. Local state is temporary. This script
never reads, prints or stores the token value.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ermik import review as review_module  # noqa: E402
from ermik.config import load_config  # noqa: E402
from ermik.github import GitHub  # noqa: E402
from ermik.runtime import WorkflowError, admit  # noqa: E402


CASES = (
    "same_head", "after_attempt", "after_remote_merge", "before_postchecks",
    "before_final_save", "changed_head", "closed_unmerged", "failed_postcheck",
)
KILL_CASES = ("after_attempt", "after_remote_merge", "before_postchecks",
              "before_final_save")


class MatrixFailure(Exception):
    """A failed assertion with a safe, fixed message."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise MatrixFailure(reason)


def command(args: list[str], *, cwd: Path | None = None,
            timeout: int = 300, input_text: str | None = None) -> str:
    # Never include stderr in a report: gh/git can echo credential helpers or URLs.
    try:
        done = subprocess.run(args, cwd=cwd, input=input_text, text=True,
                              capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MatrixFailure(f"{args[0]} unavailable or timed out") from exc
    if done.returncode:
        raise MatrixFailure(f"{args[0]} exited {done.returncode}")
    return done.stdout.strip()


def gh_json(*args: str) -> dict:
    value = json.loads(command(["gh", *args]))
    require(isinstance(value, dict), "GitHub response is not an object")
    return value


def pr_remote(repo: str, number: int) -> dict:
    return gh_json("api", f"repos/{repo}/pulls/{number}")


def base_sha(repo: str, branch: str) -> str:
    value = gh_json("api", f"repos/{repo}/git/ref/heads/{branch}")
    return value["object"]["sha"]


def remote_identity(repo: str, number: int, branch: str, head: str,
                    base: str, issue: int, *, state: str) -> dict:
    pr = pr_remote(repo, number)
    require(pr.get("number") == number and pr.get("base", {}).get("repo", {}).get("full_name") == repo,
            "remote PR repository or number changed")
    require(pr.get("head", {}).get("repo", {}).get("full_name") == repo
            and pr.get("head", {}).get("ref") == branch
            and pr.get("head", {}).get("sha") == head,
            "remote PR branch or reviewed HEAD changed")
    require(pr.get("base", {}).get("ref") == base
            and re.findall(r"(?im)\bCloses\s+#(\d+)\b", pr.get("body") or "") == [str(issue)],
            "remote base or issue link changed")
    require(("merged" if pr.get("merged_at") else pr.get("state")) == state,
            "remote PR state differs")
    return pr


def result_record(cfg, number: int) -> dict:
    return review_module._load(cfg, number)


def cli_merge(config: Path, number: int) -> tuple[int, dict | None]:
    try:
        done = subprocess.run([sys.executable, "-m", "ermik", "--config", str(config),
                               "merge", str(number)], cwd=ROOT, text=True,
                              capture_output=True, timeout=1200, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MatrixFailure("merge command unavailable or timed out") from exc
    try:
        result = json.loads(done.stdout) if done.stdout else None
    except ValueError:
        result = None
    return done.returncode, result


def guarded_repeat(cfg, number: int) -> dict:
    """Use real remote reads but fail if a second merge request is attempted."""
    original = GitHub.command

    def guard(self, args, **kwargs):
        if args[:2] == ["pr", "merge"]:
            raise MatrixFailure("recovery attempted a second merge request")
        return original(self, args, **kwargs)

    GitHub.command = guard
    try:
        return review_module.merge(cfg, number)
    finally:
        GitHub.command = original


def killed_merge(config: Path, number: int, boundary: str) -> None:
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child",
                           boundary, "--repo", load_config(config).repo,
                           "--config", str(config), "--pr", str(number)],
                          cwd=ROOT, text=True, capture_output=True, timeout=1200,
                          check=False)
    require(done.returncode == -signal.SIGKILL,
            f"fault boundary {boundary} was not reached")


def child(boundary: str, config: Path, number: int) -> None:
    cfg = load_config(config)
    kill = lambda: os.kill(os.getpid(), signal.SIGKILL)
    if boundary == "after_attempt":
        original = review_module._save

        def save_then_kill(c, n, value):
            original(c, n, value)
            if "merge_attempt" in value and "merge_commit" not in value:
                kill()

        review_module._save = save_then_kill
    elif boundary == "after_remote_merge":
        original = GitHub.command

        def merge_then_kill(self, args, **kwargs):
            answer = original(self, args, **kwargs)
            if args[:2] == ["pr", "merge"]:
                observed = self.pr(number)
                if observed["state"] == "MERGED":
                    kill()
            return answer

        GitHub.command = merge_then_kill
    elif boundary == "before_postchecks":
        review_module._post_merge_verification = lambda *_args: kill()
    elif boundary == "before_final_save":
        original = review_module._save

        def kill_before_save(c, n, value):
            if value.get("merged", {}).get("status") == "complete":
                kill()
            return original(c, n, value)

        review_module._save = kill_before_save
    else:
        raise MatrixFailure("unknown child boundary")
    review_module.merge(cfg, number)
    raise MatrixFailure("merge returned without reaching fault boundary")


def prepare_case(repo: str, base: str, image: str, root: Path,
                 case: str) -> tuple[Path, int, int, str, str, str]:
    suffix = uuid.uuid4().hex[:10]
    case_root = root / case
    state = case_root / "state"
    workspace = case_root / "work"
    checkout = case_root / "checkout"
    for directory in (state, workspace):
        directory.mkdir(parents=True, mode=0o700)
    path = f"live-matrix/{suffix}.txt"
    body = (f"## Goal\n\nAdd the unique marker for {case}.\n\n"
            f"## Allowed paths\n\n{path}\n\n"
            f"## Acceptance\n\nThe marker is exactly {suffix}.\n")
    issue_url = command(["gh", "issue", "create", "-R", repo, "--title",
                         f"Merge recovery {case} {suffix}", "--body", body,
                         "--label", "queue:worker", "--label", "state:proposed"])
    issue = int(issue_url.rstrip("/").split("/")[-1])
    branch = f"work/issue-{issue}-merge-{suffix}"
    config = case_root / "config.json"
    check = ("from pathlib import Path; "
             f"assert Path({path!r}).read_text().strip() == {suffix!r}")
    data = {"repo": repo, "maintainer": repo.split("/")[0],
            "base_branch": base, "state_dir": str(state), "workspace": str(workspace),
            "image": image, "worker_command": ["/bin/true"],
            "checks": [["python3", "-c", check]], "max_parallel": 1,
            "poll_seconds": 60}
    config.write_text(json.dumps(data), encoding="utf-8")
    config.chmod(0o600)
    cfg = load_config(config)
    admit(cfg, issue)
    command(["gh", "issue", "edit", str(issue), "-R", repo,
             "--remove-label", "state:ready", "--add-label", "state:in-progress"])
    before = base_sha(repo, base)
    command(["git", "clone", "--quiet", "--single-branch", "--branch", base,
             f"https://github.com/{repo}.git", str(checkout)])
    require(command(["git", "rev-parse", "HEAD"], cwd=checkout) == before,
            "base moved during case setup")
    command(["git", "checkout", "-q", "-b", branch], cwd=checkout)
    target = checkout / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(suffix + "\n", encoding="utf-8")
    command(["git", "add", "--", path], cwd=checkout)
    login = repo.split("/", 1)[0]
    command(["git", "-c", f"user.name={login}", "-c",
             f"user.email={login}@users.noreply.github.com", "commit", "-qm",
             f"Add merge marker {suffix}"], cwd=checkout)
    head = command(["git", "rev-parse", "HEAD"], cwd=checkout)
    command(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
    pr_url = command(["gh", "pr", "create", "-R", repo, "--base", base,
                      "--head", branch, "--draft", "--title",
                      f"Merge recovery {case} {suffix}", "--body", f"Closes #{issue}"])
    pr = int(pr_url.rstrip("/").split("/")[-1])
    remote_identity(repo, pr, branch, head, base, issue, state="open")
    if case == "failed_postcheck":
        # The CI snapshot exposes ci-head in both PR and main checkouts. This
        # deliberately passes the reviewed HEAD and fails the squash commit.
        data["checks"][0][2] += ("; assert Path('/ci-git/refs/heads/ci-head')"
                                  f".read_text().strip() == {head!r}")
        config.write_text(json.dumps(data), encoding="utf-8")
        config.chmod(0o600)
        cfg = load_config(config)
    record = review_module.review(cfg, pr)
    require(record["head"] == head and record["base"] == before
            and record["issue"] == issue and record["repo"] == repo,
            "review identity differs")
    return config, issue, pr, branch, head, before


def run_case(repo: str, base: str, image: str, root: Path, case: str) -> dict:
    config, issue, number, branch, head, before = prepare_case(repo, base, image, root, case)
    cfg = load_config(config)
    row = {"case": case, "issue": issue, "pr": number, "head": head,
           "reviewed_base": before, "status": "failed"}
    if case == "same_head":
        code, result = cli_merge(config, number)
        observed = remote_identity(repo, number, branch, head, base, issue,
                                   state="merged")
        oid = observed["merge_commit_sha"]
        require(code == 0 and result is not None and result.get("status") == "complete"
                and result.get("merge_commit") == oid
                and result.get("main_ci", {}).get("result") == "passed"
                and result.get("issue_closed") is True,
                "same-HEAD merge did not complete")
        again = guarded_repeat(cfg, number)
        require(again == result
                and pr_remote(repo, number).get("merge_commit_sha") == oid,
                "same-HEAD retry changed outcome")
        row.update(status="passed", outcome="complete", merge_commit=oid)
        return row
    if case in KILL_CASES:
        killed_merge(config, number, case)
        stored = result_record(cfg, number)
        require(stored.get("merge_attempt") == {"head": head, "base": before,
                                                "branch": branch},
                "durable merge attempt missing")
        if case == "after_attempt":
            remote_identity(repo, number, branch, head, base, issue, state="open")
            result = guarded_repeat(cfg, number)
            require(result is not None
                    and result.get("status") == "incomplete"
                    and result.get("reason") == "merge outcome is unconfirmed",
                    "pre-request recovery did not remain uncertain")
            remote_identity(repo, number, branch, head, base, issue, state="open")
            require("merge_commit" not in result_record(cfg, number),
                    "pre-request recovery recorded a merge")
            row.update(status="passed", outcome="unconfirmed_without_retry")
            return row
        observed = remote_identity(repo, number, branch, head, base, issue, state="merged")
        merge_oid = observed.get("merge_commit_sha")
        require(isinstance(merge_oid, str) and len(merge_oid) == 40,
                "merge commit absent")
        require(base_sha(repo, base) == merge_oid, "base is not exact merge commit")
        require(gh_json("api", f"repos/{repo}/git/commits/{merge_oid}")["parents"][0]["sha"] == before,
                "merge commit parent differs from reviewed base")
        code, result = cli_merge(config, number)
        require(code == 0 and result is not None and result.get("status") == "complete"
                and result.get("merge_commit") == merge_oid
                and result.get("main_ci", {}).get("result") == "passed"
                and result.get("issue_closed") is True,
                "merged recovery did not verify main")
        require(result_record(cfg, number).get("merged") == result,
                "final merge result not durable")
        again = guarded_repeat(cfg, number)
        require(again == result
                and pr_remote(repo, number).get("merge_commit_sha") == merge_oid,
                "repeat merge changed outcome")
        row.update(status="passed", outcome="complete", merge_commit=merge_oid)
        return row
    if case == "changed_head":
        checkout = root / case / "checkout"
        marker = next((checkout / "live-matrix").glob("*.txt"))
        marker.write_text(marker.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
        command(["git", "add", "--", str(marker.relative_to(checkout))], cwd=checkout)
        login = repo.split("/", 1)[0]
        command(["git", "-c", f"user.name={login}", "-c",
                 f"user.email={login}@users.noreply.github.com", "commit", "-qm",
                 "Change reviewed marker"], cwd=checkout)
        changed = command(["git", "rev-parse", "HEAD"], cwd=checkout)
        command(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
        code, result = cli_merge(config, number)
        require(code == 1 and result is None, "changed HEAD was accepted")
        require(pr_remote(repo, number)["head"]["sha"] == changed
                and pr_remote(repo, number).get("merged_at") is None
                and "merge_attempt" not in result_record(cfg, number),
                "changed HEAD created merge attempt")
        row.update(status="passed", outcome="rejected_changed_head")
        return row
    if case == "closed_unmerged":
        command(["gh", "pr", "close", str(number), "-R", repo])
        code, result = cli_merge(config, number)
        require(code == 1 and result is None, "closed PR was accepted")
        remote_identity(repo, number, branch, head, base, issue, state="closed")
        require("merge_attempt" not in result_record(cfg, number),
                "closed PR created merge attempt")
        row.update(status="passed", outcome="rejected_closed_unmerged")
        return row
    code, result = cli_merge(config, number)
    require(code == 2 and result is not None and result.get("status") == "incomplete"
            and result.get("reason") == "post-merge checks failed"
            and result.get("main_ci") is None,
            "failed postcheck claimed success")
    observed = remote_identity(repo, number, branch, head, base, issue, state="merged")
    oid = observed["merge_commit_sha"]
    require(result.get("merge_commit") == oid and base_sha(repo, base) == oid,
            "failed postcheck lost merge identity")
    again = guarded_repeat(cfg, number)
    require(again == result
            and pr_remote(repo, number)["merge_commit_sha"] == oid,
            "failed postcheck retry changed merge")
    row.update(status="passed", outcome="incomplete_postcheck", merge_commit=oid)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--image", default="ermik-demo:latest")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--child", choices=KILL_CASES)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    if args.child:
        require(args.config is not None and args.pr is not None,
                "child requires config and PR")
        child(args.child, args.config, args.pr)
        return 1
    report = {"repository": args.repo, "cases": [], "status": "failed"}
    try:
        require(sys.platform == "linux", "Linux host required")
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", args.repo)),
                "repository must be an explicit owner/name")
        require(bool(os.environ.get("GH_TOKEN")), "GH_TOKEN is required")
        details = gh_json("api", f"repos/{args.repo}")
        require(details.get("full_name") == args.repo and details.get("private") is True,
                "repository identity or privacy differs")
        base = details["default_branch"]
        require(details.get("allow_squash_merge") is True, "squash merge unavailable")
        require(gh_json("api", "user")["login"] == args.repo.split("/")[0],
                "token account differs from expected maintainer")
        command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
        command(["docker", "image", "inspect", args.image], timeout=30)
        command(["git", "--version"])
        labels = json.loads(command(["gh", "label", "list", "-R", args.repo,
                                     "--json", "name", "--limit", "1000"]))
        names = {item["name"] for item in labels}
        require({"queue:worker", "state:proposed", "state:ready",
                 "state:in-progress"} <= names, "required labels absent")
        # Scope git auth to this process tree; no global Git configuration changes.
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "credential.helper"
        os.environ["GIT_CONFIG_VALUE_0"] = "!gh auth git-credential"
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
        with tempfile.TemporaryDirectory(prefix="ermik-live-merge-") as directory:
            root = Path(directory)
            for case in CASES:
                row = run_case(args.repo, base, args.image, root, case)
                report["cases"].append(row)
        report["status"] = "passed"
    except (MatrixFailure, KeyError, ValueError, OSError, WorkflowError) as exc:
        # Only fixed error classes and messages from this file enter the report.
        report["error"] = str(exc) if isinstance(exc, MatrixFailure) else type(exc).__name__
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
        args.report.chmod(0o600)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
