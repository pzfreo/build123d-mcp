"""Local edits on ordinary solids, with independently rebuilt reference models."""

import json

import pytest

from build123d_mcp.worker import InProcessSession, WorkerSession


@pytest.fixture(params=[WorkerSession, InProcessSession])
def proxy(request):
    session = request.param(exec_timeout=90)
    try:
        yield session
    finally:
        session._kill_worker()


def ok(proxy, code):
    text = proxy.execute(code)
    assert not text.startswith(("Error:", "Constraint failed")), text
    return text


BUILDERS = """
from build123d import *

def boss(height):
    base = Box(120, 120, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
    post = Pos(0, 0, 10) * Cylinder(32.5, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = base + post
    return fillet(body.edges().filter_by(GeomType.CIRCLE).sort_by(Axis.Z)[-1:], 3)

def bored_boss(height, foot_fillet):
    body = boss(height)
    if foot_fillet:
        body = fillet(body.edges().filter_by(GeomType.CIRCLE).sort_by(Axis.Z)[:1], 2)
    bore = Pos(0,0,-1) * Cylinder(10, height+12, align=(Align.CENTER,Align.CENTER,Align.MIN))
    return body - bore

def pocket(right):
    # Rebuild the rounded cutter from dimensions, independently of the edit.
    stock = Box(80, 60, 20, align=(Align.CENTER, Align.CENTER, Align.MIN))
    cutter = Pos((right-15)/2, 0, 8) * Box(right+15, 24, 15, align=(Align.CENTER, Align.CENTER, Align.MIN))
    cutter = fillet(cutter.edges().filter_by(Axis.Z), 2)
    return stock - cutter

def signature(body):
    return (body.volume, tuple(body.bounding_box().min), tuple(body.bounding_box().max),
            [(f.area, tuple(f.center()), f.geom_type.name) for f in body.faces()],
            [tuple(v) for v in body.vertices()])
"""


COMPARE = """
assert isinstance(edited, Solid)
assert signature(part) == before
relative_error = abs(edited.volume-reference.volume)/reference.volume
print('relative_volume_error', relative_error)
assert relative_error < 1e-10
assert (edited.bounding_box().min-reference.bounding_box().min).length < 1e-7
assert (edited.bounding_box().max-reference.bounding_box().max).length < 1e-7
assert sorted(f.geom_type.name for f in edited.faces()) == sorted(f.geom_type.name for f in reference.faces())
show(reference, 'reference')
show(edited, 'edited')
"""


def compare(proxy, code):
    output = ok(proxy, BUILDERS + code + COMPARE)
    assert_reference_diff(proxy)
    return output


def assert_reference_diff(proxy):
    report = json.loads(proxy.diff_step("reference", "edited"))
    assert "error" not in report, report
    assert report["features"] == [], report
    assert report["planar_deltas"] == [], report
    assert report["unclaimed_face_changes"]["added_count"] == 0, report
    assert report["unclaimed_face_changes"]["removed_count"] == 0, report


@pytest.mark.parametrize("height", [8, 11])
def test_shorten_top_filleted_boss(proxy, height):
    compare(
        proxy,
        f"""
part = boss(13)
before = signature(part)
cap = select_faces(part, kind='plane', centroid=(0,0,23))
blend = select_faces(part, kind='torus', radius=3)
assert len(cap) == len(blend) == 1
edited = translate_faces(part, cap + blend, (0,0,{height - 13}))
reference = boss({height})
""",
    )


def test_translate_holed_top_face_preserves_bore_seam(proxy):
    compare(
        proxy,
        """
part = Box(20,20,10) - Cylinder(3,20)
before = signature(part)
edited = translate_faces(part, select_faces(part, kind='plane', direction=(0,0,1)), (0,0,2))
reference = Pos(0,0,1)*Box(20,20,12) - Cylinder(3,20)
""",
    )


@pytest.mark.parametrize("foot_fillet", [False, True])
def test_shorten_through_bored_filleted_boss(proxy, foot_fillet):
    compare(
        proxy,
        f"""
part = bored_boss(13, {foot_fillet})
before = signature(part)
cap = select_faces(part, kind='plane', centroid=(0,0,23))
blend = select_faces(part, kind='torus', radius=3)
assert len(cap) == len(blend) == 1
edited = translate_faces(part, cap + blend, (0,0,-5))
reference = bored_boss(8, {foot_fillet})
""",
    )


