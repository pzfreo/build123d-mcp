"""Pre-export validity gate (validate tool + export warning).

The gate mirrors the hard validity check CAD scorers apply before any geometric
scoring: a non-watertight / non-manifold / non-solid artifact scores zero. These
tests pin that a real solid passes and the common invalid-artifact shapes (2D
sketch, open shell, un-fused/degenerate result) fail with actionable reasons.
"""

import json
import os

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.export import export_file
from build123d_mcp.tools.validate import _gate_report, validate


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def test_solid_box_passes(session):
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    out = validate(session, "part")
    assert out.startswith("Validity gate: PASS")
    report = json.loads(out.split("\n", 1)[1])
    assert report["passes_gate"] is True
    assert report["n_solids"] == 1
    assert report["watertight_manifold"] is True
    assert report["open_edges"] == 0
    assert report["nonmanifold_edges"] == 0
    assert report["mesh_nonmanifold_edges"] == 0
    assert report["brep_valid"] is True
    assert report["reasons"] == []


def test_curved_solid_no_mesh_false_positive(session):
    """The mesh non-manifold check must not false-positive on clean curved
    geometry (the per-face tessellation is welded first). A cylinder with a bore
    passes with zero mesh defects."""
    execute_code(session, "show(Cylinder(8, 20) - Cylinder(3, 20), 'tube')")
    report = _gate_report(session.objects["tube"])
    assert report["mesh_nonmanifold_edges"] == 0
    assert report["passes_gate"] is True


def test_free_annotation_edges_ignored(session):
    """A clean solid carrying free wire edges (PMI annotation curves from an
    imported STEP, or stray construction geometry) must still PASS — free edges
    have no incident face and are not open boundaries. Regression for the gate
    false-FAILing clean NIST solids whose AP203 file carried dozens of annotation
    edges."""
    execute_code(
        session,
        "from build123d import Edge, Compound\n"
        "solid = Box(20, 20, 20)\n"
        "wire1 = Edge.make_line((40, 0, 0), (60, 0, 0))\n"
        "wire2 = Edge.make_line((0, 40, 0), (0, 60, 0))\n"
        "show(Compound(children=[solid, wire1, wire2]), 'annotated')",
    )
    report = _gate_report(session.objects["annotated"])
    assert report["open_edges"] == 0
    assert report["passes_gate"] is True


def test_mesh_nonmanifold_edge_fails(session):
    """Two solids meeting along a shared edge tessellate to a mesh edge shared by
    >2 triangles — the dominant invalid-but-watertight CADGenBench failure mode."""
    execute_code(
        session,
        "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'edge_touch')",
    )
    report = _gate_report(session.objects["edge_touch"])
    assert report["mesh_nonmanifold_edges"] > 0
    assert report["passes_gate"] is False
    assert any("mesh non-manifold edge" in r for r in report["reasons"])


def test_mesh_exact_curved_no_false_positive(session):
    """The accurate topology-stitch mesh check (used at export) must not
    false-positive on clean curved geometry — it is tolerance-free, so unlike the
    fast coordinate weld it cannot invent a non-manifold edge from rounding."""
    execute_code(session, "show(Cylinder(8, 20) - Cylinder(3, 20), 'tube')")
    report = _gate_report(session.objects["tube"], exact=True)
    assert report["mesh_nonmanifold_edges"] == 0
    assert report["passes_gate"] is True


@pytest.mark.parametrize(
    "code",
    [
        "Box(10, 10, 10)",
        "Cylinder(5, 20)",
        "Sphere(8)",
        "Box(20, 20, 10) - Cylinder(4, 12)",
        "fillet(Box(10, 10, 10).edges(), 1.5)",
    ],
)
def test_clean_solids_pass_exact_with_zero_open_edges(session, code):
    """The exact gate's open-edge (closedness) check must report zero open edges
    on clean solids — including curved/periodic bodies whose tessellated seams a
    single-deflection coordinate weld false-FAILed. The deflection ladder closes
    a valid seam at a finer rung; a genuine gap stays open at every rung. The
    report exposes both new keys regardless of verdict."""
    execute_code(session, f"show({code}, 'p')")
    report = _gate_report(session.objects["p"], exact=True)
    assert "mesh_open_edges" in report
    assert "untriangulated_faces" in report
    assert "refined_untriangulated_faces" in report
    assert report["mesh_open_edges"] == 0
    assert report["untriangulated_faces"] == 0
    assert report["refined_untriangulated_faces"] == 0
    assert report["passes_gate"] is True


