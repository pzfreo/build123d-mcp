"""find_countersinks — in-house countersink recognition (Apache, repatriable)."""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.recognizers.countersink import find_countersinks, recognise_countersinks


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


_CSK = (
    "with BuildPart() as p:\n"
    "    Box(60, 40, 12)\n"
    "    top = p.faces().sort_by(Axis.Z)[-1]\n"
    "    with Locations(top):\n"
    "        with Locations((-15, 0), (15, 0)):\n"
    "            CounterSinkHole(radius=3, counter_sink_radius=6, counter_sink_angle=82)\n"
    "result = p.part\n"
    "show(result, 'p')\n"
)
_PLAIN = (
    "with BuildPart() as p:\n"
    "    Box(60, 40, 12)\n"
    "    with Locations((-15, 0), (15, 0)):\n"
    "        Hole(3)\n"
    "result = p.part\n"
    "show(result, 'p')\n"
)


def test_recognises_countersinks(session):
    session.execute(_CSK)
    cs = recognise_countersinks(session.current_shape)
    assert len(cs) == 2
    c = cs[0]
    assert c["major_diameter"] == 12.0
    assert c["drill_diameter"] == 6.0
    assert c["included_angle"] == 82.0
    assert c["depth"] > 0


def test_plain_holes_have_no_countersinks(session):
    session.execute(_PLAIN)
    assert recognise_countersinks(session.current_shape) == []


def test_external_chamfer_is_not_a_countersink(session):
    # a chamfered outer edge is also a CONE face, but has no coaxial drilled bore
    session.execute("show(chamfer(Box(20, 20, 20).edges().group_by(Axis.Z)[-1], 3), 'c')\n")
    assert recognise_countersinks(session.current_shape) == []


def test_tool_wrapper_json(session):
    session.execute(_CSK)
    r = json.loads(find_countersinks(session, "p"))
    assert r["count"] == 2 and len(r["countersinks"]) == 2


def test_tool_unknown_object_error(session):
    assert "error" in json.loads(find_countersinks(session, "nope"))


def test_near_flat_cone_is_not_a_countersink(session):
    # a 178° near-flat conical relief is a draft/washer face, not a countersink
    session.execute(
        "with BuildPart() as p:\n"
        "    Box(60, 40, 12)\n"
        "    top = p.faces().sort_by(Axis.Z)[-1]\n"
        "    with Locations(top):\n"
        "        CounterSinkHole(radius=3, counter_sink_radius=10, counter_sink_angle=178)\n"
        "result = p.part\n"
        "show(result, 'p')\n"
    )
    assert recognise_countersinks(session.current_shape) == []


@pytest.mark.parametrize("face,into_z", [("[-1]", -1.0), ("[0]", 1.0)])
def test_axis_points_into_the_part(session, face, into_z):
    # top-drilled → axis -z; bottom-drilled → axis +z; both point INTO the part
    session.execute(
        "with BuildPart() as p:\n"
        "    Box(60, 40, 12)\n"
        f"    face = p.faces().sort_by(Axis.Z){face}\n"
        "    with Locations(face):\n"
        "        CounterSinkHole(radius=3, counter_sink_radius=6, counter_sink_angle=82)\n"
        "result = p.part\n"
        "show(result, 'p')\n"
    )
    cs = recognise_countersinks(session.current_shape)
    assert cs and cs[0]["axis"] == [0.0, 0.0, into_z]
