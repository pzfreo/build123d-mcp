"""validate()'s fast-fallback mesh check is blind to open edges — #381.

When the exact topology-stitch overran its in-loop budget on a large shape, the gate
fell back to the fast coordinate-weld, which only counts non-manifold edges and pins
``mesh_open_edges`` to 0. So a large shape with real open edges (an unclosed
tessellated boundary) used to report a silent, unwarned PASS the whole session; only
export() — which runs the exact check out-of-process — ever saw the truth.

Fix (mirrors export's architecture): validate() keeps its B-rep checks in the worker
and isolates ONLY the mesh stitch out-of-process for a large shape (the same
``_run_mesh_gate_subprocess`` export uses), so the exact check catches open edges
in-loop. If that subprocess can't finish, the mesh is reported ``"skipped"`` with a
warning while the in-worker B-rep verdict stands — a subprocess kill can never lose the
verdict. The remaining in-worker fast fallback (small dense parts) now warns too.
"""

import json

from build123d import Box, Shell

import build123d_mcp.tools.validate as v
from build123d_mcp.tools import _bounded
from build123d_mcp.tools.validate import _gate_report, _validate_gate, validate


class _StubSession:
    exec_timeout = 120

    def __init__(self, current=None, objects=None):
        self.current_shape = current
        self.objects = objects or {}


# --- Honesty: the in-worker fast fallback must warn, not silently pass ---------


def test_fast_fallback_warns_that_open_edges_are_unverified(monkeypatch):
    """Forcing the exact stitch over its triangle budget triggers the in-worker fast
    fallback; the report must now warn that the open-edge class was NOT checked, so the
    agent does not trust a verdict the fast check never made."""
    monkeypatch.setattr(v, "_EXACT_INLINE_MAX_TRIS", 1)  # any real mesh overruns → fast
    report = _gate_report(Box(10, 10, 10))
    assert report["mesh_check"] == "fast"
    assert report["passes_gate"] is True  # still a PASS — the warning is the point
    warned = " ".join(report["warnings"]).lower()
    assert "open" in warned and "not verified" in warned and "export()" in warned


def test_exact_pass_does_not_carry_the_unverified_warning():
    """A small part gets the exact check, so it must NOT be tagged 'unverified' — the
    warning is reserved for the genuinely-degraded fast/skipped paths (no crying wolf)."""
    report = _gate_report(Box(10, 10, 10))
    assert report["mesh_check"] == "exact"
    assert not any("not verified" in w.lower() for w in report["warnings"])


# --- Accuracy: the exact check actually detects mesh open edges ----------------


def test_exact_check_detects_real_mesh_open_edges():
    """The load-bearing detection: on a genuinely open tessellated boundary the exact
    mesh check reports mesh_open_edges > 0 — the exact value the blind fast fallback
    pins to 0. (A 5-faced box shell: a real unclosed boundary.)"""
    shell = Shell(Box(10, 10, 10).faces()[:5])
    report = _gate_report(shell, exact=True)
    assert report["mesh_open_edges"] > 0
    assert report["passes_gate"] is False


# --- Architecture: a large shape isolates ONLY the mesh; B-rep stays in-worker -


