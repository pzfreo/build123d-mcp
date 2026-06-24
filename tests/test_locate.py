"""locate_gate_defects() reports WHERE a solid fails the gate, with coordinates."""

import json
import subprocess

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.locate import locate_gate_defects


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def _payload(out: str) -> dict:
    return json.loads(out.split("\n", 1)[1])


def test_locate_valid_solid_has_no_defects(session):
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    out = locate_gate_defects(session, "part")
    assert "No validity defects" in out
    assert _payload(out)["count"] == 0


def test_locate_mesh_nonmanifold_edge_with_coordinates(session):
    """Two boxes meeting at an edge tessellate to an edge shared by >2 triangles —
    the tool must locate it with a 3D coordinate (here the shared edge at x=y=5)."""
    execute_code(session, "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'tt')")
    out = locate_gate_defects(session, "tt")
    defects = _payload(out)["defects"]
    nm = [d for d in defects if d["kind"] == "mesh_nonmanifold_edge"]
    assert nm, defects
    assert len(nm[0]["where"]) == 3
    assert nm[0]["shared_by_triangles"] > 2
    assert nm[0]["where"][0] == pytest.approx(5.0, abs=0.5)


def test_locate_falls_back_in_process_when_subprocess_blocked(session, monkeypatch):
    """On a host that blocks child processes (#143 / InProcessSession), subprocess.run
    raises OSError — the tool must still locate defects in-process, not break."""
    execute_code(session, "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'tt')")

    def _blocked(*a, **k):
        raise PermissionError("child process creation not permitted")

    monkeypatch.setattr(subprocess, "run", _blocked)
    out = locate_gate_defects(session, "tt")
    defects = _payload(out)["defects"]
    assert any(d["kind"] == "mesh_nonmanifold_edge" for d in defects)


def test_locate_timeout_is_a_clean_error(session, monkeypatch):
    execute_code(session, "show(Box(10, 10, 10), 'part')")

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="locate", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    out = locate_gate_defects(session, "part")
    assert "time budget" in out


def test_locate_unknown_object_errors(session):
    out = locate_gate_defects(session, "nope")
    assert "Unknown object" in out
