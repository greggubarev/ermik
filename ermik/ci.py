"""Small, language-neutral check for JUnit XML produced by a project's tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


MAX_REPORT_BYTES = 4 * 1024 * 1024
COUNTERS = ("tests", "failures", "errors", "skipped")


def _declared_counts(element: ET.Element, actual: dict[str, int]) -> None:
    present = [name for name in COUNTERS if name in element.attrib]
    if not present:
        return
    if len(present) != len(COUNTERS):
        raise ValueError("partial JUnit suite counters")
    for name in COUNTERS:
        value = element.get(name, "")
        if not value.isdecimal() or int(value) != actual[name]:
            raise ValueError(f"inconsistent JUnit {name} counter")


def _case_counts(case: ET.Element) -> dict[str, int]:
    outcomes = []
    for child in case:
        if child.tag in ("failure", "error", "skipped"):
            outcomes.append(child.tag)
        elif child.tag not in ("system-out", "system-err"):
            raise ValueError("unexpected JUnit testcase element")
    if len(outcomes) > 1:
        raise ValueError("multiple JUnit testcase outcomes")
    return {"tests": 1, "failures": int(outcomes == ["failure"]),
            "errors": int(outcomes == ["error"]),
            "skipped": int(outcomes == ["skipped"])}


def _suite_counts(suite: ET.Element) -> dict[str, int]:
    counts = dict.fromkeys(COUNTERS, 0)
    for child in suite:
        if child.tag == "testcase":
            part = _case_counts(child)
        elif child.tag == "testsuite":
            part = _suite_counts(child)
        elif child.tag in ("properties", "system-out", "system-err"):
            continue
        else:
            raise ValueError("unexpected JUnit suite element")
        for name in COUNTERS:
            counts[name] += part[name]
    _declared_counts(suite, counts)
    return counts


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
    if root.tag == "testsuite":
        counts = _suite_counts(root)
    else:
        counts = dict.fromkeys(COUNTERS, 0)
        suites = 0
        for child in root:
            if child.tag != "testsuite":
                raise ValueError("unexpected JUnit testsuites element")
            suites += 1
            part = _suite_counts(child)
            for name in COUNTERS:
                counts[name] += part[name]
        if not suites:
            raise ValueError("JUnit report has no suites")
        _declared_counts(root, counts)
    executed = counts["tests"] - counts["skipped"]
    if executed < min_tests:
        raise ValueError(f"only {executed} tests executed; required {min_tests}")
    failed = counts["failures"] + counts["errors"]
    if failed:
        raise ValueError(f"{failed} tests failed")
    return {"tests": counts["tests"], "executed": executed,
            "skipped": counts["skipped"], "failed": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a nonempty JUnit test report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-tests", type=int, default=1)
    args = parser.parse_args()
    try:
        result = verify_junit(args.report, min_tests=args.min_tests)
    except ValueError as exc:
        parser.exit(1, f"check failed: {exc}\n")
    print(f"tests={result['tests']} executed={result['executed']} "
          f"skipped={result['skipped']} failed={result['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
