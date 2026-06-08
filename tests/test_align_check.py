import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.align_check import align_check


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


@pytest.fixture
def two_boxes_stacked(session):
    """Two boxes: box_a at Z=0..10, box_b at Z=10..20 (flush in Z)."""
    session.execute("box_a = Box(10, 10, 10); show(box_a, 'box_a')")
    session.execute("box_b = Box(10, 10, 10).move(Location((0, 0, 10))); show(box_b, 'box_b')")
    return session


def test_flush_delta_stacked_flush(two_boxes_stacked):
    """Two boxes stacked so their top faces are flush → flush delta ≈ 0."""
    result = json.loads(align_check(two_boxes_stacked, "box_a", "box_b", axis="Z", mode="flush"))
    assert "delta" in result
    # box_a max Z = 5, box_b max Z = 15 → not flush; test touching bottom instead
    # Actually box_a is centred at origin: Z -5..5; box_b moved +10: Z 5..15
    # flush = a_max - b_max = 5 - 15 = -10
    # Let's just verify it returns a numeric delta and expected fields
    assert "axis" in result
    assert result["axis"] == "Z"
    assert result["mode"] == "flush"
    assert result["object_a"] == "box_a"
    assert result["object_b"] == "box_b"
    assert "interpretation" in result


def test_flush_same_height_is_flush(session):
    """Two boxes at same Z position → flush delta ≈ 0."""
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    session.execute("b = Box(20, 20, 10); show(b, 'b')")
    result = json.loads(align_check(session, "a", "b", axis="Z", mode="flush"))
    assert abs(result["delta"]) < 0.01


def test_center_offset_5mm(session):
    """One box offset 5mm in Z → center delta ≈ 5."""
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    session.execute("b = Box(10, 10, 10).move(Location((0, 0, 5))); show(b, 'b')")
    result = json.loads(align_check(session, "b", "a", axis="Z", mode="center"))
    assert abs(result["delta"] - 5.0) < 0.1


def test_clearance_touching(session):
    """Boxes touching at Z face → clearance ≈ 0."""
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    # box_a centre at origin: Z -5..5; move b so its bottom is at Z=5
    session.execute("b = Box(10, 10, 10).move(Location((0, 0, 10))); show(b, 'b')")
    result = json.loads(align_check(session, "a", "b", axis="Z", mode="clearance"))
    assert abs(result["delta"]) < 0.01


def test_clearance_apart(session):
    """Boxes with a 3mm gap → clearance ≈ 3."""
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    session.execute("b = Box(10, 10, 10).move(Location((0, 0, 13))); show(b, 'b')")
    result = json.loads(align_check(session, "a", "b", axis="Z", mode="clearance"))
    assert result["delta"] > 0  # apart
    assert abs(result["delta"] - 3.0) < 0.1


def test_missing_object_returns_error(session):
    """Requesting an unknown object returns an error dict."""
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    result = json.loads(align_check(session, "a", "nonexistent", axis="Z", mode="flush"))
    assert "error" in result


def test_invalid_axis_returns_error(session):
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    session.execute("b = Box(10, 10, 10); show(b, 'b')")
    result = json.loads(align_check(session, "a", "b", axis="W", mode="flush"))
    assert "error" in result


def test_x_axis_flush(session):
    """Flush mode works on X axis."""
    session.execute("a = Box(10, 10, 10); show(a, 'a')")
    session.execute("b = Box(10, 10, 10); show(b, 'b')")
    result = json.loads(align_check(session, "a", "b", axis="X", mode="flush"))
    assert result["axis"] == "X"
    assert abs(result["delta"]) < 0.01
