"""Live analytic selection and exact registry access, across both worker modes."""

import json

import pytest
from build123d import (
    Box,
    Compound,
    Cylinder,
    GeomType,
    Pos,
    Rot,
    ShapeList,
    Sphere,
    Torus,
    export_step,
)

from build123d_mcp.selection import select_faces
from build123d_mcp.session import Session
from build123d_mcp.tools._recognition import resolve_analysis
from build123d_mcp.tools.inspect_features import inspect_features
from build123d_mcp.worker import InProcessSession, WorkerSession


@pytest.fixture(params=[WorkerSession, InProcessSession])
def proxy(request):
    session = request.param(exec_timeout=60)
    try:
        yield session
    finally:
        session._kill_worker()


def ok(session, code):
    output = session.execute(code)
    assert not output.startswith(("Error:", "Constraint failed")), output
    return output


def test_mcp_import_inspect_select_edit_and_stale_id(proxy, tmp_path, monkeypatch):
    from build123d_mcp import server

    monkeypatch.setattr(server, "_resolve_session", lambda: proxy)
    path = tmp_path / "plate.step"
    export_step(Box(40, 30, 10) - Cylinder(3, 12), path)
    imported = json.loads(server.import_cad_file(str(path), "part"))
    assert imported["execute_name"] == "part"
    ok(proxy, "assert part is get_object('part')")
    inventory = server.inspect_features("part")
    assert len(inventory.encode()) < 2000
    hole = json.loads(server.inspect_features("part", families="holes"))["features"][0]
    report = json.loads(server.inspect_features("part", feature_ids=hole["id"]))
    assert "select_faces(part," in report["execute_example"]
    # The one-line response is executable as returned, on the imported object.
    code = (
        "from build123d import *\n"
        f"selected = {report['execute_example']}\n"
        "assert len(selected) == 1\n"
        "assert any(selected[0].is_same(f) for f in part.faces())\n"
        "before = part\n"
        "part = fillet(selected[0].edges(), radius=0.5)\n"
        "assert part is not before\n"
        "show(part, 'part')\n"
        "assert part is get_object('part')\n"
    )
    output = server.execute(code)
    assert not output.startswith("Error:"), output
    stale = json.loads(server.inspect_features("part", feature_ids=hole["id"]))
    assert "stale" in stale["error"]


def test_replacing_tool_binds_the_result(proxy):
    ok(proxy, "from build123d import *\nshow(Box(40,30,10)-Cylinder(3,12), 'plate')")
    fid = json.loads(proxy.inspect_features("plate", families="holes"))["features"][0]["id"]
    ok(proxy, "original = plate")
    result = json.loads(
        proxy.edit_feature(fid, diameter=8, object_name="plate", result_name="plate")
    )
    assert result.get("committed"), result
    assert result["execute_name"] == "plate"
    ok(proxy, "assert plate is get_object('plate')\nassert plate is not original")


def test_snapshot_restore_reset_unknown_names(proxy):
    ok(proxy, "from build123d import *\nshow(Box(2,3,4), 'part')")
    proxy.save_snapshot("before")
    ok(proxy, "show(Box(5,6,7), 'part')\nshow(Box(1,1,1), 'later')\nscalar = 42")
    proxy.restore_snapshot("before")
    ok(
        proxy,
        "assert part is get_object('part')\nassert abs(part.volume - 24) < 1e-9\nassert scalar == 42",
    )
    missing = proxy.execute("get_object('missing')")
    assert "unknown object 'missing'" in missing and "['part']" in missing
    assert "NameError" in proxy.execute("print(later)")
    proxy.reset()
    assert "NameError" in proxy.execute("print(part)")
    missing = proxy.execute("get_object('part')")
    assert "available names: []" in missing
    ok(
        proxy,
        "from build123d import *\nshow(Box(1,1,1), 'part')\nassert part is get_object('part')",
    )


@pytest.mark.parametrize(
    "name", ["list", "open", "Box", "math", "pi_alias", "bad-name", "for", "show", "ｌｉｓｔ"]
)
def test_import_collision_is_reported_and_accessible(proxy, tmp_path, name):
    path = tmp_path / "box.step"
    export_step(Box(2, 3, 4), path)
    ok(proxy, "from build123d import *\nimport math\nfrom math import pi as pi_alias")
    response = json.loads(proxy.import_cad_file(str(path), name))
    assert response["execute_name"] is None
    assert "not bound" in response["binding_warning"]
    assert response["execute_example"] == f"get_object({name!r})"
    ok(proxy, f"assert abs(get_object({name!r}).volume - 24) < 1e-9")
    ok(
        proxy,
        "assert callable(Box)\nassert math.sqrt(4) == 2\nassert pi_alias > 3\nassert list((1,2)) == [1,2]",
    )


