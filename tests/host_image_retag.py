"""Disposable Docker gate for a tag replaced between offline checks."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import uuid

from ermik import runtime
from ermik.config import Config


def command(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(args, input=input_text, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def main(base_image: str) -> None:
    suffix = uuid.uuid4().hex[:12]
    moved_tag = f"ermik-retag-{suffix}:current"
    replacement_tag = f"ermik-retag-{suffix}:replacement"
    first_id = runtime._resolve_image_id(base_image)
    try:
        command("docker", "build", "--quiet", "--pull=false", "--network=none",
                "-t", replacement_tag, "-", input_text=(
                    f"FROM {base_image}\nRUN touch /image-version-b\n"))
        second_id = runtime._resolve_image_id(replacement_tag)
        if first_id == second_id:
            raise RuntimeError("replacement image did not change identity")
        command("docker", "tag", base_image, moved_tag)
        with tempfile.TemporaryDirectory(prefix="ermik-retag-gate-") as directory:
            root = Path(directory)
            source = root / "workspace" / "source"
            checkout = root / "checkout"
            source.parent.mkdir()
            command("git", "init", "-b", "main", str(source))
            command("git", "-C", str(source), "config", "user.name", "Repository owner")
            command("git", "-C", str(source), "config", "user.email",
                    "owner@example.invalid")
            (source / "README.md").write_text("fixture\n", encoding="utf-8")
            command("git", "-C", str(source), "add", "README.md")
            command("git", "-C", str(source), "commit", "-m", "Initial fixture")
            command("git", "-C", str(source), "remote", "add", "origin", str(source))
            command("git", "-C", str(source), "fetch", "origin",
                    "main:refs/remotes/origin/main")
            command("git", "-C", str(source), "worktree", "add", "-b", "check",
                    str(checkout))
            check = ("sh", "-c", "test ! -e /image-version-b")
            cfg = Config("example/repo", "owner", root / "state",
                         root / "workspace", moved_tag, check, (check, check),
                         1, 60, (), "main")
            try:
                runtime._docker(cfg, checkout, check, issue_file=None,
                                network=False, timeout=60, image_id=first_id)
                original = runtime._docker
                count = 0

                def retag_after_first_check(*args, **kwargs):
                    nonlocal count
                    result = original(*args, **kwargs)
                    count += 1
                    if count == 1:
                        command("docker", "tag", replacement_tag, moved_tag)
                    return result

                with patch.object(runtime, "_docker", side_effect=retag_after_first_check):
                    proof = runtime.local_checks(cfg, checkout, image_id=first_id)
                if (count != 2 or proof["image_id"] != first_id
                        or runtime._resolve_image_id(moved_tag) != second_id):
                    raise RuntimeError("checks did not retain the original image")
            finally:
                command("git", "-C", str(source), "worktree", "remove", str(checkout))
    finally:
        subprocess.run(["docker", "image", "rm", moved_tag, replacement_tag],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print("host image retag passed: execution and both checks used the original image")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ermik-demo:latest")
