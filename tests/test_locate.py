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


def test_locate_mesh_open_edge_with_coordinates(session):
    """A box missing one face (5-faced Shell) is a genuinely unclosed tessellated
    boundary — mesh_open_edge, distinct from mesh_nonmanifold_edge — the class that
    previously came back with zero located defects (the locator had no open-edge
    check at all, only non-manifold-edge/-vertex), forcing an agent to hand-roll
    triangulation to find it. The missing top face's 4 rim edges must be located."""
    execute_code(session, "show(Shell(Box(10, 10, 10).faces()[:5]), 'opn')")
    out = locate_gate_defects(session, "opn")
    defects = _payload(out)["defects"]
    op = [d for d in defects if d["kind"] == "mesh_open_edge"]
    assert len(op) == 4, defects
    assert all(len(d["where"]) == 3 for d in op)


def test_mesh_vertex_deflection_defect_with_coordinates(monkeypatch):
    """#397's field bug: a healed patch's tessellated edge endpoint misses its own
    BREP vertex by a fraction of a millimetre, silently welded shut instead of
    reported. Real malformed geometry that reproduces this is a repair-patch
    artifact — sewing two faces with a mismatched boundary heals the mismatch
    geometrically rather than leaving it raw, and hand-editing a triangulation node
    is discarded the next time BRepMesh_IncrementalMesh runs (which this check
    always does) — so this drives the exact code path the real bug hit (the
    BRep_Tool.Pnt_s lookup inside the vertex-merge guard) directly, on an otherwise
    clean Box, matching the same technique used for the gate-side equivalent in
    test_validate.py::test_mesh_defects_exact_detects_vertex_deflection. Calls the
    locator's internal function directly rather than through locate_gate_defects()
    (which runs in a subprocess a test-time monkeypatch can't reach), the same way
    test_validate.py's _mesh_defects_exact tests already do for the gate."""
    from build123d import Box
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

    from build123d_mcp._locate_subprocess import _mesh_vertex_deflection_defects

    box = Box(10, 10, 10)
    target = box.vertices()[0].wrapped
    orig_pnt_s = BRep_Tool.Pnt_s

    def _lying_pnt_s(v):
        p = orig_pnt_s(v)
        if v.IsSame(target):
            return gp_Pnt(p.X() + 1.0, p.Y() + 1.0, p.Z() + 1.0)
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))
    defects = _mesh_vertex_deflection_defects(box)
    assert len(defects) == 1, defects
    d = defects[0]
    assert d["kind"] == "mesh_vertex_deflection_defect"
    assert len(d["where"]) == 3
    assert d["max_deviation_mm"] == pytest.approx(1.0, abs=0.05)


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


def test_locate_mesh_nonmanifold_vertex(session):
    """Corner-to-corner touch fails the gate via a non-manifold VERTEX (#298) — it
    must be located, not reported as a false 'clean' (no edge/face defect here)."""
    execute_code(session, "show(Box(10, 10, 10) + Pos(10, 10, 10) * Box(10, 10, 10), 'corner')")
    out = locate_gate_defects(session, "corner")
    defects = _payload(out)["defects"]
    nmv = [d for d in defects if d["kind"] == "mesh_nonmanifold_vertex"]
    assert nmv, out
    assert len(nmv[0]["where"]) == 3


def _open_shell_solid():
    """A box missing one face — an open (non-watertight) solid with 4 open edges."""
    from build123d import Box, Solid
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.TopoDS import TopoDS_Shell

    b = BRep_Builder()
    shell = TopoDS_Shell()
    b.MakeShell(shell)
    for f in Box(10, 10, 10).faces()[:5]:
        b.Add(shell, f.wrapped)
    return Solid(BRepBuilderAPI_MakeSolid(shell).Solid())


def test_locate_checks_every_solid_of_a_compound():
    """B-rep checks must cover ALL solids of a compound, not just the first — an
    open edge on a non-first solid must not be a false 'clean'."""
    from build123d import Box, Compound, Location

    from build123d_mcp._locate_subprocess import collect_defects

    clean = Box(10, 10, 10)
    leaky = _open_shell_solid().moved(Location((30, 0, 0)))
    defects = collect_defects(Compound(children=[clean, leaky]))  # clean solid first
    assert any(d["kind"] == "open_edge" for d in defects), [d["kind"] for d in defects]
