import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.recognise_features import recognise_features


@pytest.fixture
def featured_session():
    session = Session()
    session.execute(
        """
from build123d import *
with BuildPart() as bp:
    Box(60, 40, 10)
    with Locations((22, 12, 10), (-22, 12, 10), (22, -12, 10), (-22, -12, 10)):
        Hole(2.25)
show(bp.part, 'plate')
"""
    )
    return session


def test_summary_is_compact_and_second_family_query_reuses_run(featured_session):
    summary = json.loads(recognise_features(featured_session, "plate"))
    assert summary["cached"] is False
    assert summary["inventory"]["holes"] == 4
    assert summary["targetable"]["holes"] == 4
    assert summary["features"] == []

    holes = json.loads(recognise_features(featured_session, "plate", families="hole"))
    assert holes["cached"] is True
    assert holes["run"] == summary["run"]
    assert holes["matched"] == holes["returned"] == 4
    assert all(feature["family"] == "holes" for feature in holes["features"])


def test_feature_handle_resolves_exact_constituent_and_defining_faces(featured_session):
    report = json.loads(
        recognise_features(featured_session, "plate", families="holes", include_faces=True)
    )
    feature = report["features"][0]
    assert feature["record"]["diameter"] == pytest.approx(4.5)
    assert feature["constituent_face_count"] == 1
    assert feature["constituent_faces"][0]["geom_type"] == "CYLINDER"
    assert feature["constituent_faces"][0]["selector"].startswith(".faces()[")

    resolve_faces = featured_session.namespace["recognition_faces"]
    constituent = resolve_faces(feature["ref"])
    defining = resolve_faces(feature["ref"], role="defining")
    assert len(constituent) == len(defining) == 1
    assert constituent[0].wrapped.IsSame(defining[0].wrapped)


def test_feature_handle_refuses_replaced_source_geometry(featured_session):
    report = json.loads(recognise_features(featured_session, "plate", families="holes"))
    reference = report["features"][0]["ref"]
    featured_session.execute("show(Box(5, 5, 5), 'plate')")

    with pytest.raises(ValueError, match="Stale recognition reference"):
        featured_session.namespace["recognition_faces"](reference)


def test_part_frame_returns_frame_and_caller_faces(featured_session):
    report = json.loads(
        recognise_features(
            featured_session,
            "plate",
            families="holes",
            coordinate_frame="part",
            include_faces=True,
        )
    )
    assert report["frame"]["gauge"] in {"full", "orthogonal", "axial"}
    assert len(report["frame"]["origin"]) == 3
    assert report["features"][0]["constituent_faces"][0]["geom_type"] == "CYLINDER"


def test_unknown_family_and_invalid_limits_are_explicit(featured_session):
    unknown = json.loads(recognise_features(featured_session, "plate", families="gears"))
    assert "Unknown targetable families" in unknown["error"]
    assert "holes" in unknown["targetable_families"]

    bad_limit = json.loads(recognise_features(featured_session, "plate", max_features=101))
    assert "between 1 and 100" in bad_limit["error"]

    bad_frame = json.loads(
        recognise_features(featured_session, "plate", coordinate_frame="drawing")
    )
    assert "caller" in bad_frame["error"] and "part" in bad_frame["error"]


def test_reset_expires_handles_and_clears_cache(featured_session):
    report = json.loads(recognise_features(featured_session, "plate", families="holes"))
    reference = report["features"][0]["ref"]
    featured_session.reset()

    with pytest.raises(ValueError, match="Unknown or expired"):
        featured_session.namespace["recognition_faces"](reference)
    assert featured_session._recognition_runs == {}
    assert featured_session._recognition_targets == {}
