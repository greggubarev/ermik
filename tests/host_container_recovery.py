"""Host smoke: a stopped controller reclaims only its labelled Docker container."""

from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from ermik import runtime
from ermik.config import Config


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=60, check=check)


def main(image: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ermik-container-smoke-") as directory:
        root = Path(directory)
        cfg = Config("example/container-smoke", "owner", root / "state", root / "work",
                     image, ("true",), (("true",),), 1, 60, (), "main", (), 0)
        namespace = runtime._container_namespace(cfg)
        name = f"ermik-smoke-{uuid.uuid4().hex[:16]}"
        runtime._cleanup_containers(cfg, mark=True)
        try:
            run(["docker", "run", "--detach", "--network", "none", "--name", name,
                 "--label", f"ermik.namespace={namespace}", "--entrypoint", "sh",
                 image, "-c", "sleep 300"])
            runtime._CLEANED_NAMESPACES.discard(namespace)
            runtime._cleanup_containers(cfg)
            if run(["docker", "inspect", name], check=False).returncode == 0:
                raise RuntimeError("orphan container is still present")
            print("container recovery passed")
        finally:
            run(["docker", "rm", "--force", name], check=False)
            runtime._CLEANED_NAMESPACES.discard(namespace)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: host_container_recovery.py IMAGE")
    main(sys.argv[1])
