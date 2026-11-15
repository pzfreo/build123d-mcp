"""Revision evidence on real parts, including bare faces and backend rendering."""

import copy
import json
import math

import pytest
from build123d import Align, Axis, Box, Cylinder, GeomType, Pos, Sphere, export_step, fillet

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
    report = face_diff(
        document,
        reindexed,
        {"features": [], "other_faces": [], "unchanged_faces": len(document["faces"])},
    )
    assert report["unchanged_faces"] == len(document["faces"])
    assert report["planar_deltas"] == []
    assert report["unclaimed_face_changes"]["removed_count"] == 0
    assert report["unclaimed_face_changes"]["added_count"] == 0


def test_real_api_overlay_returns_png_and_bounded_report():
    s = pair(Box(30, 30, 10) - Cylinder(3, 12), Box(30, 30, 10) - Cylinder(4, 12))
    result = diff_step(s, "old", "new", render=True)
    assert result["png"].startswith(b"\x89PNG\r\n\x1a\n")
    no_verdict(json.loads(result["report"]))


def test_render_failure_is_clear_and_no_fabricated_image(monkeypatch):
    from specify_core import api

    def unavailable(*args, **kwargs):
        assert kwargs["image_width"] == 900
        raise ValueError("native image rendering unavailable")

    monkeypatch.setattr(api, "diff", unavailable)
    s = pair(Box(10, 10, 10), Box(11, 10, 10))
    result = diff_step(s, "old", "new", render=True)
    assert "png" not in result
    assert "native image rendering unavailable" in json.loads(result["report"])["error"]


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


def test_off_center_hole_resize_changes_host_boundaries_without_planar_motion():
    def part(radius):
        return fillet(Box(60, 40, 20).edges().filter_by(Axis.Z), 5) - Pos(10, 5, 0) * Cylinder(
            radius, 40
        )

    s = pair(part(3), part(4))
    report = json.loads(diff_step(s, "old", "new"))
    assert report["planar_deltas"] == []
    assert report["feature_boundary_changes_count"] == 2
    assert all(
        row["area_delta"] == pytest.approx(-7 * math.pi, abs=1e-5)
        for row in report["feature_boundary_changes"]
    )
    hole = next(f for f in report["features"] if f["family"] == "holes")
    assert all(
        set(row["explaining_feature_ids"]) == {hole["old"], hole["new"]}
        for row in report["feature_boundary_changes"]
    )
    # Every exposed feature reference can be consumed on exactly its own content.
    from build123d_mcp.tools.inspect_features import inspect_features

    for target, ref in (("old", hole["old"]), ("new", hole["new"])):
        assert "error" not in json.loads(inspect_features(s, target, feature_ids=ref))
    assert "stale" in json.loads(inspect_features(s, "new", feature_ids=hole["old"]))["error"]


def test_orphaned_fillet_adjoining_planes_are_boundary_changes():
    box = Box(30, 30, 10)
    s = pair(fillet(box.edges().filter_by(Axis.Z), 2), box)
    report = json.loads(diff_step(s, "old", "new"))
    assert report["planar_deltas"] == []
    assert report["feature_boundary_changes_count"] == 6
    assert all(row["explaining_feature_ids"] for row in report["feature_boundary_changes"])
    assert any(f["class"] == "orphaned" and f["family"] == "fillets" for f in report["features"])


def test_genuine_tangential_translation_of_boss_cap_and_wall_is_reported():
    base = Box(60, 50, 6)
    old = base + Pos(-12, 0, 3) * Box(
        8,
        10,
        8,
        align=(
            Align.CENTER,
            Align.CENTER,
            Align.MIN,
        ),
    )
    new = base + Pos(12, 0, 3) * Box(
        8,
        10,
        8,
        align=(
            Align.CENTER,
            Align.CENTER,
            Align.MIN,
        ),
    )
    report = json.loads(diff_step(pair(old, new), "old", "new"))
    assert any(
        d["old"]["centroid"][2] == 11
        and d["normal_offset"] == 0
        and d["area_delta"] == 0
        and d["tangential_displacement"] == [24, 0, 0]
        for d in report["planar_deltas"]
    )

    assert any(
        d["old"]["centroid"][2] == 7
        and abs(d["old"]["direction"][1]) == 1
        and d["normal_offset"] == 0
        and d["area_delta"] == 0
        and d["tangential_displacement"] == [24, 0, 0]
        for d in report["planar_deltas"]
    )
    assert not any(
        d["old"]["centroid"][2] == 3 and d["normal_offset"] == 0 for d in report["planar_deltas"]
    )


def test_rotated_revision_motion_uses_caller_frame(tmp_path):
    from build123d import Rot

    placement = Pos(10, 20, 30) * Rot(0, 0, 90)
    before, after = tmp_path / "old.step", tmp_path / "new.step"
    export_step(placement * (Box(40, 30, 10) - Cylinder(3, 12)), before)
    export_step(placement * (Box(40, 30, 10) - Pos(5, 0, 0) * Cylinder(4, 12)), after)
    report = json.loads(diff_step(Session(), str(before), str(after)))
    hole = next(f for f in report["features"] if f["family"] == "holes")
    assert hole["changes"]["position"]["old"] == pytest.approx([10, 20, 30], abs=1e-5)
    assert hole["changes"]["position"]["new"] == pytest.approx([10, 25, 30], abs=1e-5)


