"""build123d's per-object INFO logging must not propagate to the server's stderr.

build123d logs on every object construction. With FastMCP's root stderr handler
installed, those records reach stderr; on a host whose stderr is hostile (the
Copilot/Codex CLI on Windows, where a write raises OSError) the logging machinery
then prints a "--- Logging error ---" traceback into every tool result.
_build_session cuts propagation so that cannot happen.
"""

import io
import logging

from build123d_mcp.worker import _build_session


def test_build_session_disables_build123d_log_propagation():
    logging.getLogger("build123d").propagate = True  # undo any prior state
    _build_session("", 30, False, ())
    assert logging.getLogger("build123d").propagate is False


def test_build123d_logs_do_not_reach_a_root_stderr_handler():
    """With a root stderr handler installed (as FastMCP does), a build123d INFO
    record must not surface on stderr once _build_session has run."""
    root = logging.getLogger()
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    root.addHandler(handler)
    prior_level = root.level
    root.setLevel(logging.INFO)
    try:
        logging.getLogger("build123d").propagate = True  # simulate the unpatched state
        logging.getLogger("build123d").info("context requested by Box")
        assert "context requested by Box" in captured.getvalue()  # leaks without the fix

        captured.truncate(0)
        captured.seek(0)
        _build_session("", 30, False, ())  # applies propagate = False
        logging.getLogger("build123d").info("context requested by Box")
        assert captured.getvalue() == ""  # no longer reaches the stderr handler
    finally:
        root.removeHandler(handler)
        root.setLevel(prior_level)
