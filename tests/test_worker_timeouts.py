"""Timeout-tier routing and session-loss messaging in WorkerSession (issues #214-#216).

A timeout on any worker op SIGKILLs the worker, so geometry-heavy ops must get a
budget that complex parts can actually meet (#214); every restart path replays the
parent-side execute() history to rebuild session state and reports what was
restored/reset rather than a bare "state lost" (#215, #359); and the macOS VTK
subprocess guard must fire before the parent's render poll so a VTK hang surfaces
as a clean error instead of a dead session (#216).

These tests stub the pipe/process layer — no worker subprocess is spawned.
"""

import inspect
import threading

import pytest

from build123d_mcp.security import ExecutionTimeout
from build123d_mcp.worker import (
    _EXPORT_TIMEOUT,
    _GEOMETRY_TIMEOUT,
    _RENDER_TIMEOUT,
    _SHORT_TIMEOUT,
    WorkerSession,
)


def _proxy_session(record, exec_timeout=120):
    """A WorkerSession whose _call only records (op, timeout) — no worker."""
    ws = WorkerSession.__new__(WorkerSession)

    def _call(op, args, timeout):
        record.append((op, timeout))
        return ""

    ws._call = _call
    ws._exec_timeout = exec_timeout
    return ws


def test_geometry_heavy_ops_use_geometry_timeout():
    record = []
    ws = _proxy_session(record)

    ws.measure("a")
    ws.clearance("a", "b")
    ws.cross_sections("a")
    ws.align_check("a", "b")
    ws.analyze_printability("a")
    ws.save_snapshot("s")
    ws.diff_snapshot("s")
    ws.resolve("a", ".faces()")
    ws.suggest_view_layout("a")

    assert all(t == _GEOMETRY_TIMEOUT for _op, t in record), record
    assert _GEOMETRY_TIMEOUT > _SHORT_TIMEOUT


def test_import_cad_file_honours_exec_timeout():
    # Heavy STEP imports (threads, gears) can outlast _EXPORT_TIMEOUT and a
    # timeout destroys the session, so the user's exec-timeout knob must apply
    # when it grants a larger budget (#229).
    record = []
    ws = _proxy_session(record, exec_timeout=300)
    ws.import_cad_file("part.step")
    assert record == [("import_cad_file", 300)]


def test_import_cad_file_keeps_export_floor():
    # A short exec timeout must not shrink the import budget below the default.
    record = []
    ws = _proxy_session(record, exec_timeout=10)
    ws.import_cad_file("part.step")
    assert record == [("import_cad_file", _EXPORT_TIMEOUT)]


def test_load_part_honours_exec_timeout():
    # Library part scripts can build heavy geometry just like a STEP import.
    record = []
    ws = _proxy_session(record, exec_timeout=300)
    ws.load_part("worm_gear")
    assert record == [("load_part", 300)]


def test_load_part_keeps_geometry_floor():
    record = []
    ws = _proxy_session(record, exec_timeout=10)
    ws.load_part("worm_gear")
    assert record == [("load_part", _GEOMETRY_TIMEOUT)]


def test_shape_compare_honours_exec_timeout():
    # shape_compare bounds its own tessellation/boolean subprocess by the op budget
    # (max(60, exec_timeout) - margin), so the worker op MUST give it that budget or
    # the parent would kill the worker while the child still runs.
    record = []
    ws = _proxy_session(record, exec_timeout=300)
    ws.shape_compare("a", "b")
    assert record == [("shape_compare", 300)]


def test_shape_compare_keeps_export_floor():
    record = []
    ws = _proxy_session(record, exec_timeout=10)
    ws.shape_compare("a", "b")
    assert record == [("shape_compare", _EXPORT_TIMEOUT)]


def test_bookkeeping_ops_keep_short_timeout():
    record = []
    ws = _proxy_session(record)

    ws.session_state()
    ws.objects_types()
    ws.last_error()
    ws.script()
    ws.restore_snapshot("s")
    ws.search_library("q")

    assert all(t == _SHORT_TIMEOUT for _op, t in record), record


class _StubConn:
    """Pipe stand-in: poll() result and recv() behaviour are scripted."""

    def __init__(self, poll_result=True, recv_exc=None):
        self._poll_result = poll_result
        self._recv_exc = recv_exc

    def send(self, msg):
        pass

    def poll(self, timeout=None):
        return self._poll_result

    def recv(self):
        if self._recv_exc is not None:
            raise self._recv_exc
        return {"ok": True, "result": ""}


