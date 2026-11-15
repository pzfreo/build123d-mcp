"""Generic feature selection and conservative transactional hole edits."""

import json
import math

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.edit_features import edit_feature
from build123d_mcp.tools.inspect_features import inspect_features
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
    return json.loads(inspect_features(session, "plate", "holes"))["features"]


def test_inspection_exposes_literal_axis_side_and_size(drilled_plate):
    holes = _holes(drilled_plate)
    assert len(holes) == 2
    assert all(abs(h["axis"][2]) == 1 and h["diameter"] == 6 for h in holes)
    assert sorted(h["location"][0] for h in holes) == [-15, 15]


def test_rotated_caller_axis_is_preserved(drilled_plate):
    drilled_plate.execute("show(Rot(90,0,0)*plate, 'plate')")
    assert all(abs(h["axis"][1]) == 1 for h in _holes(drilled_plate))


def test_polygonal_boss_inspection_has_axis_and_dimensions():
    session = Session()
    session.execute(
        "from build123d import *\npart=Box(40,40,8)+Pos(0,0,4)*extrude(RegularPolygon(8,6),amount=7)\nshow(part,'part')"
    )
    report = json.loads(inspect_features(session, "part", "polygonal_bosses"))
    assert report["matched"] == 1
    boss = report["features"][0]
    assert boss["side_count"] == 6 and boss["across_flats"] > 0
    assert abs(boss["axis"][2]) == 1
    assert boss["height"] == 7


def test_hole_ids_and_faces_identify_interfaces(drilled_plate):
    holes = _holes(drilled_plate)
    report = json.loads(
        inspect_features(drilled_plate, "plate", feature_ids=",".join(h["id"] for h in holes))
    )
    assert report["matched"] == 2
    assert all(f["faces"][0]["kind"] == "cylinder" for f in report["features"])


def test_coplanar_split_does_not_duplicate_hole():
    session = Session()
    session.execute(
        "from build123d import *\np=Box(60,40,10)-Pos(0,0,5)*Box(2,42,2)-Pos(15,0,0)*Cylinder(3,12)\nshow(p,'p')"
    )
    report = json.loads(inspect_features(session, "p", "holes"))
    assert report["matched"] == 1
    assert report["features"][0]["location"][0] == 15


@pytest.mark.parametrize("diameter", [4.0, 8.0])
def test_edit_resizes_one_hole_and_proves_annular_change(drilled_plate, diameter):
    holes = _holes(drilled_plate)
    target = holes[0]["id"]
    protected = holes[1]["id"]
    before = drilled_plate.objects["plate"]
    report = json.loads(
        edit_feature(
            drilled_plate,
            target,
            diameter,
            object_name="plate",
            protected_refs=json.dumps([protected]),
        )
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
    assert sorted(h["diameter"] for h in after) == [min(6, diameter), max(6, diameter)]


def test_failed_edit_does_not_replace_source(drilled_plate):
    target = _holes(drilled_plate)[0]["id"]
    before = drilled_plate.objects["plate"]
    report = json.loads(edit_feature(drilled_plate, target, 8, protected_refs=json.dumps([target])))
    assert report["committed"] is False
    assert "protected" in report["error"]
    assert drilled_plate.objects["plate"] is before


def test_edit_rejects_hole_that_breaks_outer_wall(drilled_plate):
    target = next(h["id"] for h in _holes(drilled_plate) if h["location"][0] > 0)
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
    target = json.loads(inspect_features(session, "p", "holes"))["features"][0]["id"]
    before = session.objects["p"]
    report = json.loads(edit_feature(session, target, 12))
    assert report["committed"] is False
    assert "dependent" in report["error"]
    assert session.objects["p"] is before


def test_edit_tools_use_worker_owned_geometry():
    worker = WorkerSession(exec_timeout=30)
    try:
        worker.execute("from build123d import *\nshow(Box(40, 40, 10) - Cylinder(3, 12), 'plate')")
        candidates = json.loads(worker.inspect_features("plate", "holes"))
        assert candidates["matched"] == 1
        result = json.loads(worker.edit_feature(candidates["features"][0]["id"], 8))
        assert result["committed"] is True
        assert result["measured"] == result["predicted"]
    finally:
        worker._kill_worker()


def test_stale_id_is_refused_without_mutation(drilled_plate):
    target = _holes(drilled_plate)[0]["id"]
    drilled_plate.execute("show(Box(10,10,10), 'plate')")
    source = drilled_plate.objects["plate"]
    report = json.loads(edit_feature(drilled_plate, target, 8, object_name="plate"))
    assert not report["committed"] and "stale" in report["error"]
    assert drilled_plate.objects["plate"] is source


def test_face_mapping_mismatch_is_refused_even_for_enlargement(drilled_plate, monkeypatch):
    from build123d_mcp.tools._recognition import Evidence

    target = _holes(drilled_plate)[0]["id"]
    original = Evidence.face

    def reordered(self, index):
        self._faces = tuple(reversed(self._faces))
        return original(self, index)

    monkeypatch.setattr(Evidence, "face", reordered)
    source = drilled_plate.objects["plate"]
    report = json.loads(edit_feature(drilled_plate, target, 8, object_name="plate"))
    assert not report["committed"] and "mapping mismatch" in report["error"]
    assert drilled_plate.objects["plate"] is source