def test_large_shape_runs_mesh_check_out_of_process(monkeypatch):
    """A large shape's mesh stitch runs in the export-style subprocess (exact), while
    the B-rep checks run in the worker — recorded as mesh_check == 'exact-subprocess'."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)  # treat any shape as large
    calls = {"n": 0}

    def _fake_subproc(step_path, timeout):
        calls["n"] += 1
        return (0, 0, 0, 0, 0, True)  # clean mesh, determined

    monkeypatch.setattr(v, "_run_mesh_gate_subprocess", _fake_subproc)
    out = validate(_StubSession(current=Box(10, 10, 10)))
    assert out.startswith("Validity gate: PASS")
    assert calls["n"] == 1  # the mesh check ran out-of-process
    assert json.loads(out.split("\n", 1)[1])["mesh_check"] == "exact-subprocess"


def test_small_shape_keeps_mesh_check_in_worker(monkeypatch):
    """A small shape must NOT spawn the mesh subprocess — a STEP round-trip would
    dominate; it runs the exact check in-worker."""

    def _boom(*a, **k):
        raise AssertionError("small shape must not spawn the mesh subprocess")

    monkeypatch.setattr(v, "_run_mesh_gate_subprocess", _boom)
    out = validate(_StubSession(current=Box(10, 10, 10)))  # 6 faces < _FACE_GATE
    assert out.startswith("Validity gate: PASS")
    assert json.loads(out.split("\n", 1)[1])["mesh_check"] == "exact"


# --- Graceful degradation: a mesh-gate failure never loses the B-rep verdict ---


def test_mesh_gate_timeout_degrades_to_skipped_keeping_brep(monkeypatch):
    """When the out-of-process mesh gate can't finish (returns None), the mesh is
    reported 'skipped' + warning, but the in-worker B-rep verdict stands — the whole
    point of keeping B-rep out of the killable subprocess."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    monkeypatch.setattr(v, "_run_mesh_gate_subprocess", lambda *a, **k: None)  # timed out
    report = _validate_gate(_StubSession(), Box(10, 10, 10))
    assert report["mesh_check"] == "skipped"
    assert report["passes_gate"] is True  # B-rep verdict survives the mesh-gate failure
    assert any("not verified" in w.lower() for w in report["warnings"])


def test_unserialisable_shape_skips_mesh_not_crash(monkeypatch):
    """If the shape can't be written to STEP for the mesh subprocess, the mesh is
    skipped (not a crash) and the B-rep verdict still stands."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)

    def _boom(shape, path):
        raise RuntimeError("cannot serialise")

    monkeypatch.setattr("build123d_mcp.tools.export._write_step", _boom)
    report = _validate_gate(_StubSession(), Box(10, 10, 10))
    assert report["mesh_check"] == "skipped"
    assert report["passes_gate"] is True


# --- End-to-end regression through the REAL subprocess ------------------------


def test_out_of_process_validate_end_to_end(monkeypatch):
    """The whole isolated path — temp STEP → real _gate_subprocess → exact mesh — still
    returns a correct verdict for a clean solid (mesh_check reflects the exact gate)."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    out = validate(_StubSession(current=Box(8, 8, 8)))
    assert out.startswith("Validity gate: PASS")
    assert json.loads(out.split("\n", 1)[1])["mesh_check"] == "exact-subprocess"


def test_isolated_ceiling_covers_a_shape_above_the_old_inline_cap():
    """Regression for the ceiling itself (#381 follow-up): a real ~90k-triangle part —
    comfortably above the in-worker export ceiling (80k) but below the isolated one
    (300k) — must run the EXACT check out-of-process, not get skipped. A grid of holes
    reliably produces a high triangle count from modest geometry (each hole boundary
    costs tessellation triangles independent of the hole's tiny size)."""
    from build123d import Circle, Compound, Pos, Rectangle, extrude

    from build123d_mcp.tools.validate import _EXACT_EXPORT_MAX_TRIS, _EXACT_ISOLATED_MAX_TRIS

    pitch, r, n = 6.0, 1.5, 29  # measured: ~90,840 triangles, ~13.5% over the old 80k cap
    length = n * pitch + 6
    circles = [
        Pos((i - n / 2) * pitch, (j - n / 2) * pitch) * Circle(r)
        for i in range(n)
        for j in range(n)
    ]
    plate = extrude(Rectangle(length, length) - Compound(children=circles), 5)

    report = json.loads(validate(_StubSession(current=plate)).split("\n", 1)[1])
    assert report["mesh_check"] == "exact-subprocess"  # not "skipped"
    assert report["passes_gate"] is True
    assert _EXACT_ISOLATED_MAX_TRIS > _EXACT_EXPORT_MAX_TRIS  # the isolated path is more generous
