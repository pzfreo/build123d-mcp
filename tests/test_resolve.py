"""Tests for the resolve() tool."""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.resolve import resolve
from build123d_mcp.tools.session_state import session_state


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


@pytest.fixture
def box_session(session):
    session.execute("b = Box(10, 10, 20); show(b, 'box')")
    return session


def test_top_face_by_z_filter(box_session):
    """Resolve top face of a box by Z filter."""
    result = json.loads(resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]"))
    assert "error" not in result
    assert result["type"] == "Face"
    assert result["object"] == "box"
    # Top face of a 20mm tall box centred at origin: Z = 10
    assert abs(result["center"][2] - 10.0) < 0.1


def test_face_has_area(box_session):
    result = json.loads(resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]"))
    assert "area" in result
    assert result["area"] > 0


def test_face_has_normal(box_session):
    result = json.loads(resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]"))
    assert "normal" in result
    # Normal should point in +Z direction
    assert result["normal"][2] > 0.9


def test_unknown_object_returns_error(box_session):
    result = json.loads(resolve(box_session, "nonexistent", ".faces()[0]"))
    assert "error" in result


def test_label_stored_in_session(box_session):
    resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]", label="top_face")
    assert "top_face" in box_session.geometry_refs
    stored = box_session.geometry_refs["top_face"]
    assert stored["type"] == "Face"
    assert stored["label"] == "top_face"


def test_label_appears_in_session_state(box_session):
    resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]", label="top_face")
    state = json.loads(session_state(box_session))
    assert "geometry_refs" in state
    assert "top_face" in state["geometry_refs"]


def test_ref_format_with_label(box_session):
    result = json.loads(resolve(box_session, "box", ".faces()[0]", label="base"))
    assert result["ref"] == "@cad[box#base]"


def test_bad_selector_returns_error(box_session):
    result = json.loads(resolve(box_session, "box", ".nonexistent_method()"))
    assert "error" in result


def test_dunder_traversal_rejected(box_session):
    """Selector cannot traverse dunder attributes (issue #186 sandbox escape)."""
    result = json.loads(resolve(box_session, "box", ".__class__.__mro__"))
    assert "error" in result
    assert "rejected" in result["error"].lower()


def test_subclasses_escape_rejected(box_session):
    """The classic __subclasses__() escape chain is blocked (issue #186)."""
    result = json.loads(resolve(box_session, "box", ".__class__.__base__.__subclasses__()"))
    assert "error" in result
    assert "type" not in result


def test_blocked_builtin_in_selector_rejected(box_session):
    """A blocked builtin call in the selector is rejected (issue #186)."""
    result = json.loads(resolve(box_session, "box", " or getattr(obj, 'volume')"))
    assert "error" in result
    assert "rejected" in result["error"].lower()


def test_geometry_refs_cleared_on_reset(box_session):
    resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]", label="top_face")
    assert "top_face" in box_session.geometry_refs
    box_session.reset()
    assert box_session.geometry_refs == {}


# --- entity centres (#456) ---------------------------------------------------
#
# build123d's default CenterOf.GEOMETRY is the parametric midpoint, which on a
# closed curve or a cylindrical surface lies ON the entity, a full radius from
# the axis. Planar faces are unaffected, which is what made this easy to miss.


@pytest.fixture
def plate_session(session):
    """60 x 40 x 10 plate, four Ø4.5 through holes at (±22, ±12)."""
    session.execute("""
with BuildPart() as bp:
    with BuildSketch():
        Rectangle(60, 40)
        with Locations((22, 12), (-22, 12), (22, -12), (-22, -12)):
            Circle(2.25, mode=Mode.SUBTRACT)
    extrude(amount=10)
show(bp.part, 'plate')
""")
    return session


def _resolved(session, selector):
    return json.loads(resolve(session, object_name="plate", selector=selector))


def test_cylindrical_face_centre_is_the_axis_not_the_wall(plate_session):
    d = _resolved(plate_session, ".faces().filter_by(GeomType.CYLINDER).sort_by(Axis.X)[-1]")
    assert d["center"] == pytest.approx([22.0, 12.0, 5.0], abs=1e-6)
    # The old answer was the wall itself, one radius out.
    assert d["center"][0] != pytest.approx(19.75, abs=1e-6)


