"""WorkerSession end-to-end routing for state-dependent tools (issue #179).

align_check, resolve, suggest_view_layout, and script read Session state
(objects, current_shape, geometry_refs, execute_history) that lives in the
worker subprocess. In production ``_session`` is a ``WorkerSession`` proxy, so
each must dispatch into the worker instead of reading the empty parent proxy.
These tests spawn a real WorkerSession and exercise the proxy methods that
server.py calls.
"""

import json

import pytest

from build123d_mcp.worker import WorkerSession


@pytest.fixture
def ws():
    s = WorkerSession(exec_timeout=30)
    s.execute(
        "from build123d import *\n"
        "show(Box(1, 1, 1), 'a')\n"
        "show(Box(1, 1, 1).move(Location((0, 0, 1))), 'b')\n"
    )
    try:
        yield s
    finally:
        s._kill_worker()


def test_align_check_routes_to_worker(ws):
    result = json.loads(ws.align_check("a", "b", axis="Z", mode="center"))
    assert "error" not in result
    assert result["object_a"] == "a"
    # b's centre sits 1 mm above a's on Z; delta = a_cen - b_cen = -1.0
    assert abs(result["delta"] + 1.0) < 1e-6


def test_resolve_routes_to_worker(ws):
    result = json.loads(ws.resolve("a", ".faces().sort_by(Axis.Z)[-1]"))
    assert "error" not in result
    assert result["type"] == "Face"


def test_resolve_label_persists_in_worker_session_state(ws):
    ws.resolve("a", ".faces().sort_by(Axis.Z)[-1]", label="top")
    state = json.loads(ws.session_state())
    assert "top" in state["geometry_refs"]


def test_suggest_view_layout_routes_to_worker(ws):
    result = json.loads(ws.suggest_view_layout("a"))
    assert "error" not in result
    assert "views" in result


def test_script_routes_to_worker(ws):
    result = json.loads(ws.script())
    # The fixture runs exactly one execute() call; execute_history lives in the
    # worker, so the parent proxy would report 0 blocks (the #179 bug).
    assert result["blocks"] == 1
    assert "Box(1, 1, 1)" in result["script"]