def test_mesh_exact_nonmanifold_edge_fails(session):
    """The accurate mesh check detects the edge-touch non-manifold: the fused
    shared edge stitches the four incident faces into a mesh edge shared by >2
    triangles."""
    execute_code(
        session,
        "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'edge_touch')",
    )
    report = _gate_report(session.objects["edge_touch"], exact=True)
    assert report["mesh_nonmanifold_edges"] > 0
    assert report["passes_gate"] is False


def test_mesh_exact_passes_disjoint_touching_bodies():
    """Two valid solids that merely touch (coincident face) are two manifold
    bodies, not a non-manifold solid. The topology-stitch exact check correctly
    passes them — it stitches only topologically-shared edges — where the fast
    coordinate weld over-flags (it merges the distinct bodies by proximity).
    Regression guarding the exact check against reverting to a coordinate weld."""
    from build123d import Box, Compound, Pos

    from build123d_mcp.tools.validate import _mesh_defects, _mesh_defects_exact

    two = Compound([Box(10, 10, 10), Pos(0, 0, 10) * Box(10, 10, 10)])
    assert _mesh_defects(two)[0] > 0  # fast weld over-flags two touching bodies
    assert _mesh_defects_exact(two).nonmanifold_edges == 0  # exact topology is correct


def test_validate_small_part_uses_exact_check(session):
    """A small part is cheap to stitch, so interactive validate() runs the exact
    mesh check (not just the fast fallback) — recorded in mesh_check."""
    execute_code(session, "show(Box(10, 10, 10), 'b')")
    report = _gate_report(session.objects["b"])  # default inline path
    assert report["mesh_check"] == "exact"
    assert report["passes_gate"] is True


def test_mesh_check_falls_back_to_fast_over_budget(session, monkeypatch):
    """Above the triangle budget the gate falls back to the fast check instead of
    running (or hanging on) the slow stitch — the perf guard."""
    import build123d_mcp.tools.validate as v

    monkeypatch.setattr(v, "_EXACT_INLINE_MAX_TRIS", 1)
    execute_code(session, "show(Box(10, 10, 10), 'b')")
    report = _gate_report(session.objects["b"])
    assert report["mesh_check"] == "fast"
    assert report["passes_gate"] is True


def test_2d_sketch_fails(session):
    execute_code(session, "show(Rectangle(5, 5), 'sk')")
    out = validate(session, "sk")
    assert out.startswith("Validity gate: FAIL")
    report = json.loads(out.split("\n", 1)[1])
    assert report["passes_gate"] is False
    assert report["n_solids"] == 0
    assert any("solid" in r for r in report["reasons"])


def test_open_shell_fails(session):
    # Five of a box's six faces — an open (non-watertight) shell with boundary
    # edges that belong to only one face.
    execute_code(session, "b = Box(10, 10, 10)\nshow(Shell(b.faces()[:5]), 'open')")
    report = _gate_report(session.objects["open"])
    assert report["passes_gate"] is False
    assert report["open_edges"] > 0
    assert report["watertight_manifold"] is False


def test_closed_solid_with_unreliable_is_manifold_passes():
    """Regression for the gate's reliance on build123d.is_manifold (#276): an
    imported closed solid can report is_manifold=False while being watertight
    (zero open/non-manifold edges). The edge-defect test must pass it where the
    old is_manifold check would false-FAIL. Synthesized here as a fused solid;
    the real-world trigger was NIST CAD model imports."""
    from build123d import Box, Cylinder, Pos

    part = Box(20, 20, 10) - Pos(0, 0, 0) * Cylinder(4, 10)
    report = _gate_report(part)
    assert report["open_edges"] == 0
    assert report["nonmanifold_edges"] == 0
    assert report["watertight_manifold"] is True
    assert report["passes_gate"] is True


