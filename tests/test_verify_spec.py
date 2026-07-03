"""verify_spec() checks the built solid against a declared design-intent spec."""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.verify_spec import verify_spec


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def _run(session, program: str, spec: dict, **kwargs) -> dict:
    session.execute(program)
    return json.loads(verify_spec(session, spec=json.dumps(spec), **kwargs))


# A plate (80×60×8, centred) with a 4-hole bolt circle at Ø40 of Ø6.6 holes.
_PLATE = (
    "plate_thickness = 8.0\n"
    "with BuildPart() as p:\n"
    "    Box(80, 60, plate_thickness)\n"
    "    with PolarLocations(20, 4):\n"
    "        Hole(3.3)\n"
    "result = p.part\n"
    "show(result, 'part')\n"
)


def test_conforming_design_passes(session):
    r = _run(
        session,
        _PLATE,
        {
            "envelope_mm": {"x": [0, 100], "y": [0, 80], "z": [0, 20]},
            "solid": {"count": 1, "valid": True},
            "features": [
                {
                    "kind": "hole_pattern",
                    "pattern": "bolt_circle",
                    "holes": 4,
                    "bcd_mm": 40,
                    "diameter_mm": 6.6,
                }
            ],
            "parameters": [{"name": "plate_thickness", "min": 4, "max": 12}],
        },
    )
    assert r["summary"]["conforms"] is True
    assert r["summary"]["fail"] == 0
    assert all(e["status"] == "PASS" for e in r["conformance"])


def test_missing_hole_fails_recognised(session):
    # spec asks for 6 holes; only 4 exist → FAIL at the recognised tier
    r = _run(
        session,
        _PLATE,
        {
            "features": [
                {
                    "kind": "hole_pattern",
                    "pattern": "bolt_circle",
                    "holes": 6,
                    "bcd_mm": 40,
                    "diameter_mm": 6.6,
                }
            ]
        },
    )
    e = r["conformance"][0]
    assert e["status"] == "FAIL" and e["tier"] == "recognised"
    assert r["summary"]["conforms"] is False


def test_envelope_bust_fails_measured(session):
    r = _run(session, _PLATE, {"envelope_mm": {"x": [0, 50]}})  # part is 80 wide
    e = r["conformance"][0]
    assert e["status"] == "FAIL" and e["tier"] == "measured" and e["actual"] == 80.0


def test_2d_sketch_fails_structural(session):
    r = _run(session, "show(Rectangle(10, 10), 'flat')\n", {"solid": {"count": 1, "valid": True}})
    tiers = {e["tier"]: e for e in r["conformance"]}
    assert tiers["structural"]["status"] == "FAIL"
    assert r["summary"]["conforms"] is False


def test_parameter_out_of_range_fails(session):
    r = _run(session, _PLATE, {"parameters": [{"name": "plate_thickness", "min": 10, "max": 20}]})
    assert r["conformance"][0]["status"] == "FAIL"
    assert r["conformance"][0]["actual"] == 8.0


def test_unverifiable_target_is_unverified_not_failed(session):
    r = _run(
        session,
        _PLATE,
        {
            "solid": {"count": 1, "valid": True},
            "targets": [{"name": "fatigue_life", "verifiable": False}],
        },
    )
    tgt = next(e for e in r["conformance"] if e["requirement"].startswith("target"))
    assert tgt["status"] == "UNVERIFIED" and tgt["tier"] == "unverified"
    # UNVERIFIED does not flip conforms — the solid requirement passed
    assert r["summary"]["conforms"] is True and r["summary"]["unverified"] == 1


def test_unrecognised_feature_kind_is_unverified_not_failed(session):
    # An unrecognised feature kind is UNVERIFIED (not a false FAIL); and since it is
    # the only requirement, nothing was actually checked → conforms is False (not a
    # vacuous True).
    r = _run(session, _PLATE, {"features": [{"kind": "spline_pocket"}]})
    assert r["conformance"][0]["status"] == "UNVERIFIED"
    assert r["summary"]["fail"] == 0
    assert r["summary"]["checked"] == 0
    assert r["summary"]["conforms"] is False


