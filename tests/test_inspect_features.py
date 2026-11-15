"""Stateless STEP inspection: real geometry, caller coordinates and bounded output."""

import json

import pytest
from build123d import Box, Cylinder, Pos, Rot, Vertex, export_step

from build123d_mcp.session import Session
from build123d_mcp.tools._recognition import _CACHE_SIZE, resolve_analysis
from build123d_mcp.tools.inspect_features import inspect_features


@pytest.fixture
def featured_session():
    session = Session()
    session.execute(
        "from build123d import *\n"
        "with BuildPart() as bp:\n"
        "    Box(60,40,10)\n"
        "    with Locations((22,12,10),(-22,12,10),(22,-12,10),(-22,-12,10)):\n"
        "        Hole(2.25)\n"
        "show(bp.part, 'plate')"
    )
    return session


def test_summary_and_family_query_reuse_content(featured_session, monkeypatch):
    from specify_core import api

    calls = []
    for name in ("load", "analyse"):
        original = getattr(api, name)

        def counted(*args, _fn=original, _name=name, **kwargs):
            calls.append(_name)
            return _fn(*args, **kwargs)

        monkeypatch.setattr(api, name, counted)
    summary = json.loads(inspect_features(featured_session, "plate"))
    assert summary["inventory"]["holes"]["count"] == 4
    assert summary["inventory"]["holes"]["diameter"] == [4.5, 4.5]
    holes = json.loads(inspect_features(featured_session, "plate", "hole"))
    assert holes["matched"] == holes["returned"] == 4
    assert all(f["family"] == "holes" and "faces" not in f for f in holes["features"])
    assert calls == ["load", "analyse"]


def test_feature_ids_return_only_selected_analytic_faces(featured_session):
    features = json.loads(inspect_features(featured_session, "plate", "holes"))["features"]
    report = json.loads(inspect_features(featured_session, "plate", feature_ids=features[0]["id"]))
    (feature,) = report["features"]
    (face,) = feature["faces"]
    assert feature["id"] == features[0]["id"]
    assert face["kind"] == "cylinder"
    assert face["radius"] == pytest.approx(2.25)
    assert all(k in face for k in ("centroid", "direction", "origin", "area"))
    assert len(report["features"]) == report["matched"] == 1


def test_stale_id_refused_after_replacing_source(featured_session):
    fid = json.loads(inspect_features(featured_session, "plate", "holes"))["features"][0]["id"]
    featured_session.execute("show(Box(5,5,5), 'plate')")
    assert (
        "stale" in json.loads(inspect_features(featured_session, "plate", feature_ids=fid))["error"]
    )
    assert "recognition_faces" not in featured_session.namespace


def test_records_and_faces_stay_in_caller_frame():
    s = Session()
    transform = Pos(7, 9, 11) * Rot(10, 20, 30)
    s.objects["p"] = transform * (Box(40, 30, 10) - Cylinder(4, 12))
    (hole,) = json.loads(inspect_features(s, "p", "holes"))["features"]
    expected = (transform * Vertex(0, 0, 5)).center()
    assert hole["location"] == pytest.approx(tuple(expected), abs=1e-5)
    assert abs(hole["axis"][2]) < 0.99
    (row,) = json.loads(inspect_features(s, "p", feature_ids=hole["id"]))["features"]
    assert row["faces"][0]["centroid"] == pytest.approx((7, 9, 11), abs=1e-5)


def test_unknown_family_and_invalid_limits_are_explicit(featured_session):
    assert (
        "unknown family"
        in json.loads(inspect_features(featured_session, "plate", "gears"))["error"]
    )
    assert (
        "between 1 and 100"
        in json.loads(inspect_features(featured_session, max_features=101))["error"]
    )
    assert (
        "choose families"
        in json.loads(inspect_features(featured_session, "plate", "holes", "holes:6"))["error"]
    )


