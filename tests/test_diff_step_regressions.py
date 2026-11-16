"""Caller-frame face accounting must survive upstream rigid alignment."""

import json

import pytest
from build123d import Axis, Box, Cylinder, Pos, Rot, fillet

from build123d_mcp.session import Session
from build123d_mcp.tools._recognition import resolve_analysis
from build123d_mcp.tools.diff_step import diff_step


def curved_case(case):
    if case == "translated":
        old = Box(40, 30, 10) - Pos(-8, 0, 0) * Cylinder(2, 12)
        old -= Pos(8, 0, 0) * Cylinder(3, 12)
        return old, Pos(3, 0, 0) * old, (2, 2)
    if case == "rotated":
        old = Box(40, 30, 10) - Pos(6, 3, 0) * Cylinder(2, 12)
        return old, Rot(0, 0, 90) * old, (1, 1)
    # A reoriented rectangular part exercises the ambiguous fillet match from
    # the review. An axis-aligned resize has uniquely adapted fillets instead.
    box = Box(20, 30, 10)
    return (
        fillet(box.edges().filter_by(Axis.Z), 2),
        Rot(0, 0, 90) * fillet(box.edges().filter_by(Axis.Z), 3),
        (0, 4),
    )


def assert_all_faces_accounted(session, report):
    # These three edits change every caller-frame analytic signature.
    assert report["unchanged_faces"] == 0
    for side, unclaimed_key in (("old", "removed"), ("new", "added")):
        analysis = resolve_analysis(session, side)
        planar = {row[side]["id"] for row in report["planar_deltas"]}
        changes = report["unclaimed_face_changes"]
        unclaimed = {row["id"] for row in changes[unclaimed_key]}
        unclaimed.update(row[side]["id"] for row in changes["modified"])
        feature_ids = {f.get(side) for f in report["features"]}
        claimed = {
            index
            for feature in analysis.document["features"]
            if analysis.bind_id(feature["id"]) in feature_ids
            for index in feature["faces"]
        }
        # Adjoining host boundaries belong to the explaining feature, not plane motion.
        claimed.update(
            row["face_id" if side == "old" else "new_face_id"]
            for row in report["feature_boundary_changes"]
        )
        assert not (planar & unclaimed or planar & claimed or unclaimed & claimed)
        assert planar | unclaimed | claimed == {f["id"] for f in analysis.document["faces"]}
        assert (
            report["unchanged_faces"]
            + report["planar_deltas_count"]
            + changes[unclaimed_key + "_count"]
            + changes["modified_count"]
            + len(claimed)
            == report["measures"][side]["faces"]
        )


@pytest.mark.parametrize("case", ["translated", "rotated", "fillet_resize"])
def test_curved_faces_preserve_part4_accounting(case):
    old, new, expected = curved_case(case)
    session = Session()
    session.objects.update(old=old, new=new)
    report = json.loads(diff_step(session, "old", "new"))
    assert "error" not in report, report
    changes = report["unclaimed_face_changes"]
    actual = tuple(
        sum(face["kind"] != "plane" for face in changes[key]) for key in ("removed", "added")
    )
    assert actual == expected
    assert_all_faces_accounted(session, report)


def test_identical_content_counts_all_faces_independently_of_upstream(monkeypatch):
    from specify_core import api

    original = api.diff

    def undercount(*args, **kwargs):
        result = original(*args, **kwargs)
        result["report"]["unchanged_faces"] = 0
        result["report"]["other_faces"] = []
        return result

    monkeypatch.setattr(api, "diff", undercount)
    session = Session()
    session.objects["part"] = Box(30, 30, 10) - Cylinder(3, 12)
    report = json.loads(diff_step(session, "part", "part"))
    assert report["unchanged_faces"] == report["measures"]["old"]["faces"] == 7
    assert report["features"] == []
    assert report["planar_deltas_count"] == report["feature_boundary_changes_count"] == 0
    changes = report["unclaimed_face_changes"]
    assert changes["removed_count"] == changes["added_count"] == changes["modified_count"] == 0
