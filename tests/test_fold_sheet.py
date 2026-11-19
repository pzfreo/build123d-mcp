"""fold_sheet folds a flat sheet-metal pattern exactly."""

import json
import math

import pytest
from build123d import Align, Circle, Pos, Rectangle

from build123d_mcp.session import Session
from build123d_mcp.sheet_metal import fold_sheet
from build123d_mcp.tools.measure import measure

T, R, K = 3.0, 2.0, 0.33
BA90 = math.pi / 2 * (R + K * T)  # bend allowance of a 90 degree bend


def blank(w=100, h=40):
    return Rectangle(w, h, align=(Align.MIN, Align.MIN)).faces()[0]


def box(shape):
    bb = shape.bounding_box()
    return tuple(round(v, 3) for v in (*bb.min, *bb.max))


@pytest.mark.parametrize("direction", ["up", "down"])
def test_single_90_bend_lands_flange_exactly(direction):
    s = fold_sheet(
        blank(), T, [{"line": ((60, 0), (60, 40)), "direction": direction, "radius": R, "k": K}]
    )
    assert s.is_valid
    flange = 100 - 60 - BA90 / 2
    corner = 60 - BA90 / 2  # where the bend starts
    if direction == "up":
        # inside radius on top: outer face of the flange at corner + R + T, flange tip above
        assert box(s) == pytest.approx((0, 0, 0, corner + R + T, 40, T + R + flange), abs=1e-3)
    else:
        assert box(s) == pytest.approx((0, 0, -(R + flange), corner + R + T, 40, T), abs=1e-3)


def test_inside_and_outside_bend_radii():
    s = fold_sheet(
        blank(), T, [{"line": ((60, 0), (60, 40)), "direction": "up", "radius": R, "k": K}]
    )
    radii = sorted({round(f.radius, 6) for f in s.faces() if f.geom_type.name == "CYLINDER"})
    assert radii == pytest.approx([R, R + T])


def test_bend_line_direction_does_not_matter():
    a = fold_sheet(blank(), T, [{"line": ((60, 0), (60, 40)), "direction": "up", "radius": R}])
    b = fold_sheet(blank(), T, [{"line": ((60, 40), (60, 0)), "direction": "up", "radius": R}])
    assert box(a) == box(b)
    assert a.volume == pytest.approx(b.volume, rel=1e-9)


def test_u_channel_two_bends_both_up():
    bends = [
        {"line": ((30, 0), (30, 40)), "direction": "up", "radius": R},
        {"line": ((70, 0), (70, 40)), "direction": "up", "radius": R},
    ]
    s = fold_sheet(blank(), T, bends, fixed=(50, 20))
    assert s.is_valid and len(s.solids()) == 1
    ba = math.pi / 2 * (R + 0.33 * T)
    leg = 30 - ba / 2
    lo, hi = 30 + ba / 2, 70 - ba / 2  # flat web, unchanged in place
    assert box(s) == pytest.approx((lo - R - T, 0, 0, hi + R + T, 40, T + R + leg), abs=1e-3)


def test_z_bend_chains_down_then_up():
    # A Z: first flange down, then the tip back up to run parallel to the base.
    bends = [
        {"line": ((40, 0), (40, 40)), "direction": "down", "radius": R},
        {"line": ((70, 0), (70, 40)), "direction": "up", "radius": R},
    ]
    s = fold_sheet(blank(), T, bends, fixed=(10, 20))
    assert s.is_valid and len(s.solids()) == 1
    bb = s.bounding_box()
    # The tip runs back along +X, below the base by the drop of the middle web.
    drop = R + (70 - 40 - BA90) + R + T  # inside radius, web, outside radius of the second bend
    tops = sorted(
        {
            round(f.center().Z, 3)
            for f in s.faces()
            if f.geom_type.name == "PLANE" and abs(f.normal_at().Z) > 0.99
        }
    )
    assert tops[0] == pytest.approx(-drop, abs=1e-3)
    assert bb.max.X > 70


def test_45_degree_bend():
    s = fold_sheet(
        blank(), T, [{"line": ((60, 0), (60, 40)), "direction": "up", "angle": 45, "radius": R}]
    )
    ba = math.pi / 4 * (R + K * T)
    flange = 100 - 60 - ba / 2
    bb = s.bounding_box()
    # The flange's bottom edge rises at 45 degrees from the outer arc's end.
    assert bb.max.Z == pytest.approx(
        (T + R)
        - (T + R) * math.cos(math.pi / 4)
        + flange * math.sin(math.pi / 4)
        + T * math.cos(math.pi / 4),
        abs=1e-3,
    )


def test_holes_in_flange_are_carried():
    outline = blank() - Pos(85, 20) * Circle(3)
    s = fold_sheet(
        outline.faces()[0], T, [{"line": ((60, 0), (60, 40)), "direction": "up", "radius": R}]
    )
    holes = [f for f in s.faces() if f.geom_type.name == "CYLINDER" and abs(f.radius - 3) < 1e-6]
    assert len(holes) == 1
    c = holes[0].bounding_box().center()
    # The hole now sits in the vertical flange: same Y, lifted in Z, at the flange's X.
    assert c.Y == pytest.approx(20, abs=1e-3)
    assert c.Z == pytest.approx(T + R + (85 - 60 - BA90 / 2), abs=1e-3)


def test_fixed_region_choice_moves_the_other_side():
    a = fold_sheet(
        blank(), T, [{"line": ((60, 0), (60, 40)), "direction": "up", "radius": R}], fixed=(10, 20)
    )
    b = fold_sheet(
        blank(), T, [{"line": ((60, 0), (60, 40)), "direction": "up", "radius": R}], fixed=(90, 20)
    )
    assert a.bounding_box().min.X == pytest.approx(0, abs=1e-6)
    assert b.bounding_box().max.X == pytest.approx(100, abs=1e-6)
    assert a.volume == pytest.approx(b.volume, rel=1e-9)


@pytest.mark.parametrize(
    ("bends", "message"),
    [
        ([], "nonempty"),
        ([{"line": ((60, 0), (60, 0))}], "zero length"),
        ([{"line": ((60, 0), (60, 40)), "direction": "sideways"}], "'up' or 'down'"),
        ([{"line": ((60, 0), (60, 40)), "angle": 180}], "< 180"),
        ([{"line": ((60, 0), (60, 40)), "k": 2}], "between 0 and 1"),
        ([{"line": ((60, 0), (60, 40)), "colour": "red"}], "unknown keys"),
    ],
)
def test_bad_bends_are_refused(bends, message):
    with pytest.raises(ValueError, match=message):
        fold_sheet(blank(), T, bends)


def test_outline_off_the_xy_plane_is_refused():
    from build123d import Pos as P

    with pytest.raises(ValueError, match="XY plane"):
        fold_sheet((P(0, 0, 5) * blank()), T, [{"line": ((60, 0), (60, 40))}])


def test_available_inside_execute_without_import():
    session = Session()
    session.execute("from build123d import *")
    out = session.execute(
        "blank = Rectangle(100, 40, align=(Align.MIN, Align.MIN)).faces()[0]\n"
        "result = fold_sheet(blank, 3, [dict(line=((60, 0), (60, 40)), direction='up', radius=2)])"
    )
    assert "Error" not in out, out
    data = json.loads(measure(session))
    assert data["topology"]["solids"] == 1
    assert data["bbox"]["zmax"] == pytest.approx(T + R + 100 - 60 - BA90 / 2, abs=1e-3)
