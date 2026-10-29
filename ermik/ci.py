"""Small, language-neutral check for JUnit XML produced by a project's tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


MAX_REPORT_BYTES = 4 * 1024 * 1024


def verify_junit(path: Path, *, min_tests: int = 1) -> dict:
    if type(min_tests) is not int or not 1 <= min_tests <= 100000:
        raise ValueError("min_tests must be 1..100000")
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("JUnit report missing, linked, or too large")
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError as exc:
        raise ValueError("invalid JUnit XML") from exc
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError("unexpected JUnit root")
    cases = list(root.iter("testcase"))
    if len(cases) < min_tests:
        raise ValueError(f"only {len(cases)} tests recorded; required {min_tests}")
    failed = sum(any(child.tag in ("failure", "error") for child in case)
                 for case in cases)
    if failed:
        raise ValueError(f"{failed} tests failed")
    return {"tests": len(cases), "failed": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a nonempty JUnit test report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-tests", type=int, default=1)
    args = parser.parse_args()
    try:
        result = verify_junit(args.report, min_tests=args.min_tests)
    except ValueError as exc:
        parser.exit(1, f"check failed: {exc}\n")
    print(f"tests={result['tests']} failed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
