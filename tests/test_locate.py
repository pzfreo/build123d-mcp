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
    payload = _payload(out)
    assert payload["count"] == 0
    assert payload["diagnosis"]["status"] == "no_located_defects"
    assert payload["diagnosis"]["primary_kind"] is None


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
    assert nm[0]["diagnostic_class"] == "mesh_topology"
    assert nm[0]["repair_family"] == "separate_self_touch_or_redo_boolean"
    assert "export" in nm[0]["verify_after_repair"]
    diagnosis = _payload(out)["diagnosis"]
    assert diagnosis["primary_kind"] == "mesh_nonmanifold_edge"
    assert diagnosis["counts_by_kind"]["mesh_nonmanifold_edge"] >= 1
    assert "mesh_topology" in diagnosis["diagnostic_classes"]


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
    assert d["deflection_mm"] == pytest.approx(0.017321, abs=1e-6)


def test_mesh_vertex_deflection_locator_uses_gate_ladder(monkeypatch):
    """#398: locate the same ladder-only vertex that the gate counts.

    The missing face keeps the shell open through every rung. The 0.01 mm
    displacement is below the base threshold but above base/4, so a base-only
    locator returns nothing while the gate and this locator must both report it.
    """
    from build123d import Box, Shell
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

    from build123d_mcp._locate_subprocess import _mesh_vertex_deflection_defects
    from build123d_mcp.tools.validate import _mesh_defects_exact

    shell = Shell(Box(10, 10, 10).faces()[:5])
    target = shell.vertices()[0].wrapped
    orig_pnt_s = BRep_Tool.Pnt_s

    def _lying_pnt_s(v):
        p = orig_pnt_s(v)
        if v.IsSame(target):
            return gp_Pnt(p.X() + 0.01, p.Y(), p.Z())
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))

    evidence = {}
    gate = _mesh_defects_exact(shell, vertex_deflection_evidence=evidence)
    defects = _mesh_vertex_deflection_defects(shell)

    assert gate.vertex_deflection_defects == 1
    assert len(evidence) == 1
    assert len(defects) == gate.vertex_deflection_defects
    assert defects[0]["max_deviation_mm"] == pytest.approx(0.01, abs=0.001)
    assert defects[0]["deflection_mm"] < 0.01


def test_mesh_vertex_deflection_locator_reports_the_coarsest_failing_rung(monkeypatch):
    """A vertex that already misses at base must report the BASE threshold.

    The shell stays open through every rung, so /4../32 also flag this vertex. Were
    the finest rung to win, a 1 mm miss would be reported against a ~0.0005 mm
    deflection and read as a hair-splitting tolerance issue rather than the gross
    defect the gate actually fails on.
    """
    from build123d import Box, Shell
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

    from build123d_mcp._locate_subprocess import _mesh_vertex_deflection_defects

    shell = Shell(Box(10, 10, 10).faces()[:5])
    target = shell.vertices()[0].wrapped
    orig_pnt_s = BRep_Tool.Pnt_s

    def _lying_pnt_s(v):
        p = orig_pnt_s(v)
        if v.IsSame(target):
            return gp_Pnt(p.X() + 1.0, p.Y(), p.Z())
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))

    defects = _mesh_vertex_deflection_defects(shell)

    assert len(defects) == 1, defects
    assert defects[0]["max_deviation_mm"] == pytest.approx(1.0, abs=0.05)
    assert defects[0]["deflection_mm"] == pytest.approx(0.017321, abs=1e-6)


def test_mesh_vertex_deflection_locator_stops_at_first_closed_rung(monkeypatch):
    """A clean base rung must not expose a defect that exists only below base."""
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
            return gp_Pnt(p.X() + 0.01, p.Y(), p.Z())
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))

    # Base deflection is ~0.0173 mm and the closed Box does not enter /4.
    assert _mesh_vertex_deflection_defects(box) == []


def test_mesh_vertex_deflection_locator_bounds_exact_gate(monkeypatch):
    """The caller's deadline reaches the gate; without one the gate's own budget applies."""
    import time

    from build123d import Box

    import build123d_mcp.tools.validate as validate_module
    from build123d_mcp._locate_subprocess import (
        _VERTEX_DEFLECTION_MAX_TRIS,
        _mesh_vertex_deflection_defects,
    )

    captured = {}

    def _bounded_exact(shape, **kwargs):
        captured.update(kwargs)
        return validate_module.MeshGateResult(ok=True)

    monkeypatch.setattr(validate_module, "_mesh_defects_exact", _bounded_exact)

    deadline = time.monotonic() + 100
    assert _mesh_vertex_deflection_defects(Box(10, 10, 10), deadline) == []
    assert captured["deadline"] == deadline
    assert captured["run_refined_probe"] is False
    assert captured["max_triangles"] == _VERTEX_DEFLECTION_MAX_TRIS

    # No caller deadline: the gate falls back to its own finite mesh budget, which
    # is what keeps the finer-rung triangle ceiling active.
    assert _mesh_vertex_deflection_defects(Box(10, 10, 10)) == []
    assert captured["deadline"] is None


def test_expired_deadline_does_not_start_locator_mesh(monkeypatch):
    import time

    from build123d import Box

    from build123d_mcp._locate_subprocess import _weld

    box = Box(10, 10, 10)

    def _unexpected_tessellate(*args, **kwargs):
        raise AssertionError("expired locator started native tessellation")

    monkeypatch.setattr(box, "tessellate", _unexpected_tessellate)

    with pytest.raises(RuntimeError, match="not enough time remains"):
        _weld(box, time.monotonic() - 1)