def test_push_pocket_wall_and_corner_fillets(proxy):
    compare(
        proxy,
        """
part = pocket(15)
show(part, 'baseline')
before = signature(part)
wall = select_faces(part, kind='plane', direction=(-1,0,0))
wall = [f for f in wall if abs(f.center().X-15) < 1e-6]
blends = [f for f in select_faces(part, kind='cylinder', radius=2) if f.center().X > 10]
assert len(wall) == 1 and len(blends) == 2
edited = translate_faces(part, wall + blends, (3,0,0))
reference = pocket(18)
""",
    )
    old = json.loads(proxy.inspect_features("baseline", families="fillets,blends"))
    new = json.loads(proxy.inspect_features("edited", families="fillets,blends"))
    assert len(old["features"]) == len(new["features"]) == 4
    assert [f["radius"] for f in old["features"]] == [2.0] * 4
    assert [f["radius"] for f in new["features"]] == [2.0] * 4
    # Recognition records preserve the actual blend dimensions, independent of ids/location.
    for key in ("radius", "sizes"):
        assert sorted(str(f.get(key)) for f in old["features"]) == sorted(
            str(f.get(key)) for f in new["features"]
        )


def test_closed_void_is_untouched(proxy):
    compare(
        proxy,
        """
part = Box(30,30,30) - Box(10,10,10)
before = signature(part)
old_void = min(part.shells(), key=lambda s: s.area)
old_void_faces = sorted((f.area, tuple(f.center())) for f in old_void.faces())
edited = translate_faces(part, select_faces(part, kind='plane', centroid=(0,0,15)), (0,0,3))
reference = Pos(0,0,1.5)*Box(30,30,33) - Box(10,10,10)
new_void = min(edited.shells(), key=lambda s: s.area)
assert sorted((f.area, tuple(f.center())) for f in new_void.faces()) == old_void_faces
assert abs(Solid(old_void).volume-Solid(new_void).volume) < 1e-9
assert abs(abs(Solid(new_void).volume)-1000) < 1e-9
""",
    )


@pytest.mark.parametrize("kind", ["plane", "cylinder", "hole", "sphere", "cone", "torus"])
def test_offset_faces(proxy, kind):
    code = {
        "plane": """
part = Box(20,30,10)
before = signature(part)
edited = offset_faces(part, select_faces(part, kind='plane', direction=(1,0,0)), 3)
reference = Pos(1.5,0,0)*Box(23,30,10)
""",
        "cylinder": """
part = Box(40,40,6,align=(Align.CENTER,Align.CENTER,Align.MIN)) + Pos(0,0,6)*Cylinder(8,10,align=(Align.CENTER,Align.CENTER,Align.MIN))
before = signature(part)
edited = offset_faces(part, select_faces(part, kind='cylinder', radius=8), 2)
reference = Box(40,40,6,align=(Align.CENTER,Align.CENTER,Align.MIN)) + Pos(0,0,6)*Cylinder(10,10,align=(Align.CENTER,Align.CENTER,Align.MIN))
""",
        "hole": """
part = Box(40,40,10) - Cylinder(8,12)
before = signature(part)
edited = offset_faces(part, select_faces(part, kind='cylinder', radius=8), 2)
reference = Box(40,40,10) - Cylinder(6,12)
""",
        "cone": """
part = Cone(10,6,10)
before = signature(part)
edited = offset_faces(part, select_faces(part, kind='cone'), 1)
# Parallel cone generators intersect the original cap planes at this radial shift.
radial_shift = (1 + ((10-6)/10)**2)**0.5
reference = Cone(10+radial_shift,6+radial_shift,10)
""",
        "torus": """
part = Torus(10,2)
before = signature(part)
edited = offset_faces(part, part.faces(), 1)
reference = Torus(10,3)
""",
        "sphere": """
part = Sphere(10)
before = signature(part)
edited = offset_faces(part, part.faces(), 2)
reference = Sphere(12)
""",
    }[kind]
    compare(proxy, code)


@pytest.mark.parametrize("kind", ["chamfer", "draft"])
def test_planar_offset_with_noninvariant_neighbours(proxy, kind):
    code = {
        "chamfer": """
part = Box(20,20,10)
edge = [e for e in part.edges().filter_by(Axis.Y) if e.center().X>0 and e.center().Z>0]
part = chamfer(edge, 3)
before = signature(part)
selected = [f for f in part.faces() if f.normal_at().X>0.5 and f.normal_at().Z>0.5]
assert len(selected) == 1
edited = offset_faces(part, selected, 1)
reference = Box(20,20,10)
reference_edge = [e for e in reference.edges().filter_by(Axis.Y) if e.center().X>0 and e.center().Z>0]
reference = chamfer(reference_edge, 3-2**0.5)
assert abs(edited.volume - (4000-(3-2**0.5)**2/2*20)) < 1e-8
""",
        "draft": """
part = extrude(Plane.XZ*Polygon((-10,-5),(10,-5),(8,5),(-10,5),align=None),amount=20).translate((0,10,0))
before = signature(part)
selected = [f for f in part.faces() if f.normal_at().X>0.9]
assert len(selected) == 1
edited = offset_faces(part, selected, 1)
# Offset x+0.2*z=9 by 1 along its unit outward normal. Caps stay at z=+/-5.
shift = (1+0.2**2)**0.5
reference = extrude(Plane.XZ*Polygon((-10,-5),(10+shift,-5),(8+shift,5),(-10,5),align=None),amount=20).translate((0,10,0))
""",
    }[kind]
    compare(proxy, code)


