"""Disposable host gate for Docker mounts, a linked Git worktree, and local CI."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

from ermik.config import Config
from ermik.runtime import _docker, git, local_checks


def command(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(image: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ermik-host-gate-") as directory:
        root = Path(directory)
        source, work = root / "workspace" / "source", root / "work"
        source.parent.mkdir()
        command("git", "init", "-b", "main", str(source))
        git(source, "config", "user.name", "Repository owner")
        git(source, "config", "user.email", "owner@example.invalid")
        (source / "README.md").write_text("fixture\n", encoding="utf-8")
        git(source, "add", "README.md")
        git(source, "commit", "-m", "Initial fixture")
        git(source, "remote", "add", "origin", str(source))
        git(source, "fetch", "origin", "main:refs/remotes/origin/main")
        git(source, "worktree", "add", "-b", "work/issue-1-demo", str(work))
        task_dir = root / "task"
        task_dir.mkdir()
        issue = task_dir / "issue.md"
        issue.write_text("## Allowed paths\nsrc/example.py\n", encoding="utf-8")
        cfg = Config("example/repo", "owner", root / "state", root / "workspace",
                     image, ("/bin/sh", "/worker.sh"),
                     (("python3", "-c", "import runpy; "
                       "greeting=runpy.run_path('src/example.py')['greeting']; "
                       "assert greeting() == 'Hello from the example project'"),),
                     1, 60, (), "main")
        try:
            try:
                local_checks(cfg, work)
            except RuntimeError:
                pass
            else:
                raise RuntimeError("missing file unexpectedly passed local checks")
            _docker(cfg, work, cfg.worker_command, issue_file=issue,
                    network=False, timeout=60)
            if not (work / "src" / "example.py").is_file():
                raise RuntimeError("worker did not write allowed file")
            git(work, "add", "-A")
            git(work, "commit", "-m", "Deliver fixture")
            proof = local_checks(cfg, work)
            if proof["result"] != "passed":
                raise RuntimeError("local checks did not pass")
            issue.write_text(issue.read_text(encoding="utf-8") +
                             "\n## Review correction\nAdd a short docstring.\n", encoding="utf-8")
            _docker(cfg, work, cfg.worker_command, issue_file=issue,
                    network=False, timeout=60)
            if '"""Return the example greeting."""' not in (
                    work / "src" / "example.py").read_text(encoding="utf-8"):
                raise RuntimeError("review correction did not change the same file")
            git(work, "add", "-A")
            git(work, "commit", "-m", "Apply review correction")
            if local_checks(cfg, work)["result"] != "passed":
                raise RuntimeError("corrected task did not pass local checks")
        finally:
            git(source, "worktree", "remove", str(work))
    print("host smoke passed: negative check, task, correction, local CI")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ermik-demo:latest")
