"""Generic feature selection and conservative transactional hole edits."""

import json
import math

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.edit_features import edit_feature, find_candidates, interface_features
from build123d_mcp.worker import WorkerSession


@pytest.fixture
def drilled_plate():
    session = Session()
    session.execute(
        "from build123d import *\n"
        "plate = Box(60, 40, 10)\n"
        "for x in (-15, 15):\n"
        "    plate -= Pos(x, 0, 0) * Cylinder(3, 12)\n"
        "show(plate, 'plate')"
    )
    return session


def _holes(session):
    return json.loads(find_candidates(session, "hole", object_name="plate"))["candidates"]


def test_candidates_check_both_axis_readings_and_stated_value(drilled_plate):
    literal = json.loads(find_candidates(drilled_plate, "hole", '{"axis":"Z"}', 6, "plate"))
    assert literal["count"] == 2
    assert len(literal["literal_matches"]) == 2
    assert literal["yz_swapped_matches"] == []

    swapped = json.loads(find_candidates(drilled_plate, "hole", '{"axis":"Y"}', 6, "plate"))
    assert swapped["literal_matches"] == []
    assert len(swapped["yz_swapped_matches"]) == 2

    wrong_value = json.loads(find_candidates(drilled_plate, "hole", "{}", 9, "plate"))
    assert wrong_value["stated_value_unmatched"] is True
    assert wrong_value["literal_matches"] == []

    positive_side = json.loads(
        find_candidates(drilled_plate, "hole", '{"side":"+X"}', object_name="plate")
    )
    assert len(positive_side["literal_matches"]) == 1
    assert len(positive_side["yz_swapped_matches"]) == 1


def test_polygonal_boss_candidates_include_axis_and_across_flats():
    session = Session()
    session.execute(
        "from build123d import *\n"
        "part = Box(40, 40, 8) + Pos(0, 0, 4) * extrude(RegularPolygon(8, 6), amount=7)\n"
        "show(part, 'part')"
    )
    report = json.loads(
        find_candidates(session, "polygonal_boss", '{"axis":"Z"}', object_name="part")
    )
    assert report["count"] == 1
    candidate = report["candidates"][0]
    assert candidate["record"]["side_count"] == 6
    assert candidate["measured_field"] == "across_flats"
    assert candidate["literal_axis_matches"] is True
    assert candidate["yz_swapped_axis_matches"] is False
    assert json.loads(find_candidates(session, "boss", object_name="part"))["count"] >= 1


def test_interface_suggests_planar_openings_without_claiming_intent(drilled_plate):
    report = json.loads(interface_features(drilled_plate, "plate"))
    assert len(report["protected_hole_refs"]) == 2
    assert any(len(face["hole_refs"]) == 2 for face in report["mounting_face_candidates"])
    assert "caller must choose" in report["note"]


def test_interface_does_not_attach_hole_to_other_coplanar_face():
    session = Session()
    session.execute(
        "from build123d import *\n"
        "p = Box(60, 40, 10) - Pos(0, 0, 5) * Box(2, 42, 2)\n"
        "p -= Pos(15, 0, 0) * Cylinder(3, 12)\n"
        "show(p, 'p')"
    )
    report = json.loads(interface_features(session, "p"))
    assert len(report["mounting_face_candidates"]) == 1
    assert report["mounting_face_candidates"][0]["area"] < 1200


@pytest.mark.parametrize("diameter", [4.0, 8.0])
def test_edit_resizes_one_hole_and_proves_annular_change(drilled_plate, diameter):
    holes = _holes(drilled_plate)
    target = holes[0]["ref"]
    protected = holes[1]["ref"]
    before = drilled_plate.objects["plate"]
    report = json.loads(
        edit_feature(drilled_plate, target, diameter, protected_refs=json.dumps([protected]))
    )
    assert "error" not in report
    assert report["other_holes_unchanged"] is True
    assert report["outer_envelope_unchanged"] is True
    expected = math.pi * abs((diameter / 2) ** 2 - 3**2) * 10
    assert sum(report["predicted"].values()) == pytest.approx(expected)
    assert report["measured"] == report["predicted"]
    assert report["measured_region_bbox"] == pytest.approx(
        report["predicted_region_bbox"], abs=0.02
    )
    assert drilled_plate.objects["plate"] is not before
    after = _holes(drilled_plate)
    assert sorted(h["measured_value"] for h in after) == [min(6, diameter), max(6, diameter)]


def test_failed_edit_does_not_replace_source(drilled_plate):
    target = _holes(drilled_plate)[0]["ref"]
    before = drilled_plate.objects["plate"]
    report = json.loads(edit_feature(drilled_plate, target, 8, protected_refs=json.dumps([target])))
    assert report["committed"] is False
    assert "protected" in report["error"]
    assert drilled_plate.objects["plate"] is before


def test_edit_rejects_hole_that_breaks_outer_wall(drilled_plate):
    target = next(h["ref"] for h in _holes(drilled_plate) if h["record"]["location"][0] > 0)
    before = drilled_plate.objects["plate"]
    report = json.loads(edit_feature(drilled_plate, target, 50))
    assert report["committed"] is False
    assert "mismatch" in report["error"]
    assert drilled_plate.objects["plate"] is before


def test_dependent_hole_geometry_is_refused():
    session = Session()
    session.execute(
        "from build123d import *\n"
        "p = Box(60, 60, 20) - Cylinder(5, 20) - Pos(0, 0, 7) * Cylinder(9, 6)\n"
        "show(p, 'p')"
    )
    target = json.loads(find_candidates(session, "hole", object_name="p"))["candidates"][0]["ref"]
    assert json.loads(interface_features(session, "p"))["protected_hole_refs"] == [target]
    before = session.objects["p"]
    report = json.loads(edit_feature(session, target, 12))
    assert report["committed"] is False
    assert "dependent" in report["error"]
    assert session.objects["p"] is before


def test_edit_tools_use_worker_owned_geometry():
    worker = WorkerSession(exec_timeout=30)
    try:
        worker.execute("from build123d import *\nshow(Box(40, 40, 10) - Cylinder(3, 12), 'plate')")
        candidates = json.loads(worker.find_candidates("hole", object_name="plate"))
        assert candidates["count"] == 1
        assert json.loads(worker.interface_features("plate"))["protected_hole_refs"]
        result = json.loads(worker.edit_feature(candidates["candidates"][0]["ref"], 8))
        assert result["committed"] is True
        assert result["measured"] == result["predicted"]
    finally:
        worker._kill_worker()
