"""specify-core adapter: face indices map to live faces only when proven."""

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


def test_hole_record_and_descriptors(evidence):
    (hole,) = [f for f in evidence.features if evidence.family(f) == "holes"]
    record = evidence.record(hole)
    assert record.kind == "Hole"
    assert record.to_dict()["diameter"] == pytest.approx(8.0)
    for index in evidence.constituent_faces(hole):
        descriptor = evidence._descriptors[index]
        assert descriptor["kind"] == "cylinder"
        assert descriptor["radius"] == pytest.approx(4)
        evidence.face(index)


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


def test_byte_load_is_path_equivalent_and_cached_without_temporary_files(monkeypatch, tmp_path):
    from collections import OrderedDict

    from build123d import export_step
    from specify_core import api

    from build123d_mcp.tools._recognition import analysis_from_bytes

    path = tmp_path / "part.step"
    export_step(Pos(5, 6, 7) * Rot(10, 20, 30) * (Box(40, 30, 10) - Cylinder(4, 12)), path)
    data = path.read_bytes()
    expected = api.analyse(api.load(path))
    original = api.load
    calls = []

    def load(source, **kwargs):
        assert source == data
        assert kwargs == {"name": "model.step"}
        calls.append(source)
        return original(source, **kwargs)

    def no_temp(*args, **kwargs):
        pytest.fail("byte analysis must not create temporary files")

    monkeypatch.setattr(api, "load", load)
    monkeypatch.setattr("tempfile.TemporaryDirectory", no_temp)
    cache = OrderedDict()
    actual = analysis_from_bytes(data, cache)
    assert actual.loaded.binding.to_dict() == expected["binding"]
    assert api.analyse(actual.loaded) == expected
    assert analysis_from_bytes(data, cache) is actual
    assert len(calls) == 1