def test_empty_or_all_unverified_spec_does_not_falsely_conform(session):
    # All keys unrecognised → nothing checked → conforms must be False, with a warning.
    r = _run(session, _PLATE, {"typo_envelope": {"x": [0, 100]}})
    assert r["summary"]["checked"] == 0
    assert r["summary"]["conforms"] is False
    assert "no geometry-checkable requirements" in r["note"].lower()


def test_malformed_spec_fields_return_clean_error(session):
    session.execute(_PLATE)
    for bad in (
        {"envelope_mm": {"x": 100}},  # axis must be [lo, hi]
        {"features": {"kind": "hole"}},  # must be a list
        {"volume_mm3": [0, 100]},  # must be an object
        {"solid": "yes"},  # must be an object
        {"parameters": [{"min": 1}]},  # entry needs a name
    ):
        r = json.loads(verify_spec(session, spec=json.dumps(bad)))
        assert "error" in r and "spec" in r["error"].lower(), bad


_THIN_TUBE = "show((Pos(0, 0, 10) * Cylinder(10, 20)) - (Pos(0, 0, 10) * Cylinder(9.7, 20)), 'p')\n"


def test_min_wall_below_threshold_fails_measured(session):
    # 0.3 mm tube wall must fail a 2 mm minimum, at the measured tier.
    r = _run(session, _THIN_TUBE, {"min_wall_mm": 2.0})
    e = r["conformance"][0]
    assert e["status"] == "FAIL" and e["tier"] == "measured"
    assert e["actual"] == pytest.approx(0.3, abs=0.02)


def test_min_wall_met_passes(session):
    r = _run(session, _THIN_TUBE, {"min_wall_mm": 0.2})
    assert r["conformance"][0]["status"] == "PASS"


def test_wall_thickness_at_in_range_passes(session):
    # _PLATE is 8 mm thick (z ∈ [-4, 4]); probe through it.
    r = _run(
        session,
        _PLATE,
        {
            "features": [
                {
                    "kind": "wall_thickness_at",
                    "point": [0, 0, 0],
                    "direction": [0, 0, 1],
                    "expect_mm": [7, 9],
                }
            ]
        },
    )
    e = r["conformance"][0]
    assert e["status"] == "PASS" and e["tier"] == "measured"
    assert e["actual"] == pytest.approx(8.0, abs=0.02)


def test_wall_thickness_at_out_of_range_fails(session):
    r = _run(
        session,
        _PLATE,
        {
            "features": [
                {
                    "kind": "wall_thickness_at",
                    "point": [0, 0, 0],
                    "direction": [0, 0, 1],
                    "expect_mm": [1, 2],
                }
            ]
        },
    )
    assert r["conformance"][0]["status"] == "FAIL"


def test_wall_thickness_at_no_wall_is_unverified(session):
    # a point in no wall abstains (augura returns None) — UNVERIFIED, not a false FAIL.
    r = _run(
        session,
        _PLATE,
        {
            "features": [
                {
                    "kind": "wall_thickness_at",
                    "point": [500, 0, 0],
                    "direction": [0, 0, 1],
                    "expect_mm": [7, 9],
                }
            ]
        },
    )
    assert r["conformance"][0]["status"] == "UNVERIFIED"


def test_wall_thickness_at_malformed_clean_error(session):
    session.execute(_PLATE)
    for bad in (
        {"point": [0, 0], "direction": [0, 0, 1], "expect_mm": [1, 2]},
        {"point": [0, 0, 0], "direction": [0, 0, 1], "expect_mm": [1]},
    ):
        r = json.loads(
            verify_spec(
                session, spec=json.dumps({"features": [{"kind": "wall_thickness_at", **bad}]})
            )
        )
        assert "error" in r, bad


def test_spec_from_file_path(session, tmp_path):
    session.execute(_PLATE)
    spec_file = tmp_path / "design.spec.json"
    spec_file.write_text(json.dumps({"solid": {"count": 1, "valid": True}}))
    r = json.loads(verify_spec(session, spec_path=str(spec_file)))
    assert r["summary"]["conforms"] is True