def test_degenerate_result_fails(session):
    # Intersection of disjoint solids → empty/degenerate.
    execute_code(
        session,
        "a = Box(10, 10, 10)\n"
        "b = Box(10, 10, 10).move(Location((100, 0, 0)))\n"
        "show(a & b, 'empty')",
    )
    report = _gate_report(session.objects["empty"])
    assert report["passes_gate"] is False
    assert any("volume" in r or "solid" in r for r in report["reasons"])


def test_multi_body_passes_with_advisory(session):
    """Two disjoint solids are each watertight (gate passes), but a single-part
    task wants one body — surface a non-fatal advisory, not a FAIL."""
    execute_code(
        session,
        "a = Box(10, 10, 10)\n"
        "b = Box(10, 10, 10).move(Location((30, 0, 0)))\n"
        "show(Compound([a, b]), 'two')",
    )
    report = _gate_report(session.objects["two"])
    assert report["n_solids"] == 2
    assert report["passes_gate"] is True  # disjoint closed solids are still watertight
    assert any("disjoint" in w for w in report["warnings"])
    out = validate(session, "two")
    assert out.startswith("Validity gate: PASS")
    assert "warning" in out and "disjoint" in out


def test_validate_unknown_object_reports_error(session):
    out = validate(session, "nope")
    assert "error" in out and "nope" in out


def test_validate_uses_current_shape_when_unnamed(session):
    execute_code(session, "result = Box(4, 4, 4)")
    out = validate(session)
    assert out.startswith("Validity gate: PASS")


