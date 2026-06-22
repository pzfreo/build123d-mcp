"""hasattr is allowed in execute(); getattr/vars stay blocked; --no-sandbox lifts
all layers; blocked-call errors get a call hint, not the import hint (#265)."""

import pytest

import build123d_mcp.security as _sec
from build123d_mcp.security import check_ast, make_restricted_builtins


@pytest.fixture
def no_sandbox(monkeypatch):
    """Enable DISABLE_SANDBOX for the test, auto-restored afterwards."""
    monkeypatch.setattr(_sec, "DISABLE_SANDBOX", True)


# --- #265: hasattr allowed by default, getattr/vars still blocked ---------------


def test_hasattr_passes_ast():
    check_ast("hasattr(part, 'solids')")  # must not raise


def test_hasattr_in_restricted_builtins():
    assert "hasattr" in make_restricted_builtins()


def test_getattr_still_blocked():
    with pytest.raises(ValueError, match="not allowed"):
        check_ast("getattr(x, '__class__')")


def test_vars_still_blocked():
    with pytest.raises(ValueError, match="not allowed"):
        check_ast("vars(x)")


def test_getattr_absent_from_restricted_builtins():
    assert "getattr" not in make_restricted_builtins()


# --- --no-sandbox lifts every layer --------------------------------------------


def test_no_sandbox_allows_arbitrary_import(no_sandbox):
    check_ast("import os")  # must not raise


def test_no_sandbox_allows_blocked_calls(no_sandbox):
    check_ast("open('/etc/passwd'); getattr(x, '__class__')")  # must not raise


def test_no_sandbox_allows_dunder_traversal(no_sandbox):
    check_ast("().__class__.__base__.__subclasses__()")  # must not raise


def test_no_sandbox_unrestricted_builtins(no_sandbox):
    b = make_restricted_builtins()
    assert {"open", "eval", "exec", "compile", "getattr"} <= set(b)


def test_sandbox_on_keeps_builtins_restricted():
    """Sanity: with the flag off (default), dangerous builtins are still removed."""
    b = make_restricted_builtins()
    assert "open" not in b and "eval" not in b


# --- misleading-hint fix -------------------------------------------------------


def test_blocked_call_hint_is_not_import_hint():
    from build123d_mcp.tools.repair_hints import repair_hints

    hint = repair_hints("Error: SecurityError: Call to 'getattr' is not allowed.")
    assert "Import blocked" not in hint
    assert "sandbox" in hint.lower()


def test_import_error_still_gets_import_hint():
    from build123d_mcp.tools.repair_hints import repair_hints

    hint = repair_hints("Error: SecurityError: Import of 'os' is not allowed.")
    assert "Import blocked" in hint


def test_call_block_classified_as_call_blocked():
    from build123d_mcp.tools.execute import _classify_from_error_string

    c = _classify_from_error_string("Error: SecurityError: Call to 'getattr' is not allowed.")
    assert c["failure_class"] == "call_blocked"


def test_import_block_classified_as_import_blocked():
    from build123d_mcp.tools.execute import _classify_from_error_string

    c = _classify_from_error_string("Error: SecurityError: Import of 'os' is not allowed.")
    assert c["failure_class"] == "import_blocked"


# --- end-to-end through a real spawned worker (locks in the positional plumbing) ---
# WorkerSession runs in a subprocess, so its DISABLE_SANDBOX is set there and does
# not leak into this test process. (An InProcessSession would poison global state.)


def test_worker_session_no_sandbox_runs_blocked_code():
    from build123d_mcp.worker import WorkerSession

    s = WorkerSession(exec_timeout=30, no_sandbox=True)
    try:
        result = s.execute("import os\nprint('pid', os.getpid())")
        assert "not allowed" not in result and "SecurityError" not in result, result
        assert "pid" in result, result
    finally:
        s._kill_worker()


def test_worker_session_default_blocks_import():
    """Control: the same code is rejected without no_sandbox — proves the flag is load-bearing."""
    from build123d_mcp.worker import WorkerSession

    s = WorkerSession(exec_timeout=30)
    try:
        result = s.execute("import os")
        assert "not allowed" in result, result
    finally:
        s._kill_worker()