def test_show_binding_and_collision_in_same_call(proxy):
    out = ok(
        proxy,
        "from build123d import *\nshow(Box(1,2,3), 'part')\nassert part is get_object('part')",
    )
    assert "execute_name" in out and "part" in out
    out = ok(
        proxy,
        "show(Box(1,1,1), 'Box')\nassert callable(Box)\nassert abs(get_object('Box').volume - 1) < 1e-9",
    )
    assert "not bound" in out and "get_object('Box')" in out


def test_select_rotated_translated_caller_frame():
    session = Session()
    shape = Pos(17, -9, 31) * Rot(17, 32, 51) * (Box(40, 30, 10) - Cylinder(4, 12))
    session.register_object("part", shape)
    hole = json.loads(inspect_features(session, "part", families="holes"))["features"][0]
    descriptors = json.loads(inspect_features(session, "part", feature_ids=hole["id"]))["features"][
        0
    ]["faces"]
    result = select_faces(shape, faces=descriptors)
    assert isinstance(result, ShapeList) and len(result) == 1
    assert result[0].is_same(shape.faces().filter_by(GeomType.CYLINDER)[0])
    assert abs(descriptors[0]["direction"][2]) < 0.99
    ok(session, f"assert len(select_faces(part, faces={descriptors!r})) == 1")


def test_face_indices_are_metadata_and_all_analytic_properties_match():
    shape = Box(40, 30, 10) - Cylinder(4, 12)
    session = Session()
    session.register_object("part", shape)
    descriptors = resolve_analysis(session, "part").document["faces"]
    for descriptor in descriptors:
        # Deliberately impossible index: matching MUST use geometry alone.
        descriptor = {**descriptor, "id": 10000}
        assert len(select_faces(shape, faces=descriptor)) == 1
    wall = next(d for d in descriptors if d["kind"] == "cylinder")
    for field, wrong in [
        ("radius", 90),
        ("direction", [1, 0, 0]),
        ("origin", [90, 0, 0]),
        ("area", 90),
    ]:
        with pytest.raises(ValueError, match="matched 0 faces"):
            select_faces(shape, faces={**wall, field: wrong})


def test_ambiguous_and_missing_descriptors_are_never_guessed():
    shape = Compound([Box(10, 10, 10), Box(10, 10, 10)])
    descriptor = {"kind": "plane", "centroid": [0, 0, 5], "area": 100}
    with pytest.raises(ValueError, match=r"descriptor 0 .*matched 2 faces; expected exactly 1"):
        select_faces(shape, faces=descriptor)
    with pytest.raises(ValueError, match=r"descriptor 0 .*matched 0 faces"):
        select_faces(shape, faces={**descriptor, "centroid": [100, 0, 5]})


def test_keyword_filters_and_tolerance_override():
    shape = Box(40, 30, 10) - Cylinder(4, 12)
    walls = select_faces(shape, kind="cylinder", radius=4, centroid=[0, 0, 0], direction=[0, 0, -2])
    assert isinstance(walls, ShapeList) and len(walls) == 1
    assert len(select_faces(shape, kind="plane")) == 6
    assert len(select_faces(shape, kind="plane", direction=[0, 0, 1])) == 1
    assert len(select_faces(shape, kind="plane", area=400)) == 2
    assert len(select_faces(shape, kind="cylinder", radius=4.01)) == 0
    assert len(select_faces(shape, kind="cylinder", radius=4.01, tol=0.02)) == 1
    for tol in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValueError, match="tol must be finite and nonnegative"):
            select_faces(shape, tol=tol)


@pytest.mark.parametrize("shape", [Sphere(7), Torus(10, 2)])
def test_other_and_torus_inspection_descriptors(shape):
    session = Session()
    session.register_object("part", shape)
    for descriptor in resolve_analysis(session, "part").document["faces"]:
        assert len(select_faces(shape, faces=descriptor)) == 1


def test_drawing_helpers_bind_and_rollback_keeps_registry_consistent():
    session = Session()
    ok(
        session,
        "from build123d import *\nannotate(Box(1,2,3), 'note')\nregister_centerline(Box(1,1,1), 'axis_line')",
    )
    assert session.namespace["note"] is session.objects["note"]
    assert session.namespace["axis_line"] is session.objects["axis_line"]
    output = session.execute("show(Box(5,5,5), 'note')\nshow(Box(1,1,1), 'new')\nassert False")
    assert output.startswith("Constraint failed")
    assert session.namespace["note"] is session.objects["note"]
    assert "new" not in session.namespace and "new" not in session.objects
