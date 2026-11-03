"""Opt-in live recovery exercise against a disposable GitHub fork.

Run with a credential that can create refs in the named fork::

    py -3 tests/live_probe_matrix.py --upstream OWNER/REPO --fork OWNER/REPO

The script creates only ``ermik-access-probe-*`` refs. It saves a local intent
before each write, exercises hard process exits and lost responses, and checks
that recovery removed each exact ref. A failed run retains its local intents
for inspection. Use a disposable fork; this sends real GitHub writes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ermik.github import GitHub  # noqa: E402
import ermik.github as github_module  # noqa: E402


HARD_EXIT_CASES = ("before_post", "after_post", "after_delete")
LOST_RESPONSE_CASES = ("POST", "DELETE")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
REF_PATTERN = re.compile(r"refs/heads/ermik-access-probe-[0-9a-f]{32}\Z")
SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")


class MatrixFailure(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise MatrixFailure(reason)


def credential() -> str:
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not value:
        try:
            result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                    text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MatrixFailure("GitHub credential unavailable") from exc
        if result.returncode:
            raise MatrixFailure("GitHub credential unavailable")
        value = result.stdout.strip()
    require(bool(value), "GitHub credential unavailable")
    return value


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ermik-live-probe-matrix"}


def read_json(url: str, token: str) -> dict:
    with urlopen(Request(url, headers=headers(token)), timeout=20) as response:
        row = json.load(response)
    require(isinstance(row, dict), "GitHub response is invalid")
    return row


def verify_fixture(upstream: str, fork: str, token: str) -> str:
    require(upstream.casefold() != fork.casefold(), "fork must differ from upstream")
    upstream_row = read_json(f"https://api.github.com/repos/{upstream}", token)
    fork_row = read_json(f"https://api.github.com/repos/{fork}", token)
    require(str(upstream_row.get("full_name", "")).casefold() == upstream.casefold(),
            "upstream identity changed")
    require(str(fork_row.get("full_name", "")).casefold() == fork.casefold()
            and fork_row.get("fork") is True
            and str((fork_row.get("parent") or {}).get("full_name", "")).casefold()
            == upstream.casefold(), "fork parent identity changed")
    branch = fork_row.get("default_branch")
    require(isinstance(branch, str) and re.fullmatch(r"[A-Za-z0-9_.\-/]+", branch)
            and ".." not in branch and not branch.startswith("/"),
            "fork default branch is invalid")
    row = read_json(f"https://api.github.com/repos/{fork}/git/ref/heads/{branch}", token)
    sha = (row.get("object") or {}).get("sha")
    require(row.get("ref") == f"refs/heads/{branch}"
            and isinstance(sha, str) and bool(SHA_PATTERN.fullmatch(sha)),
            "fork default ref is invalid")
    return sha


def assert_write(request: Request, fork: str, owned_ref: str) -> None:
    method = request.get_method()
    if method == "POST":
        require(request.full_url == f"https://api.github.com/repos/{fork}/git/refs",
                "unexpected POST target")
        body = json.loads(request.data)
        require(body.get("ref") == owned_ref, "unexpected POST ref")
    elif method == "DELETE":
        branch = owned_ref.removeprefix("refs/heads/")
        require(request.full_url ==
                f"https://api.github.com/repos/{fork}/git/refs/heads/{branch}",
                "unexpected DELETE target")
    else:
        require(method == "GET", "unexpected GitHub write method")


def child(mode: str, upstream: str, fork: str, directory: Path, sha: str) -> None:
    token = credential()
    require(verify_fixture(upstream, fork, token).casefold() == sha.casefold(),
            "fork default SHA changed before child write")
    ref = "refs/heads/ermik-access-probe-" + uuid.uuid4().hex
    require(bool(REF_PATTERN.fullmatch(ref)), "generated ref invalid")
    gh = GitHub(upstream, fork, probe_state_dir=directory)
    gh._save_probe_intent(fork, ref, sha, token)
    if mode == "before_post":
        os._exit(0)
    original = github_module.urlopen

    def guarded(request: Request, *args, **kwargs):
        assert_write(request, fork, ref)
        return original(request, *args, **kwargs)

    github_module.urlopen = guarded
    base = f"https://api.github.com/repos/{fork}/git/refs"
    post = Request(base, data=json.dumps({"ref": ref, "sha": sha}).encode(),
                   headers=headers(token), method="POST")
    with github_module.urlopen(post, timeout=20) as response:
        require(response.status == 201, "probe POST was not created")
    if mode == "after_post":
        os._exit(0)
    delete = Request(base + "/heads/" + ref.rsplit("/", 1)[-1],
                     headers=headers(token), method="DELETE")
    with github_module.urlopen(delete, timeout=20) as response:
        require(response.status == 204, "probe DELETE was not confirmed")
    os._exit(0)


def recover_case(mode: str, upstream: str, fork: str, root: Path,
                 sha: str, token: str) -> None:
    directory = root / mode
    directory.mkdir()
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                             "--child", mode, "--upstream", upstream,
                             "--fork", fork, "--state", str(directory),
                             "--sha", sha], capture_output=True, timeout=90,
                            check=False)
    require(result.returncode == 0, f"{mode}: child failed")
    intents = list((directory / "probe-intents").glob("*.json"))
    require(len(intents) == 1, f"{mode}: expected one durable intent")
    row = json.loads(intents[0].read_text(encoding="utf-8"))
    ref = row.get("ref")
    require(row.get("repo") == fork and row.get("sha") == sha
            and isinstance(ref, str) and bool(REF_PATTERN.fullmatch(ref)),
            f"{mode}: intent identity invalid")
    original = github_module.urlopen

    def guarded(request: Request, *args, **kwargs):
        assert_write(request, fork, ref)
        return original(request, *args, **kwargs)

    github_module.urlopen = guarded
    try:
        GitHub(upstream, fork, probe_state_dir=directory).reconcile_probe_intents()
    finally:
        github_module.urlopen = original
    require(not intents[0].exists(), f"{mode}: intent remains")
    require(GitHub(upstream, fork)._probe_ref_sha(fork, ref, headers(token), sha) is None,
            f"{mode}: ref remains")


def lost_response_case(method: str, upstream: str, fork: str, root: Path,
                       sha: str, token: str) -> None:
    directory = root / ("lost_response_" + method.lower())
    directory.mkdir()
    gh = GitHub(upstream, fork, probe_state_dir=directory)
    original = github_module.urlopen
    injected = False
    owned_ref = None

    def wrapped(request: Request, *args, **kwargs):
        nonlocal injected, owned_ref
        if request.get_method() == "POST":
            owned_ref = json.loads(request.data)["ref"]
            require(bool(REF_PATTERN.fullmatch(owned_ref)), "generated ref invalid")
        require(owned_ref is not None, "write without an owned ref")
        assert_write(request, fork, owned_ref)
        if request.get_method() == method and not injected:
            with original(request, *args, **kwargs):
                pass
            injected = True
            raise URLError("simulated lost response")
        return original(request, *args, **kwargs)

    github_module.urlopen = wrapped
    try:
        require(gh._probe_branch_write(fork, sha, token),
                f"lost {method}: probe did not succeed")
    finally:
        github_module.urlopen = original
    require(injected and owned_ref is not None, f"lost {method}: injection missed")
    require(not list((directory / "probe-intents").glob("*.json")),
            f"lost {method}: intent remains")
    require(gh._probe_ref_sha(fork, owned_ref, headers(token), sha) is None,
            f"lost {method}: ref remains")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="disposable upstream OWNER/REPO")
    parser.add_argument("--fork", required=True, help="disposable fork OWNER/REPO")
    parser.add_argument("--child", choices=HARD_EXIT_CASES, help=argparse.SUPPRESS)
    parser.add_argument("--state", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sha", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(bool(REPO_PATTERN.fullmatch(args.upstream))
            and bool(REPO_PATTERN.fullmatch(args.fork)), "repository name invalid")
    if args.child:
        require(args.state is not None and isinstance(args.sha, str)
                and bool(SHA_PATTERN.fullmatch(args.sha)), "child arguments invalid")
        child(args.child, args.upstream, args.fork, args.state, args.sha)
        return 0
    token = credential()
    sha = verify_fixture(args.upstream, args.fork, token)
    root = Path(tempfile.mkdtemp(prefix="ermik-probe-live-"))
    try:
        for mode in HARD_EXIT_CASES:
            recover_case(mode, args.upstream, args.fork, root, sha, token)
            print(f"{mode}: recovered", flush=True)
        for method in LOST_RESPONSE_CASES:
            lost_response_case(method, args.upstream, args.fork, root, sha, token)
            print(f"lost_response_{method.lower()}: recovered", flush=True)
    except BaseException:
        print(f"Probe recovery incomplete; inspect local state at {root}", file=sys.stderr)
        raise
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    resolved = root.resolve(strict=True)
    require(not root.is_symlink() and resolved.parent == temp_root
            and root.name.startswith("ermik-probe-live-")
            and root.is_dir(), "temporary state location changed; inspect manually")
    shutil.rmtree(root)
    print("All five probe cases passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MatrixFailure as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
