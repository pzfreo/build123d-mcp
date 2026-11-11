"""quiddity 0.4 adapter: face indices map to live faces only when proven."""

import pytest
from build123d import Box, Cylinder, Pos, Rot

from build123d_mcp.tools._recognition import _type_name, build_evidence


@pytest.fixture
def evidence():
    part = Pos(5, 5, 5) * Rot(10, 20, 30) * (Box(40, 30, 10) - Cylinder(4, 10))
    return build_evidence(part)


def test_every_face_index_resolves_to_the_live_face_at_that_index(evidence):
    for index, face in enumerate(evidence._faces):
        assert evidence.face(index) is face


def test_renumbered_faces_are_refused_not_misattributed(evidence):
    evidence._faces = tuple(reversed(evidence._faces))
    with pytest.raises(ValueError, match="one exact source face"):
        evidence.face(0)


def test_out_of_range_face_index_is_refused(evidence):
    with pytest.raises(ValueError, match="one exact source face"):
        evidence.face(len(evidence._faces))


def test_hole_record_and_association(evidence):
    (hole,) = [f for f in evidence.features if evidence.family(f) == "holes"]
    record = evidence.record(hole)
    assert record.kind == "Hole"
    assert record.to_dict()["diameter"] == pytest.approx(8.0)
    association = evidence.association()
    assert association["faces"]["total"] == len(evidence._faces)
    assert 0 < association["faces"]["associated"] <= association["faces"]["total"]


@pytest.mark.parametrize(
    ("family", "kind"),
    [
        ("holes", "Hole"),
        ("section_recesses", "SectionRecess"),
        ("polygonal_bosses", "PolygonalBoss"),
        ("step_levels", "StepLevel"),
        ("countersinks", "Countersink"),
    ],
)
def test_record_type_names(family, kind):
    assert _type_name(family) == kind
