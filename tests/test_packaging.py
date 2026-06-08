"""Packaging-level regression tests.

These tests guard against changes that would break installation or runtime
behaviour for end users without surfacing in the in-process tests.
"""

from importlib.metadata import requires


def test_build123d_drafting_helpers_is_runtime_dependency():
    """build123d-drafting-helpers must ship as a runtime dependency (issue #106).

    The `inspect_drawing` tool's docstring and the `build123d://drafting` cookbook
    both instruct users to `from build123d_drafting import Dimension, Draft`.
    That promise only holds if the helper package is installed alongside the
    server in a runtime install (`uv tool run build123d-mcp`, `pip install
    build123d-mcp`). Moving it back to a dev-only dep would silently break the
    advertised workflow.
    """
    deps = requires("build123d-mcp") or []
    # Each entry is a PEP 508 requirement string. Match by leading name segment
    # so version markers and extras don't break this.
    runtime_deps = {
        req.split()[0].split(";")[0].split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        for req in deps
        if "extra ==" not in req
    }
    assert "build123d-drafting-helpers" in runtime_deps, (
        f"build123d-drafting-helpers must be in runtime dependencies "
        f"(issue #106). Got: {sorted(runtime_deps)}"
    )


def test_build123d_drafting_importable_in_sandbox():
    """The sandbox must accept `from build123d_drafting import ...` without
    the user having to install anything extra."""
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=30)
    try:
        out = ws.execute(
            "from build123d_drafting import Dimension, Leader, view_axes, lint_drawing\n"
            "print('imports OK')"
        )
        assert "imports OK" in out, out
        assert "Error" not in out, out
    finally:
        ws._kill_worker()
