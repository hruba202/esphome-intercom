#!/usr/bin/env python3
"""Require executed, successful tests in a CI job's JUnit reports."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def check_report(path: Path) -> int:
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError(f"{path}: not a JUnit test report")
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError(f"{path}: no tests executed")
    for suite in root.iter():
        if suite.tag not in {"testsuites", "testsuite"}:
            continue
        for status in ("failures", "errors", "skipped"):
            count = int(suite.get(status, "0"))
            if count != 0:
                raise ValueError(f"{path}: suite reports {count} {status}")
    for status in ("failure", "error", "skipped"):
        problems = list(root.iter(status))
        if problems:
            detail = problems[0].get("message", "").strip()
            raise ValueError(f"{path}: {len(problems)} {status} results: {detail}")
    return len(cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        for path in args.reports:
            count = check_report(path)
            print(f"{path}: {count} successful test cases, no skipped/error/failure results")
    except (OSError, ET.ParseError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
