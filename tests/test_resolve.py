"""Tests for the resolve() tool."""
import json
import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.resolve import resolve
from build123d_mcp.tools.session_state import session_state


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


@pytest.fixture
def box_session(session):
    session.execute("b = Box(10, 10, 20); show(b, 'box')")
    return session


def test_top_face_by_z_filter(box_session):
    """Resolve top face of a box by Z filter."""
    result = json.loads(resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]"))
    assert "error" not in result
    assert result["type"] == "Face"
    assert result["object"] == "box"
    # Top face of a 20mm tall box centred at origin: Z = 10
    assert abs(result["center"][2] - 10.0) < 0.1


def test_face_has_area(box_session):
    result = json.loads(resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]"))
    assert "area" in result
    assert result["area"] > 0


def test_face_has_normal(box_session):
    result = json.loads(resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]"))
    assert "normal" in result
    # Normal should point in +Z direction
    assert result["normal"][2] > 0.9


def test_unknown_object_returns_error(box_session):
    result = json.loads(resolve(box_session, "nonexistent", ".faces()[0]"))
    assert "error" in result


def test_label_stored_in_session(box_session):
    resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]", label="top_face")
    assert "top_face" in box_session.geometry_refs
    stored = box_session.geometry_refs["top_face"]
    assert stored["type"] == "Face"
    assert stored["label"] == "top_face"


def test_label_appears_in_session_state(box_session):
    resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]", label="top_face")
    state = json.loads(session_state(box_session))
    assert "geometry_refs" in state
    assert "top_face" in state["geometry_refs"]


def test_ref_format_with_label(box_session):
    result = json.loads(resolve(box_session, "box", ".faces()[0]", label="base"))
    assert result["ref"] == "@cad[box#base]"


def test_bad_selector_returns_error(box_session):
    result = json.loads(resolve(box_session, "box", ".nonexistent_method()"))
    assert "error" in result


def test_geometry_refs_cleared_on_reset(box_session):
    resolve(box_session, "box", ".faces().sort_by(Axis.Z)[-1]", label="top_face")
    assert "top_face" in box_session.geometry_refs
    box_session.reset()
    assert box_session.geometry_refs == {}
