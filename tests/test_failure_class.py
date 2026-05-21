"""Tests for failure_class classification in execute() error responses."""
import json
import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def _get_failure_class(result: str) -> str | None:
    """Extract failure_class from the JSON appended to an error response."""
    # The JSON is appended after a blank line at the end of the error message.
    for line in reversed(result.splitlines()):
        line = line.strip()
        if line.startswith("{") and "failure_class" in line:
            return json.loads(line)["failure_class"]
    return None


def test_boolean_fail_classification(session):
    # Can't get geom adaptor message is triggered by ExtensionLine on short paths,
    # but we can simulate it directly via a string the classification checks for.
    # Easiest: force a ValueError whose message contains the sentinel.
    code = """
from build123d import *
raise ValueError("Can't get geom adaptor of empty wire")
"""
    result = execute_code(session, code)
    assert result.startswith("Error:")
    fc = _get_failure_class(result)
    assert fc == "boolean_fail", f"Expected boolean_fail, got {fc!r}. Full result:\n{result}"


def test_syntax_error_classification(session):
    code = "def foo(:\n    pass"
    result = execute_code(session, code)
    assert result.startswith("Error:")
    fc = _get_failure_class(result)
    assert fc == "syntax_error", f"Expected syntax_error, got {fc!r}. Full result:\n{result}"


def test_import_blocked_classification(session):
    code = "import os"
    result = execute_code(session, code)
    assert result.startswith("Error:")
    fc = _get_failure_class(result)
    assert fc == "import_blocked", f"Expected import_blocked, got {fc!r}. Full result:\n{result}"


def test_suggested_fix_present(session):
    code = "import os"
    result = execute_code(session, code)
    # Find the JSON block
    for line in reversed(result.splitlines()):
        line = line.strip()
        if line.startswith("{") and "failure_class" in line:
            data = json.loads(line)
            assert "suggested_fix" in data
            assert len(data["suggested_fix"]) > 10
            return
    pytest.fail("No failure_class JSON found in result")


def test_successful_execute_has_no_failure_class(session):
    result = execute_code(session, "x = 1 + 1")
    assert "failure_class" not in result