def test_expired_deadline_does_not_start_refined_probe(monkeypatch):
    import time

    import OCP.BRepMesh as brep_mesh
    from build123d import Box

    from build123d_mcp._locate_subprocess import _mesh_refined_untriangulated_faces

    def _unexpected_mesh(*args, **kwargs):
        raise AssertionError("expired locator started refined native tessellation")

    monkeypatch.setattr(brep_mesh, "BRepMesh_IncrementalMesh", _unexpected_mesh)

    with pytest.raises(RuntimeError, match="not enough time remains"):
        _mesh_refined_untriangulated_faces(Box(10, 10, 10), time.monotonic() - 1)


def test_locate_passes_its_own_budget_to_the_subprocess(session, monkeypatch):
    """The subprocess must know the deadline it is killed at, or its internal mesh
    budget can outlast the parent and lose every defect the cheap checks found."""
    execute_code(session, "from build123d import *\nresult = Box(10, 10, 10)")
    captured = {}

    def _capture(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(cmd="locate", timeout=1)

    monkeypatch.setattr(subprocess, "run", _capture)
    locate_gate_defects(session)

    assert float(captured["cmd"][-1]) == pytest.approx(captured["timeout"])


def test_base_untriangulated_face_has_coordinates_without_weld_crash(monkeypatch):
    """A face missing at the base mesh tolerance is the defect, not locator_error."""
    from build123d import Box
    from OCP.BRep import BRep_Tool

    from build123d_mcp._locate_subprocess import collect_defects

    box = Box(10, 10, 10)
    target = box.faces()[0].wrapped
    orig_tri = BRep_Tool.Triangulation_s

    def _missing_tri(face, loc):
        if face.IsSame(target):
            return None
        return orig_tri(face, loc)

    monkeypatch.setattr(BRep_Tool, "Triangulation_s", staticmethod(_missing_tri))

    defects = collect_defects(box)
    missing = [d for d in defects if d["kind"] == "mesh_untriangulated_face"]
    assert len(missing) == 1, defects
    assert missing[0]["face_index"] >= 1
    assert len(missing[0]["where"]) == 3
    assert not any(d["kind"] == "locator_error" and "NbNodes" in d["detail"] for d in defects)


def test_refined_untriangulated_face_with_coordinates(monkeypatch):
    """The locator should point at a face that only disappears during the refined
    triangulation probe, matching the gate's `refined_untriangulated_faces` count."""
    import OCP.BRepMesh as brep_mesh
    from build123d import Box
    from OCP.BRep import BRep_Tool

    from build123d_mcp._locate_subprocess import _mesh_refined_untriangulated_faces

    box = Box(10, 10, 10)
    state = {"deflection": None, "missed": False}
    orig_mesh = brep_mesh.BRepMesh_IncrementalMesh
    orig_tri = BRep_Tool.Triangulation_s

    def _tracking_mesh(*args):
        if len(args) >= 2 and isinstance(args[1], (int, float)):
            state["deflection"] = float(args[1])
        return orig_mesh(*args)

    def _refined_only_missing_tri(face, loc):
        if state["deflection"] is not None and state["deflection"] < 0.005 and not state["missed"]:
            state["missed"] = True
            return None
        return orig_tri(face, loc)

    monkeypatch.setattr(brep_mesh, "BRepMesh_IncrementalMesh", _tracking_mesh)
    monkeypatch.setattr(BRep_Tool, "Triangulation_s", staticmethod(_refined_only_missing_tri))

    defects = _mesh_refined_untriangulated_faces(box)
    assert len(defects) == 1, defects
    d = defects[0]
    assert d["kind"] == "mesh_refined_untriangulated_face"
    assert d["face_index"] >= 1
    assert len(d["where"]) == 3
    assert d["deflection_mm"] < 0.005


def test_refined_untriangulated_locator_ignores_base_missing_face(monkeypatch):
    """A face that is already missing at the base gate deflection is not a
    refined-only defect; the locator should leave that class to the gate's base
    `untriangulated_faces` report instead of emitting a misleading repair hint."""
    import OCP.BRepMesh as brep_mesh
    from build123d import Box
    from OCP.BRep import BRep_Tool

    from build123d_mcp._locate_subprocess import _mesh_refined_untriangulated_faces

    box = Box(10, 10, 10)
    state = {"deflection": None, "missed_base": False}
    orig_mesh = brep_mesh.BRepMesh_IncrementalMesh
    orig_tri = BRep_Tool.Triangulation_s

    def _tracking_mesh(*args):
        if len(args) >= 2 and isinstance(args[1], (int, float)):
            state["deflection"] = float(args[1])
        return orig_mesh(*args)

    def _base_only_missing_tri(face, loc):
        if (
            state["deflection"] is not None
            and state["deflection"] >= 0.005
            and not state["missed_base"]
        ):
            state["missed_base"] = True
            return None
        return orig_tri(face, loc)

    monkeypatch.setattr(brep_mesh, "BRepMesh_IncrementalMesh", _tracking_mesh)
    monkeypatch.setattr(BRep_Tool, "Triangulation_s", staticmethod(_base_only_missing_tri))

    assert _mesh_refined_untriangulated_faces(box) == []


def test_locate_falls_back_in_process_when_subprocess_blocked(session, monkeypatch):
    """A host without child processes gets bounded B-rep diagnostics, not native mesh."""
    execute_code(session, "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'tt')")

    def _blocked(*a, **k):
        raise PermissionError("child process creation not permitted")

    monkeypatch.setattr(subprocess, "run", _blocked)
    out = locate_gate_defects(session, "tt")
    defects = _payload(out)["defects"]
    assert any(
        d["kind"] == "locator_error" and "bounded child process" in d["detail"] for d in defects
    )
    assert not any(d["kind"].startswith("mesh_") for d in defects)


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
