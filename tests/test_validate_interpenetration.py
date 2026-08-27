"""Tests for the validity gate's interpenetration check (#453).

A solid count alone cannot separate genuinely disjoint bodies from
interpenetrating ones — both are watertight, both pass the B-rep and mesh
checks, and ``shape.volume`` sums them. The gate used to call both cases
"disjoint" and report the summed volume, so an overlapping pair exported to
STEP carried more material than the part had, with nothing flagging it.
"""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.validate import validate


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def _report(session, name):
    raw = validate(session, object_name=name)
    return json.loads(raw[raw.find("{") :])


def _warnings(report):
    return " ".join(report["warnings"])


# Two 20-cubes offset by 10 overlap by 10x20x20 = 4000; summed volume 16000,
# true fused volume 12000 — a 33.3% overstatement.
_OVERLAP = """
a = Box(20, 20, 20)
b = Box(20, 20, 20).locate(Location((10, 0, 0)))
show(Compound(children=[a, b]), 'overlap')
"""

_DISJOINT = """
c = Box(20, 20, 20)
d = Box(20, 20, 20).locate(Location((40, 0, 0)))
show(Compound(children=[c, d]), 'disjoint')
"""

# Offset by exactly the edge length: a shared face, zero shared material.
_TOUCHING = """
e = Box(20, 20, 20)
f = Box(20, 20, 20).locate(Location((20, 0, 0)))
show(Compound(children=[e, f]), 'touching')
"""


def test_interpenetrating_pair_is_detected(session):
    session.execute(_OVERLAP)
    report = _report(session, "overlap")
    assert report["interpenetrating_pairs"] == 1
    assert report["pairwise_overlap_volume"] == pytest.approx(4000.0, abs=1e-3)
    assert report["overlap_check"] == "exact"


def test_interpenetrating_pair_is_not_called_disjoint(session):
    """The reported defect: the advisory asserted 'disjoint' for bodies that
    interpenetrate."""
    session.execute(_OVERLAP)
    text = _warnings(_report(session, "overlap"))
    assert "INTERPENETRATING" in text
    assert "NOT disjoint" in text


def test_warning_surfaces_the_double_counted_volume(session):
    """The summed volume is the corrupted number, so the overlap it
    double-counts has to be visible next to it. With a single overlapping pair
    the excess IS the intersection, so the warning states the true volume."""
    session.execute(_OVERLAP)
    report = _report(session, "overlap")
    text = _warnings(report)
    assert "4000.0" in text
    assert str(report["volume"]) in text
    assert report["volume"] - report["pairwise_overlap_volume"] == pytest.approx(12000.0, abs=1e-3)
    assert "12000.0" in text


def test_three_way_overlap_does_not_promise_a_subtraction(session):
    """A region shared by three bodies appears in all three pairwise
    intersections, so the pairwise total over-counts the excess. Reporting it as
    the amount to subtract would swap one wrong number for another: summed 24000
    minus pairwise 12480 gives 11520 against a true fused 14400."""
    session.execute("""
a = Box(20, 20, 20)
b = Box(20, 20, 20).locate(Location((8, 0, 0)))
c = Box(20, 20, 20).locate(Location((4, 8, 0)))
show(Compound(children=[a, b, c]), 'triple')
""")
    report = _report(session, "triple")
    assert report["interpenetrating_pairs"] == 3
    text = _warnings(report)
    assert "NOT by the pairwise total" in text
    # The naive subtraction must not appear as if it were the answer.
    naive = round(report["volume"] - report["pairwise_overlap_volume"], 4)
    assert str(naive) not in text


def test_overlap_and_disjoint_are_no_longer_identical(session):
    """The control test from the issue: the two cases produced byte-identical
    reports."""
    session.execute(_OVERLAP)
    session.execute(_DISJOINT)
    over = _report(session, "overlap")
    apart = _report(session, "disjoint")
    assert over["n_solids"] == apart["n_solids"]
    assert over["volume"] == apart["volume"]
    assert over != apart
    assert over["warnings"] != apart["warnings"]


def test_genuinely_disjoint_bodies_still_say_disjoint(session):
    """The existing advisory must survive — and now it is proven rather than
    assumed from the body count."""
    session.execute(_DISJOINT)
    report = _report(session, "disjoint")
    assert report["interpenetrating_pairs"] == 0
    assert report["overlap_check"] == "exact"
    assert "disjoint solid bodies" in _warnings(report)
    assert "INTERPENETRATING" not in _warnings(report)


def test_touching_bodies_are_not_interpenetrating(session):
    """A shared face is zero shared material — not an overlap."""
    session.execute(_TOUCHING)
    report = _report(session, "touching")
    assert report["interpenetrating_pairs"] == 0
    assert report["pairwise_overlap_volume"] == 0.0


def test_single_solid_reports_no_overlap_and_no_advisory(session):
    session.execute("show(Box(20, 20, 20), 'single')")
    report = _report(session, "single")
    assert report["interpenetrating_pairs"] == 0
    assert report["pairwise_overlap_volume"] == 0.0
    assert "INTERPENETRATING" not in _warnings(report)
    assert "disjoint" not in _warnings(report)


def test_fusing_the_overlap_clears_the_advisory(session):
    """The remedy the warning recommends actually resolves it."""
    session.execute(
        "show(Box(20, 20, 20).fuse(Box(20, 20, 20).locate(Location((10, 0, 0)))), 'fused')"
    )
    report = _report(session, "fused")
    assert report["n_solids"] == 1
    assert report["volume"] == pytest.approx(12000.0, abs=1e-3)
    assert report["interpenetrating_pairs"] == 0


def test_gate_verdict_is_unchanged_for_multi_body_shapes(session):
    """Interpenetration is reported, not failed: a deliberate interference fit
    in an assembly export is legitimate geometry."""
    session.execute(_OVERLAP)
    session.execute(_DISJOINT)
    assert _report(session, "overlap")["passes_gate"] is True
    assert _report(session, "disjoint")["passes_gate"] is True