_CBORE = (
    "with BuildPart() as p:\n"
    "    Box(60, 30, 12)\n"
    "    with Locations((-15, 0), (15, 0)):\n"
    "        CounterBoreHole(radius=3, counter_bore_radius=5, counter_bore_depth=3)\n"
    "result = p.part\n"
    "show(result, 'p')\n"
)
_LINEAR = (
    "with BuildPart() as p:\n"
    "    Box(90, 20, 10)\n"
    "    with GridLocations(20, 0, 4, 1):\n"
    "        Hole(2.5)\n"
    "result = p.part\n"
    "show(result, 'p')\n"
)


def test_hole_counterbore_and_through_are_checked(session):
    r = _run(
        session,
        _CBORE,
        {
            "features": [
                {
                    "kind": "hole",
                    "count": 2,
                    "diameter_mm": 6.0,
                    "through": True,
                    "counterbore": {"diameter_mm": 10.0},
                }
            ]
        },
    )
    e = r["conformance"][0]
    assert e["status"] == "PASS" and e["found"] == 2
    assert "counterbore" in e["requirement"] and "through" in e["requirement"]


def test_hole_wrong_counterbore_diameter_fails(session):
    r = _run(
        session,
        _CBORE,
        {
            "features": [
                {
                    "kind": "hole",
                    "count": 2,
                    "diameter_mm": 6.0,
                    "counterbore": {"diameter_mm": 20.0},
                }
            ]
        },  # real cbore is Ø10
    )
    assert r["conformance"][0]["status"] == "FAIL"


def test_blind_vs_through_distinguished(session):
    # these holes are through; asking for blind must FAIL
    r = _run(session, _CBORE, {"features": [{"kind": "hole", "count": 2, "through": False}]})
    assert r["conformance"][0]["status"] == "FAIL"


_PLAIN_HOLES = (
    "with BuildPart() as p:\n"
    "    Box(60, 30, 12)\n"
    "    with Locations((-15, 0), (15, 0)):\n"
    "        Hole(3)\n"
    "result = p.part\n"
    "show(result, 'p')\n"
)


def test_counterbore_false_means_absent(session):
    # `counterbore: false` asserts NO counterbore (symmetric with through:false).
    plain = _run(
        session, _PLAIN_HOLES, {"features": [{"kind": "hole", "count": 2, "counterbore": False}]}
    )
    assert plain["conformance"][0]["status"] == "PASS"


def test_counterbore_false_fails_when_present(session):
    cbored = _run(
        session, _CBORE, {"features": [{"kind": "hole", "count": 2, "counterbore": False}]}
    )
    assert cbored["conformance"][0]["status"] == "FAIL"


def test_non_numeric_feature_field_is_clean_error(session):
    session.execute(_PLAIN_HOLES)
    r = json.loads(
        verify_spec(session, spec=json.dumps({"features": [{"kind": "hole", "diameter_mm": "6"}]}))
    )
    assert "error" in r and "diameter_mm" in r["error"]


def test_linear_array_pattern_checked(session):
    r = _run(
        session,
        _LINEAR,
        {
            "features": [
                {
                    "kind": "hole_pattern",
                    "pattern": "linear_array",
                    "holes": 4,
                    "pitch_mm": 20.0,
                    "diameter_mm": 5.0,
                }
            ]
        },
    )
    e = r["conformance"][0]
    assert e["status"] == "PASS" and e["found"]["pitch"] == 20.0


def test_malformed_counterbore_is_clean_error(session):
    session.execute(_CBORE)
    r = json.loads(
        verify_spec(
            session, spec=json.dumps({"features": [{"kind": "hole", "counterbore": [1, 2]}]})
        )
    )
    assert "error" in r and "counterbore" in r["error"]


def test_missing_and_malformed_spec_error(session):
    session.execute(_PLATE)
    assert "error" in json.loads(verify_spec(session))  # neither spec nor spec_path
    assert "error" in json.loads(verify_spec(session, spec="{not json"))
    assert "error" in json.loads(verify_spec(session, spec="[1,2,3]"))  # not an object


