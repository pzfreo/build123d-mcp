"""mesh_holes / mesh_section — cross-section analysis for meshes.

These fixtures encode the failure modes the tools exist to survive:

  * an imported STL has no topology, so find_holes() sees nothing at all
  * two blind pockets bored into opposite faces share a cross-plane centre and
    a diameter; merging them reports one through hole where there are two
    pockets and solid material between
  * a pocket shallower than one sample step can fall entirely between planes,
    so a bar with an identical pocket in each end reads as having only one
  * an enclosed duct and an open groove look the same from outside
"""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.mesh_analysis import mesh_holes, mesh_section


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


# A 200 mm bar with, along X, one through hole; two 4 mm blind pockets bored
# into opposite faces on a shared axis; and a 4 mm pocket in each end face.
_BAR = (
    "from build123d import *\n"
    "bar = Box(200, 40, 20)\n"
    "bar -= Pos(-60, 0, 0) * Rot(90, 0, 0) * Cylinder(2.5, 60)\n"
    "for sy in (-1, 1):\n"
    "    bar -= Pos(0, sy * 18, 0) * Rot(90, 0, 0) * Cylinder(2.35, 4)\n"
    "for sx in (-1, 1):\n"
    "    bar -= Pos(sx * 98, 0, 0) * Rot(0, 90, 0) * Cylinder(2.35, 4)\n"
    "show(bar, 'bar')\n"
)

# Same outline, but the "duct" is an open groove cut in from the top face.
_DUCT = (
    "from build123d import *\n"
    "b = Box(60, 40, 30)\n"
    "b -= Pos(0, 0, 0) * Rot(90, 0, 0) * Cylinder(5, 60)\n"
    "show(b, 'duct')\n"
)
_GROOVE = (
    "from build123d import *\n"
    "b = Box(60, 40, 30)\n"
    "b -= Pos(0, 0, 15) * Box(10, 60, 20)\n"
    "show(b, 'groove')\n"
)


def _holes(session, name, **kw):
    return json.loads(mesh_holes(session, name, **kw))["holes"]


def test_through_hole_is_reported_as_through(session):
    session.execute(_BAR)
    through = [h for h in _holes(session, "bar") if h["through"]]
    assert len(through) == 1, [h["location"] for h in through]
    assert through[0]["axis"] == "Y"
    assert abs(through[0]["location"][0] - (-60.0)) < 1.0


def test_opposed_blind_pockets_are_not_merged_into_one_through_hole(session):
    """The bug this tool was written around.

    Both pockets sit on the same Y axis at X=0, Z=0, same diameter. Keyed by
    cross-plane centre alone they collapse into a single record spanning the
    whole 40 mm width — reporting a through hole through 32 mm of solid bar.
    """
    session.execute(_BAR)
    at_x0 = [
        h for h in _holes(session, "bar")
        if h["axis"] == "Y" and abs(h["location"][0]) < 1.0
    ]
    assert len(at_x0) == 2, f"expected two separate pockets, got {at_x0}"
    for h in at_x0:
        assert not h["through"]
        assert h["depth"] < 8.0, f"pocket reported {h['depth']} mm deep"
    assert at_x0[0]["location"][1] * at_x0[1]["location"][1] < 0, "not on opposite faces"


def test_a_pocket_thinner_than_one_sample_step_is_still_found(session):
    """4 mm pockets in the ends of a 200 mm bar, sampled every ~4.2 mm.

    A uniform grid can step straight over one of them, so the bar reads as
    having a pocket in one end and nothing in the other.
    """
    session.execute(_BAR)
    ends = [h for h in _holes(session, "bar", slices=48) if h["axis"] == "X"]
    assert len(ends) == 2, f"expected a pocket in each end, got {ends}"
    assert ends[0]["location"][0] * ends[1]["location"][0] < 0, "both found at one end"


def test_an_enclosed_duct_reads_as_an_enclosed_passage(session):
    session.execute(_DUCT)
    # Normal to the duct's OWN axis. Sectioned across it instead, a round duct
    # reads as a slot that splits the outline - correctly, 0 enclosed.
    r = json.loads(mesh_section(session, "duct", axis="Y", position=0.0))
    assert r["enclosed_passages"] == 1
    across = json.loads(mesh_section(session, "duct", axis="Z", position=0.0))
    assert across["enclosed_passages"] == 0


def test_an_open_groove_reads_as_no_passage(session):
    """A groove is a notch in the outline, however deep it looks in a render."""
    session.execute(_GROOVE)
    for axis, pos in (("Z", 10.0), ("Y", 0.0)):
        r = json.loads(mesh_section(session, "groove", axis=axis, position=pos))
        assert r["enclosed_passages"] == 0, f"groove read as enclosed on {axis}"


def test_it_works_on_an_imported_stl_where_find_holes_cannot(session, tmp_path):
    """An STL import is a shell: 0 solids, and the recognisers return nothing."""
    from build123d_mcp.tools.find_features import find_holes

    session.execute(_BAR)
    stl = tmp_path / "bar.stl"
    session.execute(f"export_stl(bar, {str(stl)!r})")
    session.execute(f"imported = import_stl({str(stl)!r})\nshow(imported, 'imported')")

    shell = session.objects["imported"]
    assert len(shell.solids()) == 0, "fixture is not a topology-free shell"
    assert json.loads(find_holes(session, "imported"))["count"] == 0

    holes = _holes(session, "imported")
    assert len(holes) >= 4, f"mesh path found only {len(holes)} holes"


def test_min_depth_drops_slivers(session):
    session.execute(_BAR)
    deep = _holes(session, "bar", min_depth=6.0)
    assert all(h["depth"] >= 6.0 for h in deep)
    assert len(deep) < len(_holes(session, "bar", min_depth=0.0))


def test_bad_axis_is_rejected(session):
    session.execute(_BAR)
    with pytest.raises(ValueError, match="axis must be X, Y or Z"):
        mesh_section(session, "bar", axis="Q")