@pytest.mark.parametrize("case", ["merging_holes", "side_breakthrough", "shortcut_collapse"])
def test_specific_offset_failure_without_internal_prefix(proxy, case):
    setup, operation, reason = {
        "merging_holes": (
            "part = Box(40,30,10)-Pos(-6,0,0)*Cylinder(3,20)-Pos(6,0,0)*Cylinder(3,20)",
            "offset_faces(part, [f for f in select_faces(part, kind='cylinder') if f.center().X<0], -7)",
            "offset failed: self-intersection or face collapse",
        ),
        "side_breakthrough": (
            "part = Box(20,20,10)-Pos(5,0,0)*Cylinder(2,20)",
            "offset_faces(part, select_faces(part, kind='cylinder'), -4)",
            "offset failed: self-intersection or face collapse",
        ),
        "shortcut_collapse": (
            "part = Box(20,30,10)",
            "offset_faces(part, select_faces(part, kind='plane', direction=(1,0,0)), -20)",
            "topology change: move collapses or inverts",
        ),
    }[case]
    ok(proxy, BUILDERS + setup + "\nbefore = signature(part)")
    output = proxy.execute(operation)
    assert output.startswith("Error: ValueError: offset_faces: "), output
    assert reason in output, output
    assert "translate_faces:" not in output, output
    assert "Null TopoDS" not in output, output
    ok(proxy, "assert signature(part) == before")


@pytest.mark.parametrize(
    "setup,operation,reason",
    [
        (
            "part = Box(20,30,10)",
            "translate_faces(part, select_faces(part, kind='plane', direction=(1,0,0)), (-20,0,0))",
            "collapses or inverts",
        ),
        (
            "part = loft([Plane.XY*Rectangle(20,20), Plane(origin=(0,0,5))*Circle(8), Plane(origin=(0,0,10))*Rectangle(15,15)])",
            "translate_faces(part, select_faces(part, kind='plane', direction=(0,0,1)), (0,0,2))",
            "non-analytic neighbour BSPLINE cannot be extended",
        ),
        (
            "part = Box(30,30,30) - Box(10,10,10)",
            "translate_faces(part, select_faces(part, kind='plane', centroid=(0,0,15)), (0,0,-13))",
            "self-intersection",
        ),
        (
            "part = boss(13)",
            "translate_faces(part, select_faces(part, kind='plane', centroid=(0,0,23)), (0,0,-2))",
            "neighbour TORUS cannot be extended",
        ),
        (
            "part = boss(13)",
            "offset_faces(part, select_faces(part, kind='cylinder', radius=32.5), 2)",
            "tangent propagation or topology change",
        ),
        (
            "part = Cylinder(8,10)",
            "offset_faces(part, select_faces(part, kind='cylinder'), -9)",
            "offset collapses or inverts the radius",
        ),
    ],
)
def test_refusals_preserve_input(proxy, setup, operation, reason):
    ok(proxy, BUILDERS + setup + "\nbefore = signature(part)")
    output = proxy.execute(operation)
    assert output.startswith("Error: ValueError:"), output
    assert reason in output, output
    ok(proxy, "assert signature(part) == before")


def test_split_planar_face_volume_and_topology(proxy):
    output = compare(
        proxy,
        """
part = Box(20,30,10)
before = signature(part)
top = select_faces(part, kind='plane', direction=(0,0,1))[0]
edited = split_face_chord(part, top, (-10,0,5), (10,0,5))
assert isinstance(edited, Solid)
assert len(edited.faces()) == len(part.faces())+1
assert len(select_faces(edited, kind='plane', direction=(0,0,1))) == 2
assert signature(part) == before
# Integration on identical analytic supports differs only through floating-point
# summation; 1e-9 relative is conservative for OCCT's double precision quadrature.
print('absolute_volume_error', abs(edited.volume-part.volume))
assert abs(edited.volume-part.volume) <= 1e-9*part.volume
assert (edited.bounding_box().min-part.bounding_box().min).length < 1e-7
assert (edited.bounding_box().max-part.bounding_box().max).length < 1e-7
# Independent seven-face reference: sew five block faces and two fresh top patches.
reference_faces = [f for f in Box(20,30,10).faces() if f.normal_at().Z < 0.9]
for low, high in [(-15,0), (0,15)]:
    boundary = Wire.make_polygon([(-10,low,5), (10,low,5), (10,high,5), (-10,high,5)])
    reference_faces.append(Face(boundary))
reference = Solid(Shell(reference_faces))
""",
    )
    assert "absolute_volume_error 0.0" in output
    assert "PASS" in proxy.validate("edited")


