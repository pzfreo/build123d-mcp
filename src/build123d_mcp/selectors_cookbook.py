"""Single source of truth for the build123d selectors cookbook.

Task-indexed: each example answers a real "how do I select X?" question.
Sections with a label are runnable code blocks executed by
tests/test_selectors_cookbook.py — they must end with `result = ...` or
`show(...)` so current_shape is set.
"""

from dataclasses import dataclass


@dataclass
class Section:
    text: str
    label: str | None = None  # None = prose only, not tested


SECTIONS: list[Section] = [
    Section(
        "BUILD123D SELECTORS COOKBOOK\n"
        "============================\n"
        "Task-indexed reference for picking faces, edges, and vertices.\n"
        "Use the explicit form (.sort_by/.filter_by); the operator shortcuts\n"
        "(>, <, |, etc.) are summarised at the end.\n"
        "\n"
        "Canonical idiom from build123d's own FAQ: select from higher up\n"
        "in the topology first, then drill down. Find the parent face,\n"
        "then ask the face for its edges. Avoids brittle global queries."
    ),
    Section(
        label="drilldown",
        text="""\
## Drill-down idiom — find the parent, then its features
# This is THE canonical pattern. Filleting a hole? Find the hole's face
# first, then get its edges. Don't try to select a specific edge globally.
from build123d import *
plate = Box(40, 40, 5) - Cylinder(3, 5).move(Location((0, 0, 0)))
top_face = plate.faces().sort_by(Axis.Z)[-1]
hole_edges = top_face.edges().filter_by(GeomType.CIRCLE)
print(f"top face area: {top_face.area:.1f}, hole edges: {len(hole_edges)}")
result = plate""",
    ),
    Section(
        label="cardinal_faces",
        text="""\
## Cardinal faces — top/bottom/left/right by axis sort
# named_face(shape, "top") wraps the same chain. The raw form gives you
# fine control (e.g. multiple faces, custom axes).
from build123d import *
plate = Box(20, 30, 5)
top    = plate.faces().sort_by(Axis.Z)[-1]   # highest Z
bottom = plate.faces().sort_by(Axis.Z)[0]    # lowest Z
right  = plate.faces().sort_by(Axis.X)[-1]   # highest X
print(f"top={top.area}, bottom={bottom.area}, right={right.area}")
result = plate""",
    ),
    Section(
        label="by_geom_type_edges",
        text="""\
## Edges by geometric type — circles, lines, splines
# Edge GeomType: LINE, CIRCLE, ELLIPSE, BEZIER, BSPLINE, HYPERBOLA, PARABOLA, OFFSET, OTHER.
from build123d import *
plate = Box(40, 40, 5) - Cylinder(3, 5).move(Location((0, 0, 0)))
circles = plate.edges().filter_by(GeomType.CIRCLE)
lines   = plate.edges().filter_by(GeomType.LINE)
print(f"circles: {len(circles)}, lines: {len(lines)}")
result = plate""",
    ),
    Section(
        label="by_geom_type_faces",
        text="""\
## Faces by geometric type — planes, cylinders, spheres
# Face GeomType: PLANE, CYLINDER, CONE, SPHERE, TORUS, BEZIER, BSPLINE, REVOLUTION, EXTRUSION, OFFSET, OTHER.
# A counterbore = a cylindrical face. Cylinder hole walls are CYLINDER faces.
from build123d import *
part = Box(40, 40, 10) - Cylinder(5, 10).move(Location((0, 0, 0)))
cylinders = part.faces().filter_by(GeomType.CYLINDER)
planes    = part.faces().filter_by(GeomType.PLANE)
print(f"cylindrical faces (hole walls): {len(cylinders)}, planar: {len(planes)}")
result = part""",
    ),
    Section(
        label="parallel_perpendicular",
        text="""\
## Faces parallel to a plane / perpendicular to an axis
# filter_by(Plane.XY) → faces parallel to the XY plane (normals along Z).
# filter_by(Axis.X)   → faces perpendicular to X axis (normals along X).
from build123d import *
part = Box(20, 20, 5)
horizontal = part.faces().filter_by(Plane.XY)  # 2 faces: top + bottom
side_x     = part.faces().filter_by(Axis.X)    # 2 faces: +X and -X
print(f"horizontal: {len(horizontal)}, perpendicular to X: {len(side_x)}")
result = part""",
    ),
    Section(
        label="largest_face",
        text="""\
## Largest face by area / longest edge by length
# Use SortBy.AREA, SortBy.LENGTH, SortBy.RADIUS, SortBy.VOLUME, SortBy.DISTANCE.
from build123d import *
part = Box(40, 20, 10)
largest_face = part.faces().sort_by(SortBy.AREA)[-1]
longest_edge = part.edges().sort_by(SortBy.LENGTH)[-1]
print(f"largest face area: {largest_face.area}, longest edge: {longest_edge.length}")
result = part""",
    ),
    Section(
        label="lambda_filter",
        text="""\
## Filter by arbitrary property — use a lambda
# Any boolean expression on the entity. Floats: compare with a tolerance.
from build123d import *
part = Box(40, 20, 10)
twenty_long = part.edges().filter_by(lambda e: abs(e.length - 20) < 1e-3)
print(f"edges ~20mm long: {len(twenty_long)}")
result = part""",
    ),
    Section(
        label="circles_by_radius",
        text="""\
## Circles of a specific radius — combine GeomType with lambda
# Filtering circular edges first lets the lambda assume .radius exists.
from build123d import *
plate = (Box(40, 40, 5)
         - Cylinder(3, 5).move(Location((10, 10, 0)))
         - Cylinder(5, 5).move(Location((-10, -10, 0))))
r3 = plate.edges().filter_by(GeomType.CIRCLE).filter_by(lambda e: abs(e.radius - 3) < 1e-3)
print(f"3mm circles: {len(r3)}")
result = plate""",
    ),
    Section(
        label="select_last_in_builder",
        text="""\
## Select.LAST — only the geometry from the most recent operation
# Inside BuildPart, `p.edges(Select.LAST)` returns just the edges that the
# last operation produced. Critical when you want to fillet only the
# freshly-cut hole, not every edge in the part.
from build123d import *
with BuildPart() as p:
    Box(20, 20, 10)
    Cylinder(3, 10, mode=Mode.SUBTRACT)
    new = p.edges(Select.LAST)
    print(f"edges from cylinder cut: {len(new)}")
    fillet(new.filter_by(GeomType.CIRCLE), radius=0.5)
result = p.part""",
    ),
    Section(
        label="select_modes",
        text="""\
## Select.ALL vs Select.LAST vs Select.NEW
# Select.ALL  = every edge/face in the part (default if omitted)
# Select.LAST = entities produced by the most recent operation
# Select.NEW  = entities created (not just touched) by the most recent op
# Select.LAST is the workhorse; ALL is the explicit default; NEW is narrower
# than LAST and depends on operation semantics.
from build123d import *
with BuildPart() as p:
    Box(20, 20, 10)
    Cylinder(3, 10, mode=Mode.SUBTRACT)
    print(f"ALL: {len(p.edges(Select.ALL))}, LAST: {len(p.edges(Select.LAST))}")
result = p.part""",
    ),
    Section(
        label="chain_filters",
        text="""\
## Chain filters — narrow progressively
# Each .filter_by() reduces the ShapeList. Order doesn't affect correctness,
# but cheap filters first (GeomType) before expensive ones (lambda) is faster.
from build123d import *
part = Box(20, 20, 10) - Cylinder(3, 10).move(Location((0, 0, 0)))
small_circles = part.edges().filter_by(GeomType.CIRCLE).filter_by(lambda e: e.radius < 5)
print(f"small circles: {len(small_circles)}")
result = part""",
    ),
    Section(
        label="sort_then_slice",
        text="""\
## Sort then slice — "the N largest/smallest"
# sort_by returns a ShapeList; standard Python slicing applies.
# [-2:] = last two (largest), [:2] = first two (smallest), [-1] = single largest.
from build123d import *
plate = (Box(40, 40, 5)
         - Cylinder(2, 5).move(Location((10, 10, 0)))
         - Cylinder(3, 5).move(Location((-10, 10, 0)))
         - Cylinder(4, 5).move(Location((0, -10, 0))))
two_biggest = plate.edges().filter_by(GeomType.CIRCLE).sort_by(SortBy.RADIUS)[-2:]
print(f"two biggest circles, radii: {sorted(e.radius for e in two_biggest)}")
result = plate""",
    ),
    Section(
        label="convex_fillets",
        text="""\
## Detect outer fillets — Face.is_circular_convex
# After filleting outer edges, the rounded corners are convex circular faces.
from build123d import *
with BuildPart() as p:
    Box(20, 20, 10)
    fillet(p.part.edges().filter_by(Axis.Z), radius=2)
result = p.part
outer_fillets = result.faces().filter_by(Face.is_circular_convex)
print(f"outer fillet faces: {len(outer_fillets)}")""",
    ),
    Section(
        label="concave_fillets",
        text="""\
## Detect hole walls / inner fillets — Face.is_circular_concave
# Cylindrical hole walls are concave from outside. Useful for finding all
# holes regardless of size.
from build123d import *
with BuildPart() as p:
    Box(20, 20, 10)
    Cylinder(5, 10, mode=Mode.SUBTRACT)
result = p.part
hole_walls = result.faces().filter_by(Face.is_circular_concave)
print(f"concave circular faces (hole walls): {len(hole_walls)}")""",
    ),
    Section(
        label="inner_wires_count",
        text="""\
## Faces with N holes — filter by inner_wires count
# Inner wires represent holes/recesses cut into a face. Use to locate
# "the face that has the 4-bolt pattern" or similar.
from build123d import *
plate = (Box(40, 40, 5)
         - Cylinder(2, 5).move(Location((-10, -10, 0)))
         - Cylinder(2, 5).move(Location((10, -10, 0)))
         - Cylinder(2, 5).move(Location((-10, 10, 0)))
         - Cylinder(2, 5).move(Location((10, 10, 0))))
top = plate.faces().sort_by(Axis.Z)[-1]
print(f"top face inner wires (holes): {len(top.inner_wires())}")
faces_with_4_holes = plate.faces().filter_by(lambda f: len(f.inner_wires()) == 4)
print(f"faces with 4 holes: {len(faces_with_4_holes)}")
result = plate""",
    ),
    Section(
        text="""\
## Operator shortcuts — terse but harder to read
# build123d overloads operators on ShapeList. The explicit forms above are
# usually clearer; these shortcuts appear in community examples.
#
#   selector | criterion       →  selector.filter_by(criterion)
#   selector > criterion       →  selector.sort_by(criterion)        # ascending
#   selector < criterion       →  selector.sort_by(criterion, reverse=True)
#   selector >> criterion      →  selector.group_by(criterion)[-1]   # last group
#   selector << criterion      →  selector.group_by(criterion)[0]    # first group
#   selector @ Axis.X          →  filter by position along axis
#
# Example translation:
#   part.faces() > Axis.Z                # = sort_by(Axis.Z), ascending
#   part.faces() | GeomType.CYLINDER     # = filter_by(GeomType.CYLINDER)
#   part.edges() | GeomType.CIRCLE | (lambda e: e.radius < 5)
#                                        # = chained filter_by""",
    ),
    Section(
        text="""\
## Common pitfalls
# 1. Sketch on a non-default plane breaks global axis sort.
#    BuildSketch(Plane.XZ) creates the sketch in local coords (XY of that plane).
#    Sorting by global Axis.Z gives random results because every sketch point
#    has Z=0 in local space. Use the local-axis equivalent (Axis.Y for XZ).
#
# 2. "Order is not guaranteed" — never index unsorted ShapeLists.
#    part.edges()[5] returns A edge, but which one isn't stable across runs
#    or operations. Always .sort_by(...) before slicing if order matters.
#
# 3. Select.NEW is narrower than Select.LAST.
#    For some operations they return the same set; for others NEW excludes
#    edges that existed before but were touched. When in doubt, use LAST.
#
# 4. lambda comparing floats with == fails for computed values.
#    e.length == 20 is unreliable; use abs(e.length - 20) < 1e-3."""
    ),
]


def _build123d_version_banner() -> str:
    """Reflect the actually-installed build123d version so callers know exactly
    which API surface these examples target."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        v = version("build123d")
    except PackageNotFoundError:
        v = "unknown"
    return (
        f"Examples below were tested against build123d {v}, the version installed "
        f"in this environment. If you see API drift (renamed methods, changed "
        f"signatures), check build123d's CHANGELOG against this version."
    )


def build_selectors_cookbook_text() -> str:
    return _build123d_version_banner() + "\n\n" + "\n\n".join(s.text for s in SECTIONS)


RUNNABLE_EXAMPLES: list[tuple[str, str]] = [
    (s.label, s.text) for s in SECTIONS if s.label is not None
]
