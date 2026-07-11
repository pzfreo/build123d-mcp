"""Prototype coverage for the compact generation checkpoint audit (#417)."""

import json

import pytest

from build123d_mcp._shape_op_subprocess import _run
from build123d_mcp.session import Session
from build123d_mcp.tools import _bounded
from build123d_mcp.tools.inspect_part import _check_expected_groups, inspect_part


@pytest.fixture
def session():
    value = Session()
    value.execute("from build123d import *")
    return value


def test_inspect_part_groups_holes_bosses_and_bolt_circle(session):
    session.execute(
        "import math\n"
        "part = Box(80, 80, 8)\n"
        "for i in range(4):\n"
        "    a = math.radians(90 * i)\n"
        "    part -= Pos(25 * math.cos(a), 25 * math.sin(a), 0) * Cylinder(3, 12)\n"
        "part += Pos(0, 0, 8) * Cylinder(8, 8)\n"
        "show(part, 'checkpoint')"
    )

    result = json.loads(inspect_part(session, "checkpoint", section_slices=5))

    assert result["status"] == "INVENTORY"
    assert result["topology"]["solids"] == 1
    assert result["holes"]["count"] == 4
    assert any(
        group["count"] == 4 and group["diameter"] == 6.0 for group in result["holes"]["groups"]
    )
    assert result["bosses"]["count"] == 1
    assert result["patterns"]["groups"] == [
        {
            "type": "bolt_circle",
            "center": [-0.0, 0.0, 4.0],
            "diameter": 50.0,
            "member_count": 4,
            "member_diameter": 6.0,
            "count": 1,
        }
    ]

    checked = json.loads(
        inspect_part(
            session,
            "checkpoint",
            expected=json.dumps(
                {
                    "patterns": [
                        {
                            "count": 1,
                            "type": "bolt_circle",
                            "diameter": 50,
                            "member_count": 4,
                            "member_diameter": 6,
                        }
                    ]
                }
            ),
        )
    )
    assert checked["status"] == "PASS"


def test_inspect_part_expectations_pass_and_fail(session):
    session.execute(
        "part = Box(40, 30, 10)\n"
        "part -= Pos(-10, 0, 0) * Cylinder(2, 14)\n"
        "part -= Pos(10, 0, 0) * Cylinder(2, 14)\n"
        "show(part, 'plate')"
    )
    expected = {
        "bbox": [40, 30, 10],
        "solid_count": 1,
        "holes": [{"count": 2, "diameter": 4, "axis": [0, 0, 1], "bottom": "through"}],
    }

    passing = json.loads(inspect_part(session, "plate", expected=json.dumps(expected)))
    assert passing["status"] == "PASS"
    assert passing["passes_expectations"] is True

    expected["holes"][0]["count"] = 3
    failing = json.loads(inspect_part(session, "plate", expected=json.dumps(expected)))
    assert failing["status"] == "FAIL"
    assert failing["passes_expectations"] is False
    assert "expected 3 hole feature(s)" in failing["mismatches"][0]


def test_inspect_part_cored_profile_reports_section_variation(session):
    session.execute(
        "outer = Box(40, 40, 20)\n"
        "cavity = Pos(0, 0, 5) * Box(30, 30, 12)\n"
        "show(outer - cavity, 'cored')"
    )

    result = json.loads(
        inspect_part(
            session,
            "cored",
            section_axis="Z",
            section_slices=7,
            expected=json.dumps({"section_varying": True}),
        )
    )

    assert result["status"] == "PASS"
    assert result["sections"]["constant_section"] is False
    assert result["sections"]["variation_ratio"] > 0.1


def test_inspect_part_thin_wall_reports_constant_section_profile(session):
    session.execute(
        "outer = Box(40, 40, 20)\ninner = Box(36, 36, 24)\nshow(outer - inner, 'thin_wall')"
    )

    result = json.loads(
        inspect_part(
            session,
            "thin_wall",
            section_slices=5,
            expected=json.dumps({"section_varying": False}),
        )
    )

    assert result["status"] == "PASS"
    assert result["sections"]["constant_section"] is True
    areas = [section["area"] for section in result["sections"]["samples"]]
    assert areas[0] > 0
    assert areas == pytest.approx([areas[0]] * 5)


def test_inspect_part_linear_pattern_preserves_and_checks_relationship(session):
    session.execute(
        "part = Box(60, 30, 8)\n"
        "for x in (-15, -5, 5, 15):\n"
        "    part -= Pos(x, 0, 0) * Cylinder(2, 12)\n"
        "show(part, 'linear')"
    )

    result = json.loads(
        inspect_part(
            session,
            "linear",
            expected=json.dumps(
                {
                    "patterns": [
                        {
                            "type": "linear_array",
                            "count": 1,
                            "pitch": 10,
                            "direction": [1, 0, 0],
                            "member_count": 4,
                            "member_diameter": 4,
                        }
                    ]
                }
            ),
        )
    )

    assert result["status"] == "PASS"
    assert result["patterns"]["groups"][0]["member_count"] == 4


