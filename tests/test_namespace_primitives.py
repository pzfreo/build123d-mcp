"""In-namespace analysis primitives (#366): measure/clearance/cross_sections/find_holes
are callable INSIDE execute() and return real Python objects, so an agent composes over
results (filter, arithmetic) instead of hand-copying numbers out of a JSON tool result.
"""

import pytest

from build123d_mcp.session import Session, _bounded_result


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def test_measure_primitive_returns_composable_dict(session):
    session.execute("part = Box(10, 20, 30)")
    out = session.execute(
        "vol = measure(part)['volume']\nfaces = measure(part)['topology']['faces']"
    )
    assert "Error" not in out
    assert session.namespace["vol"] == 6000.0
    assert session.namespace["faces"] == 6


def test_measure_defaults_to_current_shape(session):
    session.execute("show(Box(10, 10, 10), 'b')")
    session.execute("v = measure()['volume']")
    assert session.namespace["v"] == 1000.0


def test_clearance_primitive_composes(session):
    out = session.execute(
        "g = clearance(Box(30, 30, 30), Box(5, 5, 5))\nfits = g['status'] == 'containing'"
    )
    assert "Error" not in out
    assert session.namespace["fits"] is True
    assert session.namespace["g"]["clearance"] > 0


def test_cross_sections_primitive_returns_list(session):
    session.execute("cs = cross_sections(Box(10, 10, 10), num_slices=5)")
    cs = session.namespace["cs"]
    assert isinstance(cs, list) and len(cs) == 5
    assert "area" in cs[0] and "position" in cs[0]


def test_find_holes_primitive_returns_filterable_records(session):
    session.execute("part = Box(20, 20, 20) - Cylinder(3, 25)")  # a through hole
    session.execute("holes = find_holes(part)\nn = len(holes)")
    assert session.namespace["n"] == 1
    # real recogniser records, not JSON strings/dicts of rounded values
    assert not isinstance(session.namespace["holes"][0], (str, dict))


def test_missing_shape_raises_not_silently_wrong(session):
    # No shape and no current shape → a clear error, not a crash on None.
    out = session.execute("m = measure()")
    assert "no shape" in out.lower()


def test_injected_primitives_survive_a_failed_execute(session):
    session.execute("x = measure(Box(1, 1, 1))['volume']")
    session.execute("this is not valid python (")  # SyntaxError
    out = session.execute("y = measure(Box(2, 2, 2))['volume']")
    assert "Error" not in out and session.namespace["y"] == 8.0


def test_primitives_use_bounded_path_for_large_shapes(session, monkeypatch):
    # Forcing the size gate low routes the primitive through the real out-of-process
    # bounded path (run_bounded_shape_op) — it must still return a parsed dict, proving
    # a large shape can't SIGKILL the session via an in-namespace call (#360).
    from build123d_mcp.tools import _bounded

    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    session.execute("m = measure(Box(10, 10, 10))")
    assert session.namespace["m"]["volume"] == 1000.0


def test_bounded_result_parses_json_and_raises_on_error():
    assert _bounded_result('{"volume": 5}') == {"volume": 5}
    assert _bounded_result("[1, 2, 3]") == [1, 2, 3]
    with pytest.raises(RuntimeError, match="exceeded"):
        _bounded_result("Error: measure() exceeded the 60s time budget")