def test_export_3d_warns_when_gate_fails(session, tmp_path, monkeypatch):
    """A 3D export consults the gate and appends a warning when it fails, so the
    agent never silently ships a zero-scoring artifact. The no-solids cases are
    already blocked earlier by the 2D/3D format guard, so the reachable case is a
    solid that fails the gate — inject one to test the wiring deterministically."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    import build123d_mcp.tools.validate as v

    monkeypatch.setattr(
        v,
        "_gate_report",
        lambda shape, exact=False, mesh_override=None: {
            "passes_gate": False,
            "reasons": ["injected non-manifold solid"],
            "mesh_check": "exact-subprocess",
        },
    )
    out = export_file(session, "out", "step", object_name="part")
    assert os.path.exists("out.step")  # the file is still written
    assert "VALIDITY GATE FAIL" in out
    assert "injected non-manifold solid" in out


def test_export_3d_valid_no_warning(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    out = export_file(session, "out", "step", object_name="part")
    assert os.path.exists("out.step")
    assert "VALIDITY GATE FAIL" not in out  # a real box round-trips valid


def test_export_gate_validates_reimported_file_not_memory(session, tmp_path, monkeypatch):
    """Regression for #284: export() must gate the written-and-reimported STEP, not
    the in-memory shape. A valid in-memory solid whose written file degrades on
    serialization must still trigger the warning. Simulated by patching import_step
    to return an invalid (open-shell) shape — under the old in-memory gate this
    box would pass clean."""
    import build123d

    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    bad = build123d.Shell(build123d.Box(10, 10, 10).faces()[:5])  # 0 solids, open -> invalid
    monkeypatch.setattr(build123d, "import_step", lambda _p: bad)
    out = export_file(session, "out", "step", object_name="part")
    assert os.path.exists("out.step")  # file is still written
    assert "VALIDITY GATE FAIL" in out  # gate caught the (degraded) re-imported file


def test_export_gate_warns_when_reimport_fails(session, tmp_path, monkeypatch):
    """If the written STEP can't even be re-imported, a scorer would reject it —
    the gate must warn rather than silently pass."""
    import build123d

    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'part')")

    def boom(_p):
        raise RuntimeError("corrupt STEP")

    monkeypatch.setattr(build123d, "import_step", boom)
    out = export_file(session, "out", "step", object_name="part")
    assert "VALIDITY GATE FAIL" in out
    assert "could not be re-imported" in out


# --- edge-incidence detection logic (the open / non-manifold core of the gate) ---
# Deterministic unit tests of the counter that drives mesh_open_edges and
# mesh_nonmanifold_edges, so a regression in that detection is caught in CI even
# without a large geometric fixture (the full OCC stitch + ladder is validated
# against the CADGenBench corpus out-of-band).


def test_edge_incidence_closed_tetrahedron():
    import numpy as np

    from build123d_mcp.tools.validate import _edge_incidence_counts

    # Closed 2-manifold: 4 triangles, every edge incident to exactly 2.
    mf = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3], [0, 2, 3]], dtype=np.int64)
    counts = _edge_incidence_counts(mf, 4)
    assert int((counts == 1).sum()) == 0  # no open edges
    assert int((counts > 2).sum()) == 0  # no non-manifold edges


def test_edge_incidence_open_mesh():
    import numpy as np

    from build123d_mcp.tools.validate import _edge_incidence_counts

    # Drop one face of the tetrahedron — the boundary is now open.
    mf = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3]], dtype=np.int64)
    counts = _edge_incidence_counts(mf, 4)
    assert int((counts == 1).sum()) > 0  # open (1-incident) edges detected


def test_edge_incidence_nonmanifold_edge():
    import numpy as np

    from build123d_mcp.tools.validate import _edge_incidence_counts

    # Edge (0, 1) shared by three triangles — non-manifold.
    mf = np.array([[0, 1, 2], [0, 1, 3], [0, 1, 4]], dtype=np.int64)
    counts = _edge_incidence_counts(mf, 5)
    assert int((counts > 2).sum()) > 0


def test_nonmanifold_vertex_bowtie():
    import numpy as np

    from build123d_mcp.tools.validate import _nonmanifold_vertex_count

    # Two triangles meeting only at vertex 0 — two surface sheets pinched at a
    # point. Edge-manifold (no edge shared >2 ways) but a non-manifold vertex.
    mf = np.array([[0, 1, 2], [0, 3, 4]], dtype=np.int64)
    assert _nonmanifold_vertex_count(mf) == 1


def test_nonmanifold_vertex_closed_tetra():
    import numpy as np

    from build123d_mcp.tools.validate import _nonmanifold_vertex_count

    # Closed 2-manifold: every vertex's incident triangles form one fan.
    mf = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3], [0, 2, 3]], dtype=np.int64)
    assert _nonmanifold_vertex_count(mf) == 0


def test_mesh_defects_exact_no_false_nm_vertex_on_clean_solids():
    from build123d import Box, Sphere

    from build123d_mcp.tools.validate import _mesh_defects_exact

    # The vertex check runs on a coordinate-welded mesh, so poles/seams of a
    # sphere must not register as pinches (#298 regression guard).
    for shape in (Box(10, 10, 10), Sphere(8)):
        result = _mesh_defects_exact(shape)
        assert result.ok
        assert result.refined_untriangulated_faces == 0
        assert result.nonmanifold_vertices == 0
        assert result.vertex_deflection_defects == 0


def test_mesh_defects_exact_detects_refined_untriangulated_face(monkeypatch):
    """A face can tessellate at the gate's base deflection but fail when the mesh is
    refined. The refined probe must report that as its own defect class instead of
    relying on the open-edge ladder, which only escalates when the base mesh is open."""
    import OCP.BRepMesh as brep_mesh
    from build123d import Box
    from OCP.BRep import BRep_Tool

    from build123d_mcp.tools.validate import _mesh_defects_exact

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

    result = _mesh_defects_exact(box)
    assert result.ok
    assert result.refined_verified is True
    assert result.nonmanifold_edges == 0
    assert result.open_edges == 0
    assert result.untriangulated_faces == 0
    assert result.nonmanifold_vertices == 0
    assert result.vertex_deflection_defects == 0
    assert result.refined_untriangulated_faces == 1


def test_gate_report_fails_on_refined_untriangulated_face(monkeypatch):
    import OCP.BRepMesh as brep_mesh
    from build123d import Box
    from OCP.BRep import BRep_Tool

    from build123d_mcp.tools.validate import _gate_report

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

    report = _gate_report(Box(10, 10, 10), exact=True)
    assert report["passes_gate"] is False
    assert report["untriangulated_faces"] == 0
    assert report["refined_untriangulated_faces"] == 1
    assert report["refined_untriangulated_faces_verified"] is True
    assert any("finer mesh deflection" in r for r in report["reasons"])


def test_refined_probe_budget_gap_is_reported_explicitly(monkeypatch):
    """A known defect plus an over-budget refined probe must not read as refined-clean."""
    from build123d import Box, Shell

    import build123d_mcp.tools.validate as v

    monkeypatch.setattr(v, "_REFINED_UNTRIANGULATED_MAX_TRIS", 1)
    shell = Shell(Box(10, 10, 10).faces()[:5])

    result = v._mesh_defects_exact(shell)
    assert result.ok
    assert result.open_edges > 0
    assert result.refined_untriangulated_faces == 0
    assert result.refined_verified is False

    report = v._gate_report(shell, exact=True, mesh_override=result)
    assert report["passes_gate"] is False
    assert report["refined_untriangulated_faces"] == 0
    assert report["refined_untriangulated_faces_verified"] is False
    assert any("refined face-tessellation not verified" in w for w in report["warnings"])


def test_mesh_defects_exact_detects_vertex_deflection(monkeypatch):
    """A genuinely off-vertex tessellated node — the #397 field bug (a healed
    patch's polygon endpoint missing its BREP vertex by a fraction of a mm) —
    must be reported, not silently welded shut. Real malformed geometry that
    reproduces this class of defect is a repair-patch artifact, not something
    a synthetic construction (a hand-built B-rep, or a coordinate-perturbed
    triangulation) reliably reproduces: sewing two faces with mismatched
    boundaries heals the mismatch geometrically instead of leaving it raw, and
    hand-editing a triangulation node is silently discarded the next time
    BRepMesh_IncrementalMesh runs (which _mesh_defects_exact always does, at
    the top of every call). So this drives the exact code path the real bug
    hit — the comparison against BRep_Tool.Pnt_s inside the vertex-merge guard
    — by making that one lookup lie about a single named vertex's true
    position, on an otherwise completely clean, valid Box. Confirmed against
    the real field case (#397 CHANGELOG) this fires correctly: same shape of
    result, ok=True, only the targeted vertex counted."""
    from build123d import Box
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

    from build123d_mcp.tools.validate import _mesh_defects_exact

    box = Box(10, 10, 10)
    target = box.vertices()[0].wrapped
    orig_pnt_s = BRep_Tool.Pnt_s

    def _lying_pnt_s(v):
        p = orig_pnt_s(v)
        if v.IsSame(target):
            return gp_Pnt(p.X() + 1.0, p.Y() + 1.0, p.Z() + 1.0)
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))
    result = _mesh_defects_exact(box)
    assert result.ok
    assert result.vertex_deflection_defects == 1
    assert result.nonmanifold_edges == 0
    assert result.untriangulated_faces == 0
    assert result.refined_untriangulated_faces == 0
    assert result.nonmanifold_vertices == 0


def test_mesh_defects_exact_open_ladder_catches_vertex_deflection_too(monkeypatch):
    """The base-deflection vertex-merge guard above and the open-edge ladder's
    OWN, separate vertex-merge (_open_pass, run only when the shape is genuinely
    open at the base rung and must escalate to a finer one) are two independent
    union-find passes over two independently-built node arrays — fixing one does
    not fix the other. This isolates the ladder's copy specifically: a Shell
    missing one face is genuinely open (forces escalation to deflection/4), and
    the lied-about vertex is displaced by 0.01mm — under the base deflection
    (~0.0173mm for this 10mm box, so the ALREADY-guarded base pass does not flag
    it) but over the first refined rung (~0.00433mm), so only the ladder's own
    guard can catch it. If _open_pass's guard were missing (as it originally
    was), this would come back vdefl=0 despite a real off-vertex node — the
    open-edge count alone (open=4, the missing face's rim) would not surface it
    either, since the union always proceeds regardless of the guard."""
    from build123d import Box, Shell
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

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
    result = _mesh_defects_exact(shell)
    assert result.ok
    assert result.open_edges == 4  # the missing face's 4 rim edges — unaffected by the guard
    assert result.refined_untriangulated_faces == 0
    assert result.vertex_deflection_defects == 1  # only reachable via the ladder guard


def test_mesh_defects_exact_counts_multiple_ladder_only_vertices(monkeypatch):
    """A regression guard on an adversarial-review finding: the ladder's own
    vertex-deflection finding used to be folded into the base pass's count via
    `max(vertex_defl_defects, 1) if _ov_vdefl else vertex_defl_defects` — a
    boolean OR laundered into "at least 1", which silently capped the reported
    total at 1 no matter how many DISTINCT vertices the ladder alone found.
    This displaces TWO different vertices (both under the base deflection, both
    only visible at the ladder's refined rung, same setup as the test above) and
    requires the count to actually be 2, not 1 — proving the fix now unions by
    each vertex's own world-space position rather than OR-ing a flag."""
    from build123d import Box, Shell
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

    from build123d_mcp.tools.validate import _mesh_defects_exact

    shell = Shell(Box(10, 10, 10).faces()[:5])
    verts = shell.vertices()
    target1 = verts[0].wrapped
    target2 = verts[1].wrapped
    orig_pnt_s = BRep_Tool.Pnt_s

    def _lying_pnt_s(v):
        p = orig_pnt_s(v)
        if v.IsSame(target1) or v.IsSame(target2):
            return gp_Pnt(p.X() + 0.01, p.Y(), p.Z())
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))
    result = _mesh_defects_exact(shell)
    assert result.ok
    assert result.open_edges == 4
    assert result.refined_untriangulated_faces == 0
    assert result.vertex_deflection_defects == 2  # both distinct vertices counted


