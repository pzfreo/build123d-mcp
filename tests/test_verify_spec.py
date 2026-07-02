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


def test_min_wall_is_deferred_unverified(session):
    r = _run(session, _PLATE, {"min_wall_mm": 2.0})
    assert r["conformance"][0]["status"] == "UNVERIFIED"
    assert "deferred" in r["conformance"][0]["note"]


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
