"""InProcessSession — the no-subprocess fallback for hosts that block spawn (#143).

Verifies the WorkerSession-compatible surface works with the Session living
in-process, and that the error contract matches the worker path (tool
exceptions surface as RuntimeError("TypeName: message")).
"""

import json
import os

import pytest

from build123d_mcp.worker import InProcessSession


@pytest.fixture(scope="module")
def session():
    return InProcessSession(exec_timeout=60)


def test_execute_and_show(session):
    out = session.execute("from build123d import *\nshow(Box(10, 10, 10), 'b')")
    assert "Registered 'b'" in out
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
    result = session.export_file("part", "step")
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