class _StubProc:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


def _stubbed_session(conn, alive=True, history=None):
    ws = WorkerSession.__new__(WorkerSession)
    ws._conn = conn
    ws._proc = _StubProc(alive=alive)
    ws._exec_timeout = 120
    ws._lock = threading.Lock()
    ws._execute_history = list(history or [])
    ws._kill_worker = lambda: None
    ws._start_worker = lambda: None
    return ws


def test_generic_timeout_message_reports_recovery():
    # An empty history restarts to an empty session and says so (no more "lost").
    ws = _stubbed_session(_StubConn(poll_result=False))
    with pytest.raises(RuntimeError, match="restarted"):
        ws._call("measure", {}, 1)


def test_execute_timeout_message_reports_dropped_step():
    ws = _stubbed_session(_StubConn(poll_result=False))
    with pytest.raises(ExecutionTimeout, match="the step was dropped"):
        ws._call("execute", {"code": "pass"}, 1)


def test_dead_worker_message_reports_restart():
    ws = _stubbed_session(_StubConn(), alive=False)
    with pytest.raises(RuntimeError, match="restarted"):
        ws._call("measure", {}, 1)


def test_execute_timeout_with_unreplayable_history_reports_reset():
    # A non-empty history whose replay can't complete (the stub pipe never yields a
    # reply) must reset to a clean empty session, not a half-rebuilt one.
    ws = _stubbed_session(_StubConn(poll_result=False), history=["x = 1"])
    with pytest.raises(ExecutionTimeout, match="could not be rebuilt"):
        ws._call("execute", {"code": "pass"}, 1)
    assert ws._execute_history == []  # cleared on failed replay


def test_mid_call_crash_message_reports_restart():
    ws = _stubbed_session(_StubConn(recv_exc=EOFError()))
    with pytest.raises(RuntimeError, match="crashed during"):
        ws._call("measure", {}, 1)


def test_sigalrm_timeout_result_is_not_logged_but_normal_result_is():
    # A worker-side SIGALRM timeout comes back as ok=True with an "ExecutionTimeout"
    # result string; it carries no state and would re-hang the replay budget, so it
    # must NOT be logged. A normal execute IS logged (under the lock, in _do_call).
    ws = _stubbed_session(_StubConn())
    ws._conn.recv = lambda: {"ok": True, "result": "Error: ExecutionTimeout: Code exceeded"}
    ws._call("execute", {"code": "while True: pass"}, 1)
    assert ws._execute_history == []

    ws._conn.recv = lambda: {"ok": True, "result": "OK"}
    ws._call("execute", {"code": "x = 1"}, 1)
    assert ws._execute_history == ["x = 1"]


def test_successful_op_whose_output_contains_timeout_word_is_still_logged():
    # Only the exact rolled-back sentinel ("Error: ExecutionTimeout:") is excluded —
    # a SUCCESSFUL op whose captured stdout or `# vars:` summary merely contains the
    # substring "ExecutionTimeout" must NOT be dropped from the replay log.
    ws = _stubbed_session(_StubConn())
    ws._conn.recv = lambda: {
        "ok": True,
        "result": "ExecutionTimeout\n# vars: msg='ExecutionTimeout'",
    }
    ws._call("execute", {"code": 'msg = "ExecutionTimeout"\nprint(msg)'}, 1)
    assert ws._execute_history == ['msg = "ExecutionTimeout"\nprint(msg)']


def test_vtk_subprocess_timeout_below_render_poll():
    # If an inner guard and the parent's poll expire together, the parent SIGKILLs
    # the worker and the session dies; both inner guards must win. render_view runs
    # tessellation then VTK sequentially, so their budgets ADD against the parent's
    # watchdog — the sum (plus margin) must stay under _RENDER_TIMEOUT.
    from build123d_mcp.tools.render import (
        _RENDER_OVERHEAD_MARGIN_S,
        _TESS_BUDGET_S,
        _VTK_BUDGET_S,
        _vtk_render_subprocess,
    )

    inner = inspect.signature(_vtk_render_subprocess).parameters["timeout"].default
    assert inner == _VTK_BUDGET_S
    # The two stages run sequentially, so their budgets ADD, plus parent-side
    # overhead — the sum + margin must stay within the parent watchdog.
    assert _TESS_BUDGET_S + _VTK_BUDGET_S + _RENDER_OVERHEAD_MARGIN_S <= _RENDER_TIMEOUT
