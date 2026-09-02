"""Tests for cross_sections() slice areas.

Internal voids must SUBTRACT from a slice, not add to it (#454). Every
expected area here is hand-computed from the part's dimensions, so a
regression shows up as a wrong number rather than a wrong shape of output.
"""

import json
import math

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.cross_sections import cross_sections
from build123d_mcp.tools.inspect_part import inspect_part


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


# 60 x 40 x 10 plate, R5 vertical fillets, four Ø4.5 through holes on a 44 x 24 grid.
_BRACKET = """
with BuildPart() as bp:
    with BuildSketch():
        RectangleRounded(60, 40, 5)
        with Locations((22, 12), (-22, 12), (22, -12), (-22, -12)):
            Circle(2.25, mode=Mode.SUBTRACT)
    extrude(amount=10)
show(bp.part, 'bracket')
"""

_BRACKET_OUTER = 60 * 40 - 4 * 5**2 * (1 - math.pi / 4)
_BRACKET_HOLES = 4 * math.pi * 2.25**2
_BRACKET_TRUE = _BRACKET_OUTER - _BRACKET_HOLES  # 2314.922565


def _areas(session, **kwargs):
    return [s["area"] for s in json.loads(cross_sections(session, **kwargs))]


def test_through_holes_subtract_from_z_slices(session):
    """Z cuts the holes as internal loops — the case that was inverted."""
    session.execute(_BRACKET)
    for area in _areas(session, object_name="bracket", axis="Z", num_slices=7):
        assert area == pytest.approx(_BRACKET_TRUE, abs=1e-3)


def test_z_slice_area_is_not_the_void_adding_result(session):
    """Guard the specific wrong answer: outer + holes instead of outer - holes."""
    session.execute(_BRACKET)
    wrong = _BRACKET_OUTER + _BRACKET_HOLES  # 2442.157068
    for area in _areas(session, object_name="bracket", axis="Z", num_slices=3):
        assert abs(area - wrong) > 1.0


def test_z_slice_agrees_with_the_prismatic_end_face(session):
    """For a prismatic part the end face IS the cross-section; the two tools
    inside the server must not disagree about the same quantity."""
    session.execute(_BRACKET)
    session.execute("result = bp.part.faces().sort_by(Axis.Z)[-1].area")
    face_area = session.namespace["result"]
    areas = _areas(session, object_name="bracket", axis="Z", num_slices=3)
    assert areas[0] == pytest.approx(face_area, abs=1e-3)


def test_notch_cutting_axis_still_correct(session):
    """Along X the holes cut open notches, not internal loops — this axis was
    already right and must stay right."""
    session.execute(_BRACKET)
    areas = _areas(session, object_name="bracket", axis="X", num_slices=7)
    # Mid-span slices miss the holes entirely: full 40 x 10 rectangle.
    assert areas[3] == pytest.approx(400.0, abs=1e-3)


def test_solid_without_voids_is_unchanged(session):
    """No internal loops — the classification must be a no-op."""
    session.execute("show(Box(20, 30, 10), 'block')")
    for area in _areas(session, object_name="block", axis="Z", num_slices=5):
        assert area == pytest.approx(600.0, abs=1e-3)


def test_island_inside_a_cavity_adds_back(session):
    """A post standing in an annular pocket nests two deep: the pocket
    subtracts and the post inside it adds back."""
    session.execute("""
with BuildPart() as bp:
    Box(60, 60, 10)
    with BuildSketch():
        Circle(20)
        Circle(8, mode=Mode.SUBTRACT)
    extrude(amount=10, mode=Mode.SUBTRACT)
show(bp.part, 'pocket')
""")
    expected = 60 * 60 - math.pi * 20**2 + math.pi * 8**2
    # Slice inside the pocket depth (the box spans z -5..5, the cut z 0..10).
    areas = _areas(session, object_name="pocket", axis="Z", num_slices=3)
    assert areas[-1] == pytest.approx(expected, abs=1e-3)


def test_disjoint_regions_each_keep_their_own_holes(session):
    """Two separated blocks, each bored: both outer loops add, both bores
    subtract — nesting is not assumed to be a single outer boundary."""
    session.execute("""
with BuildPart() as bp:
    with BuildSketch():
        with Locations((-40, 0)):
            Rectangle(30, 30)
            Circle(5, mode=Mode.SUBTRACT)
        with Locations((40, 0)):
            Rectangle(30, 30)
            Circle(5, mode=Mode.SUBTRACT)
    extrude(amount=10)
show(bp.part, 'pair')
""")
    expected = 2 * (30 * 30 - math.pi * 5**2)
    for area in _areas(session, object_name="pair", axis="Z", num_slices=3):
        assert area == pytest.approx(expected, abs=1e-3)


def test_inspect_part_sections_share_the_fix(session):
    """inspect_part's sections block runs the same code path, so its
    variation_ratio/constant_section are derived from corrected areas."""
    session.execute(_BRACKET)
    report = json.loads(inspect_part(session, object_name="bracket"))
    samples = report["sections"]["samples"]
    assert samples, "expected inspect_part to report section samples"
    for sample in samples:
        assert sample["area"] == pytest.approx(_BRACKET_TRUE, abs=1e-3)
    # A prismatic part read through corrected areas is a constant section.
    assert report["sections"]["constant_section"] is True


def test_well_formed_slices_carry_no_uncertainty_flag(session):
    """`area_uncertain` marks a slice whose loop classification did not
    complete. Ordinary geometry must never set it, or the signal is noise."""
    session.execute(_BRACKET)
    for axis in ("X", "Y", "Z"):
        for record in json.loads(cross_sections(session, object_name="bracket", axis=axis)):
            assert "area_uncertain" not in record, f"{axis} slice flagged: {record}"
