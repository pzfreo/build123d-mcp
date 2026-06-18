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
    assert report["is_manifold"] is True
    assert report["brep_valid"] is True
    assert report["reasons"] == []


def test_2d_sketch_fails(session):
    execute_code(session, "show(Rectangle(5, 5), 'sk')")
    out = validate(session, "sk")
    assert out.startswith("Validity gate: FAIL")
    report = json.loads(out.split("\n", 1)[1])
    assert report["passes_gate"] is False
    assert report["n_solids"] == 0
    assert any("solid" in r for r in report["reasons"])


def test_open_shell_fails(session):
    # Five of a box's six faces — an open (non-watertight) shell.
    execute_code(session, "b = Box(10, 10, 10)\nshow(Shell(b.faces()[:5]), 'open')")
    report = _gate_report(session.objects["open"])
    assert report["passes_gate"] is False
    assert report["is_manifold"] is False


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
        lambda shape: {"passes_gate": False, "reasons": ["injected non-manifold solid"]},
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
    assert "VALIDITY GATE FAIL" not in out
