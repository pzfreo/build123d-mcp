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
    assert report["mesh_open_edges"] == 0
    assert report["untriangulated_faces"] == 0
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
    assert _mesh_defects_exact(two)[0] == 0  # exact (topology) is correct


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
        lambda shape, exact=False: {
            "passes_gate": False,
            "reasons": ["injected non-manifold solid"],
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
