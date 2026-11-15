"""Revision evidence on real parts, including bare faces and backend rendering."""

import copy
import json

import pytest
from build123d import Axis, Box, Cylinder, Pos, export_step, fillet

from build123d_mcp.session import Session
from build123d_mcp.tools._recognition import resolve_analysis
from build123d_mcp.tools.diff_step import diff_step, face_diff


def pair(old, new):
    session = Session()
    session.objects.update(old=old, new=new)
    return session


def no_verdict(value):
    if isinstance(value, dict):
        assert "verdict" not in value
        for v in value.values():
            no_verdict(v)
    elif isinstance(value, list):
        for v in value:
            no_verdict(v)


def test_plain_planar_wall_offset_is_reported():
    s = pair(Box(20, 30, 10), Pos(2, 0, 0) * Box(24, 30, 10))
    report = json.loads(diff_step(s, "old", "new"))
    assert report["features"] == []
    moved = [d for d in report["planar_deltas"] if d["normal_offset"] == pytest.approx(4)]
    assert len(moved) == 1
    assert moved[0]["old"]["centroid"][0] == 10
    assert moved[0]["new"]["centroid"][0] == 14
    no_verdict(report)


def test_pure_tangential_move_and_translation_reported():
    box = Box(20, 30, 10)
    report = json.loads(diff_step(pair(box, Pos(3, 0, 0) * box), "old", "new"))
    assert report["unchanged_faces"] == 0
    assert any(
        d["normal_offset"] == 0 and d["tangential_displacement"] == [3, 0, 0]
        for d in report["planar_deltas"]
    )
    assert report["planar_deltas_count"] == 6


def test_orphaned_fillet_is_reported():
    box = Box(30, 30, 10)
    rounded = fillet(box.edges().filter_by(Axis.Z), 2)
    report = json.loads(diff_step(pair(rounded, box), "old", "new"))
    assert any(
        f["class"] == "orphaned" and f["family"] in {"fillets", "blends"}
        for f in report["features"]
    )
    no_verdict(report)


def test_hole_resize_adapted_dimensions_and_claimed_face_not_duplicated():
    s = pair(Box(30, 30, 10) - Cylinder(3, 12), Box(30, 30, 10) - Cylinder(4, 12))
    report = json.loads(diff_step(s, "old", "new"))
    (hole,) = [f for f in report["features"] if f["family"] == "holes"]
    assert hole["class"] == "adapted"
    assert hole["changes"]["sizes.diameter"] == {"old": 6, "new": 8}
    changes = report["unclaimed_face_changes"]
    assert not [
        f for side in ("removed", "added") for f in changes[side] if f["kind"] == "cylinder"
    ]
    no_verdict(report)


def test_recursive_verdict_stripping_and_response_bound(monkeypatch):
    from specify_core import api

    original = api.diff

    def polluted(*args, **kwargs):
        out = original(*args, **kwargs)
        out["report"]["pmi"]["verdict"] = "ok"
        out["report"]["pmi"]["items"] = [
            {"verdict": "ok", "nested": {"verdict": "ok"}, "prose": "x" * 100_000}
        ] * 1000
        return out

    monkeypatch.setattr(api, "diff", polluted)
    text = diff_step(pair(Box(10, 10, 10), Box(11, 10, 10)), "old", "new")
    no_verdict(json.loads(text))
    assert len(text.encode()) < 100_000


def test_second_diff_reuses_baseline_load_and_analysis(monkeypatch, tmp_path):
    from specify_core import api

    s = pair(Box(30, 30, 10) - Cylinder(3, 12), Box(30, 30, 10) - Cylinder(4, 12))
    path = tmp_path / "old.step"
    export_step(s.objects["old"], path)
    calls = []
    for name in ("load", "analyse"):
        original = getattr(api, name)

        def counted(*args, _fn=original, _name=name, **kwargs):
            calls.append(_name)
            return _fn(*args, **kwargs)

        monkeypatch.setattr(api, name, counted)
    assert "error" not in json.loads(diff_step(s, str(path), "new"))
    assert calls == ["load", "analyse", "load", "analyse"]
    calls.clear()
    assert "error" not in json.loads(diff_step(s, str(path), "new"))
    assert calls == []
    # A new candidate must load/analyse only that candidate, never the baseline.
    s.objects["new"] = Box(30, 30, 10) - Cylinder(5, 12)
    assert "error" not in json.loads(diff_step(s, str(path), "new"))
    assert calls == ["load", "analyse"]


def test_identical_reindexed_tables_match_all_faces():
    s = pair(Box(20, 30, 10) - Cylinder(3, 12), Box(20, 30, 10) - Cylinder(3, 12))
    document = resolve_analysis(s, "old").document
    reindexed = copy.deepcopy(document)
    reindexed["faces"].reverse()
    for i, face in enumerate(reindexed["faces"]):
        face["id"] = i + 100
    report = face_diff(document, reindexed, {"features": []})
    assert report["unchanged_faces"] == len(document["faces"])
    assert report["planar_deltas"] == []
    assert report["unclaimed_face_changes"]["removed_count"] == 0
    assert report["unclaimed_face_changes"]["added_count"] == 0


def test_real_cli_overlay_returns_png_and_bounded_report():
    s = pair(Box(30, 30, 10) - Cylinder(3, 12), Box(30, 30, 10) - Cylinder(4, 12))
    result = diff_step(s, "old", "new", render=True)
    assert result["png"].startswith(b"\x89PNG\r\n\x1a\n")
    no_verdict(json.loads(result["report"]))


def test_render_failure_is_clear_and_no_fabricated_image(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ValueError("rendering blocked on specify-core#180 item 4")

    monkeypatch.setattr("build123d_mcp.tools.diff_step._render_overlay", unavailable)
    s = pair(Box(10, 10, 10), Box(11, 10, 10))
    result = diff_step(s, "old", "new", render=True)
    assert "png" not in result
    assert "specify-core#180 item 4" in json.loads(result["report"])["error"]


def test_compare_shape_error_names_replacement():
    from build123d_mcp.worker import WorkerSession

    proxy = WorkerSession.__new__(WorkerSession)
    assert "diff_step" in json.loads(proxy.compare("old", "new", kind="shape"))["error"]


def test_server_overlay_uses_render_view_image_delivery(monkeypatch):
    import os

    from build123d_mcp import server

    class Proxy:
        def diff_step(self, old, new, render=False):
            assert (old, new, render) == ("old", "new", True)
            return {"report": '{"features":[]}', "png": b"\x89PNG\r\n\x1a\n"}

    monkeypatch.setattr(server, "_resolve_session", lambda: Proxy())
    contents = server.diff_step("old", "new", render=True)
    assert json.loads(contents[0].text) == {"features": []}
    assert contents[1].type == "image" and contents[1].mime_type == "image/png"
    marker = contents[2].text
    assert marker.startswith("[SEND: ")
    path = marker[len("[SEND: ") : -1]
    try:
        assert os.path.isfile(path)
    finally:
        os.unlink(path)
