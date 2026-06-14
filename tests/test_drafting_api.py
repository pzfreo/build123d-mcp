"""Tests for the build123d://drafting-api resource (#260)."""

from build123d_mcp.tools.drafting_api import drafting_api


def test_reference_covers_the_public_api():
    """Every __all__ symbol of the installed library appears with a signature."""
    import build123d_drafting as bd

    ref = drafting_api(None)
    for name in bd.__all__:
        assert f"{name}(" in ref, f"public symbol '{name}' missing from the reference"


def test_reference_carries_signatures_not_just_names():
    ref = drafting_api(None)
    # Keyword names are the whole point of the resource (#260).
    assert "label" in ref
    assert "drawing_scale" in ref  # lint_drawing keyword — proves signatures, not just names


def test_resource_uses_worker_routing():
    """The MCP resource must call through the session proxy, not import the
    drafting library (and thereby OCC) in the parent process."""
    import inspect

    from build123d_mcp import server

    src = inspect.getsource(server.build123d_drafting_api)
    assert "_session.drafting_api()" in src