def test_mesh_defects_exact_no_double_count_when_base_and_ladder_agree(monkeypatch):
    """The flip side of the regression above: when the SAME vertex is large
    enough to be caught by both the base pass and the (forced-to-run) ladder,
    the union-by-world-position must dedupe it to 1, not double-count it to 2."""
    from build123d import Box, Shell
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt

    from build123d_mcp.tools.validate import _mesh_defects_exact

    shell = Shell(Box(10, 10, 10).faces()[:5])
    target = shell.vertices()[0].wrapped
    orig_pnt_s = BRep_Tool.Pnt_s

    def _lying_pnt_s(v):
        p = orig_pnt_s(v)
        if v.IsSame(target):
            return gp_Pnt(p.X() + 1.0, p.Y(), p.Z())  # well over the base deflection too
        return p

    monkeypatch.setattr(BRep_Tool, "Pnt_s", staticmethod(_lying_pnt_s))
    result = _mesh_defects_exact(shell)
    assert result.ok
    assert result.open_edges == 4
    assert result.refined_untriangulated_faces == 0
    assert result.vertex_deflection_defects == 1  # same vertex found twice, deduped


# --- out-of-process mesh gate (export retry for parts too large to mesh in-budget) ---


def test_mesh_gate_subprocess_valid_step(tmp_path):
    from build123d import Box, export_step

    from build123d_mcp.tools.validate import _run_mesh_gate_subprocess

    p = tmp_path / "box.step"
    export_step(Box(10, 10, 10), str(p))
    # A clean solid: 0 nm-edge, 0 open, 0 base/refined untriangulated, 0
    # nm-vertex, 0 vertex-deflection defects, ok=True.
    result = _run_mesh_gate_subprocess(str(p), timeout=120)
    assert result is not None
    assert result.ok
    assert result.refined_verified
    assert result.nonmanifold_edges == 0
    assert result.open_edges == 0
    assert result.untriangulated_faces == 0
    assert result.refined_untriangulated_faces == 0
    assert result.nonmanifold_vertices == 0
    assert result.vertex_deflection_defects == 0


def test_mesh_gate_subprocess_timeout_returns_none(tmp_path):
    from build123d import Box, export_step

    from build123d_mcp.tools.validate import _run_mesh_gate_subprocess

    p = tmp_path / "box.step"
    export_step(Box(10, 10, 10), str(p))
    # A hard timeout kills the subprocess and returns None (undetermined),
    # so the caller keeps its safe in-process verdict — never invents one.
    assert _run_mesh_gate_subprocess(str(p), timeout=0.001) is None
