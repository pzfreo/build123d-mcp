"""measure() reports solids, through-openings and voids the way a mesh topology check would."""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.measure import measure

PLATE = "plate = Box(60, 40, 10)\n"
HOLES = "pts = [(x, y, 0) for x in (-20, 0, 20) for y in (-10, 10)]\n"


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


CASES = [
    ("box", "result = Box(10, 10, 10)", (1, 0, 0)),
    (
        "six_through",
        PLATE + HOLES + "result = plate - [Pos(*p) * Cylinder(2, 20) for p in pts]",
        (1, 6, 0),
    ),
    (
        "six_blind",
        PLATE + HOLES + "result = plate - [Pos(p[0], p[1], 5) * Cylinder(2, 8) for p in pts]",
        (1, 0, 0),
    ),
    (
        "blind_drill_point",
        PLATE
        + "point = Pos(0, 0, -1) * Cone(0, 2, 2, align=(Align.CENTER, Align.CENTER, Align.MAX))\n"
        + "result = plate - (Pos(0, 0, 5) * Cylinder(2, 6, align=(Align.CENTER, Align.CENTER, Align.MAX))) - point",
        (1, 0, 0),
    ),
    (
        "countersunk_through",
        PLATE
        + "result = plate - Cylinder(2, 20) - Pos(0, 0, 5) * Cone(2, 5, 3, align=(Align.CENTER, Align.CENTER, Align.MAX))",
        (1, 1, 0),
    ),
    ("ring", "result = Cylinder(20, 5) - Cylinder(10, 5)", (1, 1, 0)),
    ("torus", "result = Torus(20, 5)", (1, 1, 0)),
    ("sphere", "result = Sphere(10)", (1, 0, 0)),
    ("void", "result = Box(20, 20, 20) - Sphere(4)", (1, 0, 1)),
    ("toroidal_void", "result = Box(60, 60, 30) - Torus(15, 4)", (1, 1, 1)),
    (
        "tube_radial_one_wall",
        "result = (Cylinder(10, 40) - Cylinder(8, 40)) - Pos(5, 0, 0) * Rot(0, 90, 0) * Cylinder(2, 10)",
        (1, 2, 0),
    ),
    (
        "tube_radial_both_walls",
        "result = (Cylinder(10, 40) - Cylinder(8, 40)) - Rot(0, 90, 0) * Cylinder(2, 30)",
        (1, 3, 0),
    ),
    (
        "two_solids",
        "result = Compound([Box(5, 5, 5), Pos(20, 0, 0) * (Box(10, 10, 4) - Cylinder(2, 10))])",
        (2, 1, 0),
    ),
]


@pytest.mark.parametrize(("code", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_through_openings(session, code, expected):
    execute_code(session, code)
    topo = json.loads(measure(session))["topology"]
    assert (topo["solids"], topo["through_openings"], topo["voids"]) == expected


def test_no_solid_reports_no_betti(session):
    execute_code(session, "result = Rectangle(10, 10)")
    topo = json.loads(measure(session))["topology"]
    assert not {"solids", "through_openings", "voids"} & topo.keys()


def test_open_solid_reports_solids_but_no_counts(session):
    execute_code(
        session, "result = Solid(Shell(Box(10, 10, 10).faces().filter_by(Axis.Z, reverse=True)))"
    )
    topo = json.loads(measure(session))["topology"]
    assert topo["solids"] == 1
    assert topo["through_openings"] is None and topo["voids"] is None
