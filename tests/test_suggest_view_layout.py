"""Tests for the suggest_view_layout tool."""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.suggest_view_layout import suggest_view_layout


@pytest.fixture
def session_with_box():
    s = Session()
    s.execute("from build123d import *; result = Box(40, 20, 15)")
    return s


@pytest.fixture
def session_with_named_box():
    s = Session()
    s.execute("from build123d import *; show(Box(40, 20, 15), 'part')")
    return s


# --- basic structure ---


def test_returns_valid_json(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    assert "views" in r
    assert "page_w" in r
    assert "page_h" in r
    assert "scale" in r
    assert "warnings" in r


def test_default_four_views_present(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    assert set(r["views"].keys()) == {"front", "plan", "side", "iso"}


def test_view_has_required_fields(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    for vname, vdata in r["views"].items():
        assert "VIEW_X" in vdata, f"{vname} missing VIEW_X"
        assert "VIEW_Y" in vdata, f"{vname} missing VIEW_Y"
        assert "look_at" in vdata, f"{vname} missing look_at"
        assert "camera" in vdata, f"{vname} missing camera"
        assert "up" in vdata, f"{vname} missing up"


def test_named_object(session_with_named_box):
    r = json.loads(suggest_view_layout(session_with_named_box, "part"))
    assert r["views"]["front"]["VIEW_X"] > 0


def test_unknown_object_returns_error():
    s = Session()
    r = json.loads(suggest_view_layout(s, "nonexistent"))
    assert "error" in r


def test_unknown_view_returns_error(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape", views=["front", "bad_view"]))
    assert "error" in r


# --- layout geometry ---


def test_front_view_above_title_block(session_with_box):
    margin, tb_h = 10.0, 30.0
    r = json.loads(
        suggest_view_layout(session_with_box, "shape", margin=margin, title_block_h=tb_h)
    )
    fv = r["views"]["front"]
    assert fv["VIEW_Y"] - fv["half_h"] > margin + tb_h


def test_plan_view_above_front(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    assert r["views"]["plan"]["VIEW_Y"] > r["views"]["front"]["VIEW_Y"]


def test_side_view_right_of_front(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    assert r["views"]["side"]["VIEW_X"] > r["views"]["front"]["VIEW_X"]


def test_front_and_plan_share_x_column(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    assert r["views"]["front"]["VIEW_X"] == r["views"]["plan"]["VIEW_X"]


def test_front_and_side_share_y_row(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    assert r["views"]["front"]["VIEW_Y"] == r["views"]["side"]["VIEW_Y"]


def test_scale_increases_view_positions(session_with_box):
    r1 = json.loads(suggest_view_layout(session_with_box, "shape", scale=1.0))
    r2 = json.loads(suggest_view_layout(session_with_box, "shape", scale=2.0))
    # At higher scale, plan view must be further up
    assert r2["views"]["plan"]["VIEW_Y"] > r1["views"]["plan"]["VIEW_Y"]


def test_subset_views(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape", views=["front", "plan"]))
    assert set(r["views"].keys()) == {"front", "plan"}


# --- fit checking and suggestions ---


def test_no_warnings_for_small_part_a4(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape", scale=1.0))
    assert r["warnings"] == []


def test_oversized_layout_produces_warnings(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape", scale=10.0))
    assert len(r["warnings"]) > 0


def test_oversized_layout_provides_suggestion(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape", scale=10.0))
    assert "suggestion" in r
    s = r["suggestion"]
    assert "page_w" in s and "scale" in s


def test_suggestion_scale_smaller_than_requested(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape", scale=10.0))
    assert r["suggestion"]["scale"] < 10.0


# --- look_at ---


def test_iso_look_at_unscaled(session_with_box):
    """Iso view uses unscaled world centroid as look_at."""
    r = json.loads(suggest_view_layout(session_with_box, "shape", scale=2.0))
    iso_la = r["views"]["iso"]["look_at"]
    front_la = r["views"]["front"]["look_at"]
    # Front look_at should be 2× iso look_at for a centred part
    assert abs(front_la[0] - iso_la[0] * 2) < 0.01
    assert abs(front_la[2] - iso_la[2] * 2) < 0.01


def test_part_size_included(session_with_box):
    r = json.loads(suggest_view_layout(session_with_box, "shape"))
    ps = r["part_size"]
    assert abs(ps["x"] - 40.0) < 0.01
    assert abs(ps["y"] - 20.0) < 0.01
    assert abs(ps["z"] - 15.0) < 0.01