_CSK_PART = (
    "with BuildPart() as p:\n"
    "    Box(60, 40, 12)\n"
    "    top = p.faces().sort_by(Axis.Z)[-1]\n"
    "    with Locations(top):\n"
    "        with Locations((-15, 0), (15, 0)):\n"
    "            CounterSinkHole(radius=3, counter_sink_radius=6, counter_sink_angle=82)\n"
    "result = p.part\n"
    "show(result, 'p')\n"
)


def test_countersink_feature_conforms(session):
    r = _run(
        session,
        _CSK_PART,
        {
            "features": [
                {
                    "kind": "countersink",
                    "count": 2,
                    "major_diameter_mm": 12.0,
                    "drill_diameter_mm": 6.0,
                    "included_angle_deg": 82,
                }
            ]
        },
    )
    e = r["conformance"][0]
    assert e["status"] == "PASS" and e["found"] == 2 and e["tier"] == "recognised"


def test_countersink_wrong_angle_fails(session):
    r = _run(session, _CSK_PART, {"features": [{"kind": "countersink", "included_angle_deg": 90}]})
    assert r["conformance"][0]["status"] == "FAIL"


def test_countersink_absent_fails(session):
    # a plain-hole part has no countersinks → a countersink requirement FAILs
    r = _run(session, _PLAIN_HOLES, {"features": [{"kind": "countersink"}]})
    assert r["conformance"][0]["status"] == "FAIL" and r["conformance"][0]["found"] == 0


def test_countersink_non_numeric_field_clean_error(session):
    session.execute(_CSK_PART)
    r = json.loads(
        verify_spec(
            session,
            spec=json.dumps({"features": [{"kind": "countersink", "included_angle_deg": "82"}]}),
        )
    )
    assert "error" in r and "included_angle_deg" in r["error"]


# A plate (40×40×10) with a Ø8 boss protruding at x=+20 (material ADDED there).
_BOSS_AT_X = (
    "b = Box(40, 40, 10)\n"
    "b = b + Cylinder(4, 20).rotate(Axis.Y, 90).translate((20, 0, 0))\n"
    "show(b, 'p')\n"
)
_PLATE_PLAIN = "show(Box(40, 40, 10), 'p')\n"


def test_material_at_point_discriminates_add_vs_remove(session):
    # the point sits where a boss adds material but a plain plate is empty
    spec = {"features": [{"kind": "material_at_point", "point": [22, 0, 0], "expect": "solid"}]}
    added = _run(session, _BOSS_AT_X, spec)["conformance"][0]
    assert added["status"] == "PASS" and added["actual"] == "solid" and added["tier"] == "measured"
    session2 = Session()
    session2.execute("from build123d import *")
    empty = _run(session2, _PLATE_PLAIN, spec)["conformance"][0]
    assert empty["status"] == "FAIL" and empty["actual"] == "void"


def test_material_at_point_void_expectation(session):
    # a point outside the plate but inside its bbox extent along z is void
    r = _run(
        session,
        _PLATE_PLAIN,
        {"features": [{"kind": "material_at_point", "point": [22, 0, 0], "expect": "void"}]},
    )
    assert r["conformance"][0]["status"] == "PASS"
    assert r["summary"]["conforms"] is True  # a measured PASS counts as checked


def test_material_at_point_on_2d_sketch_is_unverified(session):
    r = _run(
        session,
        "show(Rectangle(10, 10), 'flat')\n",
        {"features": [{"kind": "material_at_point", "point": [0, 0, 0], "expect": "solid"}]},
    )
    assert r["conformance"][0]["status"] == "UNVERIFIED"


def test_material_at_point_vacuous_void_warns(session):
    r = _run(
        session,
        _PLATE_PLAIN,
        {"features": [{"kind": "material_at_point", "point": [500, 0, 0], "expect": "void"}]},
    )
    e = r["conformance"][0]
    assert e["status"] == "PASS" and "hint" in e and "vacuous" in e["hint"]


def test_material_at_point_malformed_clean_error(session):
    session.execute(_PLATE_PLAIN)
    for bad in ({"point": [1, 2]}, {"point": "0,0,0"}, {"point": [0, 0, 0], "expect": "maybe"}):
        r = json.loads(
            verify_spec(
                session, spec=json.dumps({"features": [{"kind": "material_at_point", **bad}]})
            )
        )
        assert "error" in r, bad