def test_inspect_part_wrong_pattern_type_is_a_failure_not_an_exception(session):
    session.execute(
        "import math\n"
        "part = Box(60, 60, 8)\n"
        "for i in range(4):\n"
        "    a = math.radians(90 * i)\n"
        "    part -= Pos(20 * math.cos(a), 20 * math.sin(a), 0) * Cylinder(2, 12)\n"
        "show(part, 'circle')"
    )

    result = json.loads(
        inspect_part(
            session,
            "circle",
            expected=json.dumps(
                {"patterns": [{"type": "linear_array", "direction": [1, 0, 0], "count": 1}]}
            ),
        )
    )

    assert result["status"] == "FAIL"
    assert any("unexpected pattern group" in mismatch for mismatch in result["mismatches"])


def test_inspect_part_rejects_unexpected_groups(session):
    session.execute(
        "part = Box(50, 30, 8)\n"
        "part -= Pos(-12, 0, 0) * Cylinder(2, 12)\n"
        "part -= Pos(0, 0, 0) * Cylinder(2, 12)\n"
        "part -= Pos(12, 0, 0) * Cylinder(3, 12)\n"
        "show(part, 'extra_hole')"
    )

    extra = json.loads(
        inspect_part(
            session,
            "extra_hole",
            expected=json.dumps({"holes": [{"count": 2, "diameter": 4}]}),
        )
    )
    assert extra["status"] == "FAIL"
    assert any("unexpected hole group" in mismatch for mismatch in extra["mismatches"])


def test_underspecified_expectation_cannot_absorb_distinct_axis_groups():
    actual = [
        {"count": 2, "diameter": 6.0, "axis": [0.0, 0.0, 1.0]},
        {"count": 2, "diameter": 6.0, "axis": [1.0, 0.0, 0.0]},
    ]

    mismatches = _check_expected_groups(
        "hole", actual, [{"count": 4, "diameter": 6.0}], tolerance=0.1
    )

    assert any("underspecified hole expectation" in mismatch for mismatch in mismatches)


def test_inspect_part_rejects_non_object_expectations(session):
    session.execute("show(Box(10, 10, 10), 'box')")

    with pytest.raises(ValueError, match="expected must be a JSON object"):
        inspect_part(session, "box", expected="[]")


@pytest.mark.parametrize(
    ("expected", "message"),
    [
        ({}, "at least one"),
        ({"tolerance": 0.1}, "at least one"),
        ({"total_holes": 4}, "unsupported key"),
        ({"section_varying": "false"}, "must be a boolean"),
        ({"tolerance": -0.1}, "non-negative"),
        ({"bbox": [1, 2]}, "3-number"),
        ({"holes": {"count": 2}}, "must be a JSON array"),
        ({"holes": [{"count": 2, "radius": 3}]}, "unsupported key"),
        ({"patterns": [{"pitch": "ten"}]}, "finite number"),
        (
            {"holes": [{"count": 2, "diameter": 6}, {"count": 2, "diameter": 6}]},
            "overlaps",
        ),
        (
            {"holes": [{"count": 2, "diameter": 6}, {"count": 2, "axis": [0, 0, 1]}]},
            "overlaps",
        ),
    ],
)
def test_inspect_part_rejects_malformed_expectation_schema(session, expected, message):
    session.execute("show(Box(10, 10, 10), 'box')")

    with pytest.raises(ValueError, match=message):
        inspect_part(session, "box", expected=json.dumps(expected))


def test_inspect_part_wraps_malformed_json(session):
    session.execute("show(Box(10, 10, 10), 'box')")

    with pytest.raises(ValueError, match="expected must be valid JSON"):
        inspect_part(session, "box", expected="{bad")


def test_inspect_part_is_dispatched_by_bounded_shape_runner(monkeypatch):
    import build123d_mcp.tools.inspect_part as inspect_module

    shape = object()
    params = {
        "object_name": "part",
        "section_axis": "Y",
        "section_slices": 9,
        "expectation": {"solid_count": 1},
    }

    monkeypatch.setattr(
        inspect_module,
        "_inspect_part_report",
        lambda *args: repr(args),
    )

    result = _run("inspect_part", {"": shape}, params)

    assert result == repr((shape, "part", "Y", 9, {"solid_count": 1}))


def test_inspect_part_round_trips_through_real_bounded_subprocess(session, monkeypatch):
    session.execute(
        "import math\n"
        "part = Box(100, 100, 20)\n"
        "part -= Cylinder(5, 24)\n"
        "part -= Pos(0, 0, 7) * Cylinder(9, 6)\n"
        "part -= Pos(-35, 35, 4) * Cylinder(3, 8)\n"
        "for i in range(4):\n"
        "    a = math.radians(90 * i)\n"
        "    part -= Pos(30 * math.cos(a), 30 * math.sin(a), 0) * Cylinder(2, 24)\n"
        "part += Pos(35, 35, 9) * Cylinder(6, 8)\n"
        "show(part, 'featured')"
    )
    baseline = json.loads(inspect_part(session, "featured", section_slices=5))
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    round_tripped = json.loads(inspect_part(session, "featured", section_slices=5))

    for key in ("bbox", "topology", "holes", "bosses", "patterns", "sections", "warnings"):
        assert round_tripped[key] == baseline[key], key
