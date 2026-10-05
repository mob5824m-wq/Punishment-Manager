#!/usr/bin/env python3
"""Turn a pytest JUnit report into GitHub annotations.

The workflow logs are behind an authenticated blob store: the run page shows
"View raw logs" but anything that fetches them programmatically (including the
`gh` CLI in a sandbox with restricted egress) can fail with a connection error,
leaving a red leg with no visible reason. Annotations, on the other hand, are
plain REST data (`/check-runs/<id>/annotations`), so a failing test can be
diagnosed with one API call - the same trick the glibc floor check uses.

Usage::

    python -m pytest tests -q --junitxml=test-results.xml
    python .github/scripts/annotate-test-failures.py test-results.xml

Each failed or errored test becomes one `::error file=…,line=…::message`
annotation, and the script prints the same summary so a normal log read shows
it too. Missing reports are not an error: a failure before pytest ran (bad
interpreter, missing dependency) has no report, and the step that failed is
already the message.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# GitHub drops annotations past this many per step, so keep the important ones
# (the first failures) and say how many were not shown.
MAX_ANNOTATIONS = 8


def escape(value: str) -> str:
    """Escape a workflow-command message or property."""
    return (
        str(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def first_lines(text: str, limit: int = 6) -> str:
    """The start of a traceback: the assertion and where it happened."""
    lines = [line.rstrip() for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(lines[:limit]) or "no details in the report"


def failures(report: Path) -> list[dict]:
    """One entry per failed or errored test case in a JUnit report."""
    root = ET.parse(report).getroot()
    found: list[dict] = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        found.append(
            {
                "classname": case.get("classname", ""),
                "name": case.get("name", "?"),
                # pytest writes these on the testcase; older reports may not.
                "file": case.get("file", ""),
                "line": case.get("line", ""),
                "message": first_lines(problem.get("message") or (problem.text or "")),
            }
        )
    return found


def main(argv: list[str]) -> int:
    reports = [Path(arg) for arg in argv[1:]] or [Path("test-results.xml")]
    existing = [path for path in reports if path.is_file()]
    if not existing:
        print(f"annotate-test-failures: no JUnit report at {reports[0]}; nothing to annotate.")
        return 0

    results: list[dict] = []
    for path in existing:
        try:
            results.extend(failures(path))
        except ET.ParseError as exc:
            print(f"annotate-test-failures: {path} is not valid XML ({exc}).")
            return 0
    if not results:
        print("annotate-test-failures: the report contains no failures.")
        return 0

    def where(item: dict) -> str:
        classname = item["classname"].rsplit(".", 1)[-1]
        return f"{classname}.{item['name']}" if classname and classname != item["name"] else item["name"]

    print(f"annotate-test-failures: {len(results)} failing test(s).")
    for item in results[:MAX_ANNOTATIONS]:
        location = ""
        if item["file"]:
            location = f"file={escape(item['file'])},"
            if item["line"].isdigit():
                location += f"line={item['line']},"
        print(f"::error {location}title={escape(where(item))}::{escape(item['message'])}")
    if len(results) > MAX_ANNOTATIONS:
        rest = ", ".join(where(item) for item in results[MAX_ANNOTATIONS:])
        print(f"::warning::{len(results) - MAX_ANNOTATIONS} more failure(s) not shown: {escape(rest)}")
    # The step that ran pytest has already failed the job; this step only
    # reports, so it must not turn a test failure into a green build.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
