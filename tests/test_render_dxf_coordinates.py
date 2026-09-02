"""render_view DXF/SVG projections are anchored at the world origin (#455).

project_to_viewport returns 2D coordinates relative to its look_at point, so
anchoring on the aggregate part centroid displaced every written coordinate by
the centre of mass. The shape stayed exact; only the origin moved — silent,
part-dependent, and invisible to inspection until the file is placed against
other geometry.
"""

import ezdxf
import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.render import render_view

# Deliberately asymmetric, so a centroid anchor is well away from the origin.
_ASYM = """
part = (Box(180, 100, 12)
        + Pos(60, 20, 6) * Cylinder(10, 8, align=(Align.CENTER, Align.CENTER, Align.MIN))
        + Pos(-55, 25, 6) * Cylinder(10, 8, align=(Align.CENTER, Align.CENTER, Align.MIN))
        - Pos(-40, -30, 0) * Cylinder(4, 40)
        - Pos(20, -35, 0) * Cylinder(3, 40))
show(part, 'asym')
"""


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


@pytest.fixture
def asym(session):
    session.execute(_ASYM)
    return session


def _dxf_extents(path):
    doc = ezdxf.readfile(str(path))
    xs, ys = [], []
    for e in doc.modelspace():
        if e.dxftype() == "CIRCLE":
            c = e.dxf.center
            xs += [c.x - e.dxf.radius, c.x + e.dxf.radius]
            ys += [c.y - e.dxf.radius, c.y + e.dxf.radius]
        elif e.dxftype() == "LINE":
            xs += [e.dxf.start.x, e.dxf.end.x]
            ys += [e.dxf.start.y, e.dxf.end.y]
    return min(xs), max(xs), min(ys), max(ys)


def test_top_view_dxf_is_in_model_coordinates(asym, tmp_path):
    """The reported case: the offset equalled the negated centre-of-mass xy."""
    out = tmp_path / "top.dxf"
    render_view(asym, objects="asym", direction="top", format="dxf", save_to=str(out))
    x0, x1, y0, y1 = _dxf_extents(out)
    bb = asym.objects["asym"].bounding_box()
    assert x0 == pytest.approx(bb.min.X, abs=1e-4)
    assert x1 == pytest.approx(bb.max.X, abs=1e-4)
    assert y0 == pytest.approx(bb.min.Y, abs=1e-4)
    assert y1 == pytest.approx(bb.max.Y, abs=1e-4)


def test_top_view_circle_centres_are_model_positions(asym, tmp_path):
    """A consumer placing a fastener reads the circle centre; it has to be
    where the feature actually is."""
    out = tmp_path / "top.dxf"
    render_view(asym, objects="asym", direction="top", format="dxf", save_to=str(out))
    doc = ezdxf.readfile(str(out))
    centres = {
        (round(e.dxf.center.x, 4), round(e.dxf.center.y, 4))
        for e in doc.modelspace()
        if e.dxftype() == "CIRCLE"
    }
    assert centres == {(-55.0, 25.0), (-40.0, -30.0), (20.0, -35.0), (60.0, 20.0)}


def test_offset_is_not_the_centre_of_mass(asym, tmp_path):
    """Guard the specific defect rather than only the corrected value: the
    written origin must not track the centroid."""
    out = tmp_path / "top.dxf"
    render_view(asym, objects="asym", direction="top", format="dxf", save_to=str(out))
    x0, _x1, y0, _y1 = _dxf_extents(out)
    bb = asym.objects["asym"].bounding_box()
    from build123d import CenterOf

    com = asym.objects["asym"].center(CenterOf.MASS)
    assert com.X != pytest.approx(0.0, abs=1e-3), "fixture must be asymmetric to be a test"
    assert (x0 - bb.min.X) != pytest.approx(-com.X, abs=1e-4)
    assert (y0 - bb.min.Y) != pytest.approx(-com.Y, abs=1e-4)


@pytest.mark.parametrize(
    ("direction", "axes"),
    [("top", ("X", "Y")), ("front", ("X", "Z")), ("side", ("Y", "Z"))],
)
def test_every_cardinal_view_is_origin_anchored(asym, tmp_path, direction, axes):
    """Each elevation projects a different pair of model axes; all of them must
    land on model coordinates, not just the plan view."""
    out = tmp_path / f"{direction}.dxf"
    render_view(asym, objects="asym", direction=direction, format="dxf", save_to=str(out))
    x0, x1, y0, y1 = _dxf_extents(out)
    bb = asym.objects["asym"].bounding_box()
    horiz = getattr(bb.size, axes[0])
    vert = getattr(bb.size, axes[1])
    assert (x1 - x0) == pytest.approx(horiz, abs=1e-3)
    assert (y1 - y0) == pytest.approx(vert, abs=1e-3)
    # Anchored at the origin: the projected span straddles it exactly as the
    # model does on those axes.
    assert x0 == pytest.approx(getattr(bb.min, axes[0]), abs=1e-3)


def test_geometry_is_unchanged_by_the_anchor(asym, tmp_path):
    """Only the origin moves — radii and spans were already exact and must
    stay so."""
    out = tmp_path / "top.dxf"
    render_view(asym, objects="asym", direction="top", format="dxf", save_to=str(out))
    doc = ezdxf.readfile(str(out))
    radii = sorted({round(e.dxf.radius, 4) for e in doc.modelspace() if e.dxftype() == "CIRCLE"})
    assert radii == [3.0, 4.0, 10.0]
    x0, x1, y0, y1 = _dxf_extents(out)
    assert (x1 - x0) == pytest.approx(180.0, abs=1e-4)
    assert (y1 - y0) == pytest.approx(100.0, abs=1e-4)


def test_part_far_from_the_origin_still_projects_exactly(session, tmp_path):
    """The projection is parallel, so anchoring at the world origin holds even
    when the part is nowhere near it."""
    session.execute("show(Pos(10000, 10000, 0) * Box(180, 100, 12), 'far')")
    out = tmp_path / "far.dxf"
    render_view(session, objects="far", direction="top", format="dxf", save_to=str(out))
    x0, x1, y0, y1 = _dxf_extents(out)
    bb = session.objects["far"].bounding_box()
    assert x0 == pytest.approx(bb.min.X, abs=1e-3)
    assert (x1 - x0) == pytest.approx(180.0, abs=1e-3)
    assert (y1 - y0) == pytest.approx(100.0, abs=1e-3)


def test_dxf_emits_true_arcs_not_polylines(asym, tmp_path):
    """The tool description claimed polylines; it writes real CIRCLE and LINE
    entities, so arcs are exact rather than tessellated."""
    out = tmp_path / "top.dxf"
    render_view(asym, objects="asym", direction="top", format="dxf", save_to=str(out))
    kinds = {e.dxftype() for e in ezdxf.readfile(str(out)).modelspace()}
    assert "CIRCLE" in kinds
    assert not {"LWPOLYLINE", "POLYLINE"} & kinds


def test_svg_still_renders_after_the_anchor_change(asym, tmp_path):
    """SVG shares the projection helper; ExportSVG auto-fits with a margin, so
    absolute coordinates are harmless there."""
    out = tmp_path / "v.svg"
    render_view(asym, objects="asym", direction="top", format="svg", save_to=str(out))
    body = out.read_bytes()
    assert body.startswith(b"<?xml") or b"<svg" in body[:200]
    assert len(body) > 500