def test_reset_clears_cache_without_handles(featured_session):
    inspect_features(featured_session, "plate")
    assert featured_session._step_analysis_cache
    featured_session.reset()
    assert not featured_session._step_analysis_cache
    assert "recognition_faces" not in featured_session.namespace


def test_recess_family_is_explicit_and_does_not_leak_raw_arrays():
    s = Session()
    s.execute(
        "from build123d import *\np=Box(80,40,12)\nfor x in (-20,20):\n    p -= Pos(x,0,6)*Box(14,20,5,align=(Align.CENTER,Align.CENTER,Align.MAX))\nshow(p,'p')"
    )
    report = json.loads(inspect_features(s, "p", "section_recesses"))
    assert "error" not in report
    assert report["matched"] == 2
    assert all("faces" not in f for f in report["features"])
    pockets = json.loads(inspect_features(s, "p", "pockets"))
    assert pockets["matched"] == 2
    assert all(
        f["depth"] == 5 and f["width"] == 14 and f["length"] == 20 for f in pockets["features"]
    )
    faces = json.loads(inspect_features(s, "p", feature_ids=report["features"][0]["id"]))
    assert len(faces["features"][0]["faces"]) >= 4
    assert "unknown family" in json.loads(inspect_features(s, "p", "widgets"))["error"]


def test_invalid_solid_gets_repair_guidance():
    s = Session()
    from build123d import Shell

    s.objects["bad"] = Shell(Box(10, 10, 10).faces()[1:])
    report = json.loads(inspect_features(s, "bad"))
    assert "valid solid" in report["error"]
    assert "build123d://skill/repair" in report["error"]


def test_aggregate_records_are_bounded_and_have_ids(featured_session):
    report = json.loads(inspect_features(featured_session, "plate", "holes,hole_patterns"))
    assert report["matched"] == 5
    pattern = next(f for f in report["features"] if f["family"] == "hole_patterns")
    assert "id" in pattern and "holes" not in pattern and pattern["holes_count"] == 4


def test_many_features_inventory_under_two_kb_and_exact_truncation():
    s = Session()
    s.execute(
        "from build123d import *\nwith BuildPart() as bp:\n    Box(240,160,10)\n    with GridLocations(16,16,12,8):\n        Hole(3)\nshow(bp.part,'grid')"
    )
    summary = inspect_features(s, "grid")
    assert len(summary.encode()) < 2000
    assert json.loads(summary)["inventory"]["holes"]["count"] == 96
    report = json.loads(inspect_features(s, "grid", "holes", max_features=3))
    assert report["matched"] == 96 and report["returned"] == 3 and report["truncated"]
    pattern = json.loads(inspect_features(s, "grid", "hole_patterns"))
    assert all("holes" not in f for f in pattern["features"])
    assert len(json.dumps(pattern)) < 20_000


def test_path_and_object_have_same_inventory(featured_session, tmp_path):
    path = tmp_path / "plate.step"
    export_step(featured_session.objects["plate"], path)
    assert json.loads(inspect_features(featured_session, str(path))) == json.loads(
        inspect_features(featured_session, "plate")
    )


def test_cache_is_bounded_and_temp_paths_cleaned(featured_session):
    for i in range(_CACHE_SIZE + 1):
        featured_session.objects["p"] = Box(10 + i, 8, 6)
        entry = resolve_analysis(featured_session, "p")
        assert not entry.loaded.path.exists()
    assert len(featured_session._step_analysis_cache) == _CACHE_SIZE


def test_path_security_size_and_extension(featured_session, monkeypatch, tmp_path):
    assert (
        "outside the allowed"
        in json.loads(inspect_features(featured_session, "/etc/passwd"))["error"]
    )
    path = tmp_path / "p.step"
    export_step(Box(10, 10, 10), path)
    monkeypatch.setenv("BUILD123D_MAX_CAD_BYTES", "10")
    assert "exceeding" in json.loads(inspect_features(featured_session, str(path)))["error"]
    assert "path" in json.loads(inspect_features(featured_session, "missing.stl"))["error"]
