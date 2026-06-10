"""InProcessSession — the no-subprocess fallback for hosts that block spawn (#143).

Verifies the WorkerSession-compatible surface works with the Session living
in-process, and that the error contract matches the worker path (tool
exceptions surface as RuntimeError("TypeName: message")).

The parametrized smoke test reuses test_worker_boundary_coverage's per-op
inventory, so every stateful tool is exercised against InProcessSession — and
any future WorkerSession proxy method that touches self._proc directly (the
way reset() does) fails here instead of in a sandboxed user's session.
"""

import json
import os

import pytest

from build123d_mcp.worker import InProcessSession

from .test_worker_boundary_coverage import SESSION_STATEFUL_TOOLS


@pytest.fixture
def session():
    s = InProcessSession(exec_timeout=60)
    s.execute("from build123d import *\nshow(Box(10, 10, 10), 'b')")
    return s


@pytest.fixture
def seeded_in_process(tmp_path):
    """Same geometry seed as test_worker_boundary_coverage's seeded_ws."""
    s = InProcessSession(exec_timeout=60)
    s.execute(
        "from build123d import *\n"
        "show(Box(1, 1, 1), 'a')\n"
        "show(Box(1, 1, 1).move(Location((0, 0, 1))), 'b')\n"
    )
    s.save_snapshot("snap")
    return s


@pytest.mark.parametrize("op", sorted(SESSION_STATEFUL_TOOLS))
def test_stateful_tool_works_in_process(seeded_in_process, tmp_path, op):
    """Every stateful tool from the worker smoke inventory works in-process."""
    SESSION_STATEFUL_TOOLS[op](seeded_in_process, tmp_path)


def test_execute_and_show(session):
    out = session.execute("show(Cylinder(3, 10), 'c')")
    assert "Registered 'c'" in out
    assert "Error" not in out


def test_measure_round_trip(session):
    data = json.loads(session.measure("b"))
    assert data["volume"] == pytest.approx(1000, rel=0.01)
    assert data["topology"]["faces"] == 6


def test_session_state_sees_objects(session):
    state = json.loads(session.session_state())
    assert "b" in state["objects"]


def test_unknown_object_error_matches_worker_contract(session):
    """Worker path raises RuntimeError('ValueError: Unknown object ...');
    the in-process path must produce the identical surface."""
    with pytest.raises(RuntimeError, match=r"ValueError: Unknown object 'nope'"):
        session.measure("nope")


def test_execute_error_returns_error_string(session):
    out = session.execute("raise ValueError('boom')")
    assert out.startswith("Error:")
    assert "boom" in out


def test_export_round_trip(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = session.export_file("part", "step", object_name="b")
    assert "Exported to" in result
    assert os.path.exists("part.step")


def test_snapshot_round_trip(session):
    assert "saved" in session.save_snapshot("s1")
    session.execute("show(Cylinder(3, 10), 'c')")
    assert "restored" in session.restore_snapshot("s1")


def test_no_library_configured(session):
    assert session.has_library is False
    assert "No part library configured" in session.search_library("anything")


def test_reset(session):
    assert session.reset() == "Session reset."
    state = json.loads(session.session_state())
    assert state["objects"] == {}
