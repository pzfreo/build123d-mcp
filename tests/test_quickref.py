"""Verify every runnable quickref example executes without error and produces a shape."""

import pytest

from build123d_mcp.quickref import RUNNABLE_EXAMPLES, build_quickref_text
from build123d_mcp.session import Session


@pytest.fixture
def fresh_session():
    return Session()


@pytest.mark.parametrize(
    "label,code",
    RUNNABLE_EXAMPLES,
    ids=[label for label, _ in RUNNABLE_EXAMPLES],
)
def test_quickref_example_runs(fresh_session, label, code):
    result = fresh_session.execute(code)
    assert not result.startswith("Error:"), f"Example '{label}' failed:\n{result}"
    assert fresh_session.current_shape is not None, f"Example '{label}' produced no shape"


def test_quickref_resource_uses_generated_text():
    """Confirm the MCP resource returns exactly what build_quickref_text() produces."""
    from build123d_mcp.server import build123d_quickref

    assert build123d_quickref() == build_quickref_text()