def test_circular_edge_centre_is_the_arc_centre(plate_session):
    d = _resolved(plate_session, ".edges().filter_by(GeomType.CIRCLE).sort_by(Axis.X)[0]")
    assert d["center"] == pytest.approx([-22.0, -12.0, 0.0], abs=1e-6)
    assert d["radius"] == pytest.approx(2.25, abs=1e-6)


def test_planar_face_centre_is_unchanged(plate_session):
    """Planes were always right and must stay right."""
    d = _resolved(plate_session, ".faces().sort_by(Axis.Z)[-1]")
    assert d["center"] == pytest.approx([0.0, 0.0, 10.0], abs=1e-6)
    assert d["normal"] == pytest.approx([0.0, 0.0, 1.0], abs=1e-6)


def test_curved_face_reports_an_axis_instead_of_a_normal(plate_session):
    """A cylinder has no single normal; normal_at() answers for one surface
    point and reads as though it described the face."""
    d = _resolved(plate_session, ".faces().filter_by(GeomType.CYLINDER).sort_by(Axis.X)[-1]")
    assert "normal" not in d
    assert d["geom_type"] == "CYLINDER"
    assert d["axis"]["origin"] == pytest.approx([22.0, 12.0, 0.0], abs=1e-6)
    assert [abs(c) for c in d["axis"]["direction"]] == pytest.approx([0.0, 0.0, 1.0], abs=1e-6)
    assert d["radius"] == pytest.approx(2.25, abs=1e-6)


def test_partial_cylinder_reports_the_true_axis(session):
    """On a fillet the area centroid still lies on the patch, so the centre
    alone is not the axis — the axis field is what carries it."""
    session.execute("""
with BuildPart() as bp:
    with BuildSketch():
        RectangleRounded(60, 40, 5)
    extrude(amount=10)
show(bp.part, 'plate')
""")
    d = _resolved(session, ".faces().filter_by(GeomType.CYLINDER).sort_by(Axis.X)[-1]")
    assert d["axis"]["origin"][:2] == pytest.approx([25.0, 15.0], abs=1e-6)
    assert d["radius"] == pytest.approx(5.0, abs=1e-6)
    # The patch centroid is genuinely on the surface, and must not be mistaken
    # for the axis.
    assert d["center"][:2] != pytest.approx([25.0, 15.0], abs=1e-3)


def test_list_selector_reports_count_and_per_entity_data(plate_session):
    d = _resolved(plate_session, ".faces().filter_by(GeomType.CYLINDER)")
    assert d["count"] == 4
    assert len(d["entities"]) == 4
    assert all("center" in e and "area" in e for e in d["entities"])


def test_list_aggregate_centre_agrees_with_its_entities(plate_session):
    """The aggregate is averaged from the corrected per-entity centres, so the
    list and the individual descriptors cannot disagree. ShapeList.center()
    takes no CenterOf argument and inherits the same radius offset."""
    d = _resolved(plate_session, ".faces().filter_by(GeomType.CYLINDER)")
    assert d["center"] == pytest.approx([0.0, 0.0, 5.0], abs=1e-6)
    centers = [e["center"] for e in d["entities"]]
    expected = [sum(c[i] for c in centers) / len(centers) for i in range(3)]
    assert d["center"] == pytest.approx(expected, abs=1e-6)


def test_resolve_agrees_with_the_recognisers_on_hole_position(plate_session):
    """find_holes() already reported true axes while resolve() did not; the two
    must not name one hole two places."""
    from build123d_mcp.tools.find_features import find_holes

    holes = json.loads(find_holes(plate_session, object_name="plate"))["holes"]
    hole_xy = sorted((round(h["location"][0], 4), round(h["location"][1], 4)) for h in holes)
    d = _resolved(plate_session, ".faces().filter_by(GeomType.CYLINDER)")
    resolved_xy = sorted(
        (round(e["center"][0], 4), round(e["center"][1], 4)) for e in d["entities"]
    )
    assert resolved_xy == hole_xy