def test_many_feature_changes_preserve_complete_independent_sections(monkeypatch):
    from specify_core import api

    original = api.diff
    expected = {}

    def crowded(*args, **kwargs):
        out = original(*args, **kwargs)
        expected.update(out["report"]["measures"])
        template = out["report"]["features"][0]
        out["report"]["features"] = [
            {
                **template,
                "changes": {f"sizes.dimension_{i}": {"old": i, "new": i + 1} for i in range(250)},
            }
            for _ in range(500)
        ]
        out["report"]["problems"] = {"added": ["keep this independent finding"], "resolved": []}
        return out

    monkeypatch.setattr(api, "diff", crowded)
    report = json.loads(
        diff_step(
            pair(Box(30, 30, 10) - Cylinder(3, 12), Box(30, 30, 10) - Cylinder(4, 12)), "old", "new"
        )
    )
    for side in ("old", "new"):
        assert set(report["measures"][side]) == {"volume", "area", "min", "max", "faces"}
        for key in ("volume", "area", "min", "max", "faces"):
            assert report["measures"][side][key] == pytest.approx(expected[side][key], abs=1e-5)
    assert report["problems"] == {"added": ["keep this independent finding"], "resolved": []}
    assert report["features_truncated"]["total"] == 500
    assert report["features_truncated"]["shown"] < 500
    assert report["features"][-1]["omitted"] == 500 - report["features_truncated"]["shown"]
    no_verdict(report)


def test_bare_compare_keeps_shape_default_and_never_calls_fit(monkeypatch):
    import inspect

    from build123d_mcp import server
    from build123d_mcp.worker import WorkerSession

    proxy = WorkerSession.__new__(WorkerSession)
    monkeypatch.setattr(
        proxy, "clearance", lambda *args: pytest.fail("bare compare must not check fit")
    )
    assert "diff_step" in json.loads(proxy.compare("old", "new"))["error"]
    assert inspect.signature(server.compare).parameters["kind"].default == "shape"
    monkeypatch.setattr(server, "_resolve_session", lambda: proxy)
    assert "diff_step" in json.loads(server.compare("old", "new"))["error"]


@pytest.mark.xfail(strict=True, reason="needs specify-core built on quiddity >=0.5.3 (#181)")
def test_shortened_top_filleted_boss_reports_full_height():
    def boss(height):
        base = Box(120, 120, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
        post = Pos(0, 0, 10) * Cylinder(32.5, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
        body = base + post
        return fillet(body.edges().filter_by(GeomType.CIRCLE).sort_by(Axis.Z)[-1:], 3)

    report = json.loads(diff_step(pair(boss(13), boss(8)), "old", "new"))
    (boss_change,) = [f for f in report["features"] if f["family"] == "bosses"]
    assert boss_change["changes"]["sizes.height"] == {"old": 13, "new": 8}


def test_overlay_reuses_the_single_comparison_and_loaded_handles(monkeypatch):
    from specify_core import api

    session = pair(Box(30, 30, 10) - Cylinder(3, 12), Box(30, 30, 10) - Cylinder(4, 12))
    before = resolve_analysis(session, "old")
    after = resolve_analysis(session, "new")
    original = api.diff
    calls = []

    def counted(old, new, **kwargs):
        assert old is before.loaded and new is after.loaded
        calls.append(kwargs)
        return original(old, new, **kwargs)

    monkeypatch.setattr(api, "diff", counted)
    result = diff_step(session, "old", "new", render=True)
    assert result["png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert calls == [{"image_width": 900}]


def test_unclaimed_sphere_resize_uses_native_face_report(monkeypatch):
    from specify_core import api

    original = api.diff
    native = {}

    def capture(*args, **kwargs):
        out = original(*args, **kwargs)
        native.update(out["report"])
        return out

    monkeypatch.setattr(api, "diff", capture)
    report = json.loads(diff_step(pair(Sphere(10), Sphere(11)), "old", "new"))
    assert report["features"] == []
    assert len(native["other_faces"]) == 1
    change = native["other_faces"][0]
    assert change["class"] == "adapted"
    unclaimed = report["unclaimed_face_changes"]
    assert unclaimed["removed_count"] == unclaimed["added_count"] == 1
    # Spheres are native "other" surfaces: the public descriptors expose
    # their area, not a sphere radius.
    assert unclaimed["removed"][0]["kind"] == unclaimed["added"][0]["kind"] == "other"
    assert unclaimed["removed"][0]["area"] == pytest.approx(4 * math.pi * 10**2)
    assert unclaimed["added"][0]["area"] == pytest.approx(4 * math.pi * 11**2)
    assert [unclaimed[side][0]["area"] for side in ("removed", "added")] == change["area"]
