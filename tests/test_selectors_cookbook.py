"""Verify every runnable selectors cookbook example executes and produces a shape."""

import pytest

from build123d_mcp.selectors_cookbook import RUNNABLE_EXAMPLES, build_selectors_cookbook_text
from build123d_mcp.session import Session


@pytest.fixture
def fresh_session():
    return Session()


@pytest.mark.parametrize(
    "label,code",
    RUNNABLE_EXAMPLES,
    ids=[label for label, _ in RUNNABLE_EXAMPLES],
)
def test_selectors_cookbook_example_runs(fresh_session, label, code):
    result = fresh_session.execute(code)
    assert not result.startswith("Error:"), f"Example '{label}' failed:\n{result}"
    assert fresh_session.current_shape is not None, f"Example '{label}' produced no shape"


def test_selectors_cookbook_resource_uses_generated_text():
    """Confirm the MCP resource returns exactly what build_selectors_cookbook_text() produces."""
    from build123d_mcp.server import build123d_selectors_cookbook

    assert build123d_selectors_cookbook() == build_selectors_cookbook_text()