@pytest.mark.parametrize(
    "setup,p0,p1,reason",
    [
        ("Box(20,30,10)", (-10, 0, 6), (10, 0, 5), "endpoint is not on"),
        ("Box(20,30,10)-Cylinder(3,12)", (-10, 0, 5), (10, 0, 5), "chord leaves"),
        ("Box(20,30,10)", (0, 0, 5), (10, 0, 5), "face boundary"),
        ("Box(20,30,10)", (-10, -15, 5), (10, -15, 5), "split"),
    ],
)
def test_split_refuses_bad_chord(proxy, setup, p0, p1, reason):
    ok(proxy, BUILDERS + f"part = {setup}\nbefore = signature(part)")
    output = proxy.execute(
        f"split_face_chord(part, select_faces(part, kind='plane', direction=(0,0,1))[0], {p0!r}, {p1!r})"
    )
    assert output.startswith("Error: ValueError:"), output
    assert reason in output, output
    ok(proxy, "assert signature(part) == before")


def test_helpers_are_pure_and_do_not_expand_sandbox(proxy):
    ok(
        proxy,
        BUILDERS
        + """
part = Box(20,30,10)
faces = select_faces(part, kind='plane', direction=(0,0,1))
candidate = translate_faces(part, faces, (0,0,1))
assert callable(translate_faces) and callable(offset_faces) and callable(split_face_chord)
assert candidate is not part
assert not hasattr(candidate, 'module')
assert not hasattr(candidate, 'builder')
""",
    )
    assert "available names: []" in proxy.execute("get_object('candidate')")
    for attack in [
        "import build123d_mcp.face_edit",
        "from OCP.BRepOffset import BRepOffset_MakeOffset",
        "translate_faces.__globals__",
        "offset_faces.__globals__",
        "split_face_chord.__globals__",
    ]:
        output = proxy.execute(attack)
        assert "Error:" in output, output
    for name in ("translate_faces", "offset_faces", "split_face_chord"):
        output = ok(proxy, f"show(part, {name!r})\nassert callable({name})")
        assert "not bound" in output


def test_split_cylindrical_generator(proxy):
    compare(
        proxy,
        """
part = Cylinder(8,10)
before = signature(part)
wall = select_faces(part, kind='cylinder')[0]
edited = split_face_chord(part, wall, (-8,0,-5), (-8,0,5))
assert len(edited.faces()) == 4
assert len(select_faces(edited, kind='cylinder')) == 2
assert abs(edited.volume-part.volume) <= 1e-9*part.volume
assert signature(part) == before
# Independent reference: two semicylindrical supports and two circular caps.
half = Cylinder(8,10,arc_size=180,align=(Align.CENTER,Align.MIN,Align.CENTER))
reference_faces = list(Cylinder(8,10).faces().filter_by(GeomType.PLANE))
reference_faces += list(half.faces().filter_by(GeomType.CYLINDER))
reference_faces += list((Rot(0,0,180)*half).faces().filter_by(GeomType.CYLINDER))
reference = Solid(Shell(reference_faces))
""",
    )


def test_foreign_empty_duplicate_and_nonfinite_selections(proxy):
    ok(proxy, BUILDERS + "part=Box(20,30,10)\nbefore=signature(part)")
    for expression, reason in [
        ("translate_faces(part, [], (0,0,1))", "nonempty"),
        ("translate_faces(part, Box(20,30,10).faces()[:1], (0,0,1))", "does not belong"),
        ("translate_faces(part, [part.faces()[0],part.faces()[0]], (0,0,1))", "duplicate"),
        ("translate_faces(part, part.faces()[:1], (0,0,float('nan')))", "finite"),
        ("offset_faces(part, part.faces()[:1], float('inf'))", "finite"),
    ]:
        output = proxy.execute(expression)
        assert output.startswith("Error: ValueError:"), output
        assert reason in output, output
        ok(proxy, "assert signature(part) == before")