def test_sphere_reports_no_axis(session):
    """Every axis through a sphere's centre is an axis of rotation, so naming
    the parametrisation's +Z would be as arbitrary as a cylinder's normal. Its
    centre and radius already say everything."""
    session.execute("show(Sphere(10), 'plate')")
    d = _resolved(session, ".faces()[0]")
    assert d["geom_type"] == "SPHERE"
    assert "axis" not in d
    assert "normal" not in d
    assert d["center"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-6)
    assert d["radius"] == pytest.approx(10.0, abs=1e-6)


def test_cone_reports_an_axis_but_no_single_radius(session):
    """A countersink's cone has an axis worth reporting and no one radius."""
    session.execute("""
with BuildPart() as bp:
    Box(40, 40, 10)
    with Locations((0, 0, 5)):
        CounterSinkHole(2, 5)
show(bp.part, 'plate')
""")
    d = _resolved(session, ".faces().filter_by(GeomType.CONE)[0]")
    assert d["geom_type"] == "CONE"
    assert d["axis"]["origin"] == pytest.approx([0.0, 0.0, 5.0], abs=1e-6)
    assert "radius" not in d
    assert "normal" not in d


def test_elliptical_edge_uses_the_arc_centre(session):
    """An angled cut through a cylinder gives an ellipse. Its mass centroid is
    close to the centre but not equal to it; arc_center is exact."""
    session.execute("""
with BuildPart() as bp:
    with BuildSketch(Plane.XY.rotated((0, 30, 0))):
        Circle(8)
    extrude(amount=30, both=True)
    Box(60, 60, 10, mode=Mode.INTERSECT)
show(bp.part, 'plate')
""")
    d = _resolved(session, ".edges().filter_by(GeomType.ELLIPSE)[0]")
    session.execute(
        "e = bp.part.edges().filter_by(GeomType.ELLIPSE)[0]\nresult = tuple(e.arc_center)"
    )
    assert d["center"] == pytest.approx(list(session.namespace["result"]), abs=1e-6)


def test_truncated_list_still_reports_a_complete_aggregate(session):
    """Per-entity detail is capped, but the aggregate centre must be averaged
    over every match — a centre from the first 50 of 121 would be a sample
    presented as the whole."""
    session.execute("""
pts = [(x * 6 - 60, y * 6 - 60) for x in range(11) for y in range(11)]
with BuildPart() as bp:
    with BuildSketch():
        Rectangle(140, 140)
        with Locations(*pts):
            Circle(1.5, mode=Mode.SUBTRACT)
    extrude(amount=5)
show(bp.part, 'plate')
""")
    d = _resolved(session, ".faces().filter_by(GeomType.CYLINDER)")
    assert d["count"] == 121
    assert len(d["entities"]) == 50
    assert d["entities_truncated"] is True
    # Mean of the full 11x11 grid spanning -60..0 on both axes.
    assert d["center"] == pytest.approx([-30.0, -30.0, 2.5], abs=1e-6)


def test_non_analytic_face_degrades_without_axis_or_normal(session):
    """A swept/spline face has neither a single normal nor an axis; it must
    still report what it does have rather than erroring out."""
    session.execute("""
with BuildPart() as bp:
    with BuildSketch():
        with BuildLine():
            Spline((0, 0), (10, 5), (20, -5), (30, 0))
            Line((30, 0), (30, -10))
            Line((30, -10), (0, -10))
            Line((0, -10), (0, 0))
        make_face()
    extrude(amount=5)
show(bp.part, 'plate')
""")
    d = _resolved(session, ".faces().filter_by(GeomType.EXTRUSION)[0]")
    assert d["geom_type"] == "EXTRUSION"
    assert d["area"] > 0
    assert "center" in d
    assert "axis" not in d
    assert "normal" not in d


def test_empty_list_selector_reports_zero_not_an_error(session):
    session.execute("show(Box(10, 10, 10), 'plate')")
    d = _resolved(session, ".faces().filter_by(GeomType.TORUS)")
    assert d["count"] == 0
    assert "error" not in d
