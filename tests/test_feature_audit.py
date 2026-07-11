"""Prototype coverage for the compact generation checkpoint audit (#417)."""

import json

import pytest

from build123d_mcp._shape_op_subprocess import _run
from build123d_mcp.session import Session
from build123d_mcp.tools import _bounded
from build123d_mcp.tools.feature_audit import feature_audit


@pytest.fixture
def session():
    value = Session()
    value.execute("from build123d import *")
    return value


def test_feature_audit_groups_holes_bosses_and_bolt_circle(session):
    session.execute(
        "import math\n"
        "part = Box(80, 80, 8)\n"
        "for i in range(4):\n"
        "    a = math.radians(90 * i)\n"
        "    part -= Pos(25 * math.cos(a), 25 * math.sin(a), 0) * Cylinder(3, 12)\n"
        "part += Pos(0, 0, 8) * Cylinder(8, 8)\n"
        "show(part, 'checkpoint')"
    )

    result = json.loads(feature_audit(session, "checkpoint", section_slices=5))

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
        feature_audit(
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


def test_feature_audit_expectations_pass_and_fail(session):
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

    passing = json.loads(feature_audit(session, "plate", expected=json.dumps(expected)))
    assert passing["status"] == "PASS"
    assert passing["passes_expectations"] is True

    expected["holes"][0]["count"] = 3
    failing = json.loads(feature_audit(session, "plate", expected=json.dumps(expected)))
    assert failing["status"] == "FAIL"
    assert failing["passes_expectations"] is False
    assert "expected 3 hole feature(s)" in failing["mismatches"][0]


def test_feature_audit_cored_profile_reports_section_variation(session):
    session.execute(
        "outer = Box(40, 40, 20)\n"
        "cavity = Pos(0, 0, 5) * Box(30, 30, 12)\n"
        "show(outer - cavity, 'cored')"
    )

    result = json.loads(
        feature_audit(
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


def test_feature_audit_thin_wall_reports_constant_section_profile(session):
    session.execute(
        "outer = Box(40, 40, 20)\ninner = Box(36, 36, 24)\nshow(outer - inner, 'thin_wall')"
    )

    result = json.loads(
        feature_audit(
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


def test_feature_audit_linear_pattern_preserves_and_checks_relationship(session):
    session.execute(
        "part = Box(60, 30, 8)\n"
        "for x in (-15, -5, 5, 15):\n"
        "    part -= Pos(x, 0, 0) * Cylinder(2, 12)\n"
        "show(part, 'linear')"
    )

    result = json.loads(
        feature_audit(
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


def test_feature_audit_wrong_pattern_type_is_a_failure_not_an_exception(session):
    session.execute(
        "import math\n"
        "part = Box(60, 60, 8)\n"
        "for i in range(4):\n"
        "    a = math.radians(90 * i)\n"
        "    part -= Pos(20 * math.cos(a), 20 * math.sin(a), 0) * Cylinder(2, 12)\n"
        "show(part, 'circle')"
    )

    result = json.loads(
        feature_audit(
            session,
            "circle",
            expected=json.dumps(
                {"patterns": [{"type": "linear_array", "direction": [1, 0, 0], "count": 1}]}
            ),
        )
    )

    assert result["status"] == "FAIL"
    assert any("unexpected pattern group" in mismatch for mismatch in result["mismatches"])


def test_feature_audit_rejects_unexpected_and_ambiguously_matched_groups(session):
    session.execute(
        "part = Box(50, 30, 8)\n"
        "part -= Pos(-12, 0, 0) * Cylinder(2, 12)\n"
        "part -= Pos(0, 0, 0) * Cylinder(2, 12)\n"
        "part -= Pos(12, 0, 0) * Cylinder(3, 12)\n"
        "show(part, 'extra_hole')"
    )

    extra = json.loads(
        feature_audit(
            session,
            "extra_hole",
            expected=json.dumps({"holes": [{"count": 2, "diameter": 4}]}),
        )
    )
    assert extra["status"] == "FAIL"
    assert any("unexpected hole group" in mismatch for mismatch in extra["mismatches"])

    ambiguous = json.loads(
        feature_audit(
            session,
            "extra_hole",
            expected=json.dumps(
                {
                    "holes": [
                        {"count": 2, "diameter": 4},
                        {"count": 2, "axis": [0, 0, 1]},
                        {"count": 1, "diameter": 6},
                    ]
                }
            ),
        )
    )
    assert ambiguous["status"] == "FAIL"
    assert any("ambiguous hole group" in mismatch for mismatch in ambiguous["mismatches"])


def test_feature_audit_rejects_non_object_expectations(session):
    session.execute("show(Box(10, 10, 10), 'box')")

    with pytest.raises(ValueError, match="expected must be a JSON object"):
        feature_audit(session, "box", expected="[]")


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
    ],
)
def test_feature_audit_rejects_malformed_expectation_schema(session, expected, message):
    session.execute("show(Box(10, 10, 10), 'box')")

    with pytest.raises(ValueError, match=message):
        feature_audit(session, "box", expected=json.dumps(expected))


def test_feature_audit_is_dispatched_by_bounded_shape_runner(monkeypatch):
    import build123d_mcp.tools.feature_audit as audit_module

    shape = object()
    params = {
        "object_name": "part",
        "section_axis": "Y",
        "section_slices": 9,
        "expectation": {"solid_count": 1},
    }

    monkeypatch.setattr(
        audit_module,
        "_feature_audit_report",
        lambda *args: repr(args),
    )

    result = _run("feature_audit", {"": shape}, params)

    assert result == repr((shape, "part", "Y", 9, {"solid_count": 1}))


def test_feature_audit_round_trips_through_real_bounded_subprocess(session, monkeypatch):
    session.execute("show(Box(12, 10, 8), 'box')")
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)

    result = json.loads(
        feature_audit(
            session,
            "box",
            section_slices=3,
            expected=json.dumps({"bbox": [12, 10, 8], "solid_count": 1}),
        )
    )

    assert result["status"] == "PASS"
    assert result["bbox"] == {"x": 12.0, "y": 10.0, "z": 8.0}
