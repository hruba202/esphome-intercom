"""A green command must not conceal missing CI test execution."""

from pathlib import Path
import subprocess
import sys

import pytest


CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_test_results.py"


@pytest.mark.parametrize("report,accepted", [
    ('<testsuite><testcase name="executed"/></testsuite>', True),
    ('<testsuites><testsuite><testcase/></testsuite></testsuites>', True),
    ('<testsuite tests="5"/>', False),
    ('<testsuite skipped="1"><testcase/></testsuite>', False),
    ('<testsuites errors="1"><testsuite><testcase/></testsuite></testsuites>', False),
    ('<testsuite><testcase><skipped message="missing peer"/></testcase></testsuite>', False),
    ('<testsuite><testcase><failure message="wrong result"/></testcase></testsuite>', False),
    ('<testsuite><testcase><error message="setup failed"/></testcase></testsuite>', False),
    ('<testsuite><testcase/></testsuite><invalid>', False),
    ('<unrelated><testcase/></unrelated>', False),
])
def test_ci_requires_real_successful_test_results(tmp_path, report, accepted):
    path = tmp_path / 'results.xml'
    path.write_text(report)
    result = subprocess.run([sys.executable, str(CHECKER), str(path)],
                            capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_ci_rejects_missing_report(tmp_path):
    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path / 'missing.xml')],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert 'missing.xml' in result.stderr
