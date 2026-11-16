"""Multi-solid inspection keeps the whole engine part and its placement."""

import importlib
import json

import pytest
from build123d import Box, Compound, Cylinder, Pos, Rot, export_step
from OCP.TopAbs import TopAbs_COMPOUND
from specify_core import api

from build123d_mcp.session import Session
from build123d_mcp.tools._recognition import resolve_analysis
from build123d_mcp.tools.diff_step import diff_step
from build123d_mcp.tools.inspect_features import inspect_features


def compound(radius, arrangement):
    shape = Compound([Box(20, 20, 10) - Cylinder(radius, 12), Pos(30, 0, 0) * Box(10, 10, 10)])
    if arrangement == "nested_assembly":
        leaf = Pos(3, 4, 5) * shape
        inner = Compound(children=[leaf])
        inner.location = Pos(7, 8, 9) * Rot(0, 0, 90)
        shape = Compound(children=[inner])
        shape.location = Pos(11, 12, 13) * Rot(10, 0, 0)
    elif arrangement == "translated_compound":
        shape = Pos(10, 20, 30) * Rot(0, 0, 90) * shape
    return shape


@pytest.mark.parametrize("target_kind", ["session", "step"])
@pytest.mark.parametrize("arrangement", ["compound", "nested_assembly", "translated_compound"])
def test_inspect_and_diff_multi_solid_part(tmp_path, target_kind, arrangement):
    session = Session()
    targets = []
    for name, radius in (("old", 4), ("new", 6)):
        shape = compound(radius, arrangement)
        path = tmp_path / f"{name}.step"
        export_step(shape, path)
        session.objects[name] = shape
        target = name if target_kind == "session" else str(path)
        targets.append(target)
        summary = json.loads(inspect_features(session, target))
        assert "error" not in summary, summary
        assert summary["measures"]["faces"] == 13
        assert summary["inventory"]["holes"]["count"] == 1
        assert summary["inventory"]["holes"]["diameter"] == [2 * radius, 2 * radius]
        analysis = resolve_analysis(session, target)
        assert len(analysis.shape.solids()) == 2
        box = shape.bounding_box()
        assert summary["measures"]["min"] == pytest.approx(tuple(box.min), abs=1e-5)
        assert summary["measures"]["max"] == pytest.approx(tuple(box.max), abs=1e-5)
        # Compare against the engine's selected part, not an assumed leaf frame.
        expected = api.parts(path)[analysis.loaded.binding.part]["placements"]
        assert len(expected) == 1
        for actual_row, expected_row in zip(analysis.placement, expected[0]):
            assert actual_row == pytest.approx(expected_row, abs=1e-8)
        (hole,) = json.loads(inspect_features(session, target, "holes"))["features"]
        selected = json.loads(inspect_features(session, target, feature_ids=hole["id"]))
        assert "error" not in selected, selected
        assert selected["features"][0]["faces"][0]["radius"] == radius

    report = json.loads(diff_step(session, *targets))
    assert "error" not in report, report
    assert report["measures"]["old"]["faces"] == report["measures"]["new"]["faces"] == 13
    (hole,) = [f for f in report["features"] if f["family"] == "holes"]
    assert hole["class"] == "adapted"
    assert hole["changes"]["sizes.diameter"] == {"old": 8, "new": 12}


@pytest.mark.parametrize("tool", ["inspect_features", "diff_step"])
@pytest.mark.parametrize("exception", [KeyError(TopAbs_COMPOUND), RuntimeError("STEP read failed")])
def test_inspection_errors_include_exception_type_and_message(monkeypatch, tool, exception):
    module = importlib.import_module(f"build123d_mcp.tools.{tool}")

    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(module, "resolve_analysis", fail)
    args = (Session(), "old", "new") if tool == "diff_step" else (Session(), "old")
    error = json.loads(getattr(module, tool)(*args))["error"]
    assert error == f"{type(exception).__name__}: {exception}"
