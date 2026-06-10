"""Timeout-tier routing and session-loss messaging in WorkerSession (issues #214-#216).

A timeout on any worker op SIGKILLs the worker and destroys all session state,
so geometry-heavy ops must get a budget that complex parts can actually meet
(#214), every restart path must tell the caller that state was lost (#215),
and the macOS VTK subprocess guard must fire before the parent's render poll
so a VTK hang surfaces as a clean error instead of a dead session (#216).

These tests stub the pipe/process layer — no worker subprocess is spawned.
"""

import inspect

import pytest

from build123d_mcp.security import ExecutionTimeout
from build123d_mcp.worker import WorkerSession

_STATE_LOSS_MARKER = "has been lost"


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
    ws.shape_compare("a", "b")
    ws.align_check("a", "b")
    ws.analyze_printability("a")
    ws.save_snapshot("s")
    ws.diff_snapshot("s")
    ws.resolve("a", ".faces()")
    ws.suggest_view_layout("a")

    assert all(t == WorkerSession._GEOMETRY_TIMEOUT for _op, t in record), record
    assert WorkerSession._GEOMETRY_TIMEOUT > WorkerSession._SHORT_TIMEOUT


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
    assert record == [("import_cad_file", WorkerSession._EXPORT_TIMEOUT)]


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
    assert record == [("load_part", WorkerSession._GEOMETRY_TIMEOUT)]


def test_bookkeeping_ops_keep_short_timeout():
    record = []
    ws = _proxy_session(record)

    ws.session_state()
    ws.objects_types()
    ws.last_error()
    ws.script()
    ws.restore_snapshot("s")
    ws.search_library("q")

    assert all(t == WorkerSession._SHORT_TIMEOUT for _op, t in record), record


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


def _stubbed_session(conn, alive=True):
    ws = WorkerSession.__new__(WorkerSession)
    ws._conn = conn
    ws._proc = _StubProc(alive=alive)
    ws._exec_timeout = 120
    ws._kill_worker = lambda: None
    ws._start_worker = lambda: None
    return ws


def test_generic_timeout_message_reports_session_loss():
    ws = _stubbed_session(_StubConn(poll_result=False))
    with pytest.raises(RuntimeError, match=_STATE_LOSS_MARKER):
        ws._call("measure", {}, 1)


def test_execute_timeout_message_reports_session_loss():
    ws = _stubbed_session(_StubConn(poll_result=False))
    with pytest.raises(ExecutionTimeout, match=_STATE_LOSS_MARKER):
        ws._call("execute", {"code": "pass"}, 1)


def test_dead_worker_message_reports_session_loss():
    ws = _stubbed_session(_StubConn(), alive=False)
    with pytest.raises(RuntimeError, match=_STATE_LOSS_MARKER):
        ws._call("measure", {}, 1)


def test_mid_call_crash_message_reports_session_loss():
    ws = _stubbed_session(_StubConn(recv_exc=EOFError()))
    with pytest.raises(RuntimeError, match=_STATE_LOSS_MARKER):
        ws._call("measure", {}, 1)


def test_vtk_subprocess_timeout_below_render_poll():
    # If the inner guard and the parent's poll expire together, the parent
    # SIGKILLs the worker and the session dies; the inner guard must win.
    from build123d_mcp.tools.render import _vtk_render_subprocess

    inner = inspect.signature(_vtk_render_subprocess).parameters["timeout"].default
    assert inner < WorkerSession._RENDER_TIMEOUT
