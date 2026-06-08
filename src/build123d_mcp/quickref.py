"""Single source of truth for the build123d quick reference.

Each Section with a label is a fully self-contained, executable code block.
Prose-only sections (no label) are reference documentation that cannot be run.

build_quickref_text() assembles all sections into the MCP resource text.
RUNNABLE_EXAMPLES is the list used by tests/test_quickref.py.
"""

from dataclasses import dataclass


@dataclass
class Section:
    text: str
    label: str | None = None  # None = prose only, not tested


SECTIONS: list[Section] = [
    Section(
        "BUILD123D QUICK REFERENCE — all measurements in mm\n"
        "===================================================="
    ),
    Section(
        label="pattern1",
        text="""\
## Pattern 1: algebra mode (functional composition with operators)
# Use for: one-off shapes, quick composition, when you don't need selectors
# during construction. Each statement returns a new shape.
from build123d import *
result = Box(20, 10, 5)
result = result - Cylinder(3, 6).move(Location((0, 0, 0)))
show(result, "part")""",
    ),
    Section(
        label="pattern2",
        text="""\
## Pattern 2: builder mode (BuildPart context manager)
# Use for: multi-step parts, anywhere you need extrude/revolve/loft/fillet,
# or when you want Select.LAST to grab just-added geometry.
from build123d import *
with BuildPart() as p:
    Box(20, 10, 5)
    Cylinder(3, 6, mode=Mode.SUBTRACT)   # cut hole
result = p.part
show(result, "part")""",
    ),
    Section(
        text="""\
## Primitives
Box(length, width, height)              # centred at origin
Cylinder(radius, height)
Sphere(radius)
Cone(bottom_radius, top_radius, height)
Torus(major_radius, minor_radius)""",
    ),
    Section(
        text="""\
## Boolean operators (direct algebra)
a + b    # union
a - b    # cut b from a
a & b    # intersection""",
    ),
    Section(
        text="""\
## Boolean modes (inside BuildPart)
mode=Mode.ADD        # default — union with existing solid
mode=Mode.SUBTRACT   # cut from existing solid
mode=Mode.INTERSECT  # keep overlap only
mode=Mode.REPLACE    # replace current solid entirely""",
    ),
    Section(
        label="align",
        text="""\
## Positioning
# Alignment — corner vs centred
from build123d import *
corner = Box(10, 5, 3, align=(Align.MIN, Align.MIN, Align.MIN))        # corner at origin
result = Box(10, 5, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))  # centred XY, bottom at Z=0""",
    ),
    Section(
        label="translate",
        text="""\
# Translate (and optionally rotate)
from build123d import *
shape = Box(10, 5, 3)
result = shape.move(Location((5, 0, 0)))
rotated = shape.move(Location((5, 0, 0), (0, 0, 45)))""",
    ),
    Section(
        label="extrude",
        text="""\
## Sketch → solid (requires BuildPart)
# Extrude
from build123d import *
with BuildPart() as p:
    with BuildSketch() as sk:            # default plane: XY
        Circle(10)
        Rectangle(6, 6, mode=Mode.SUBTRACT)   # cutout in sketch
    extrude(amount=15)
result = p.part""",
    ),
    Section(
        label="revolve",
        text="""\
# Revolve — profile in Plane.XZ, offset from axis
from build123d import *
with BuildPart() as p:
    with BuildSketch(Plane.XZ) as sk:
        with Locations((12, 0)):
            Rectangle(4, 8)
    revolve(axis=Axis.Z)                 # full 360°
result = p.part""",
    ),
    Section(
        label="selectors",
        text="""\
## Selecting edges and faces
from build123d import *
result = Box(20, 10, 5)
top_face    = result.faces().sort_by(Axis.Z)[-1]        # highest-Z face
bottom_face = result.faces().sort_by(Axis.Z)[0]         # lowest-Z face
top_edges   = result.edges().sort_by(Axis.Z)[-4:]       # 4 edges at highest Z (for fillet)
z_edges     = result.edges().filter_by(Axis.Z)          # edges parallel to Z
flat_faces  = result.faces().filter_by(GeomType.PLANE)  # planar faces only""",
    ),
    Section(
        label="fillet_chamfer",
        text="""\
## Fillets and chamfers (inside BuildPart only)
from build123d import *
with BuildPart() as p:
    Box(20, 10, 5)
    fillet(p.part.edges().sort_by(Axis.Z)[-4:], radius=1)
result = p.part

from build123d import *
with BuildPart() as p:
    Box(20, 10, 5)
    chamfer(p.part.edges().sort_by(Axis.Z)[-4:], length=0.5)
result = p.part""",
    ),
    Section(
        label="joints_rigid",
        text="""\
## Joints — assembly relationships
# Joints express how parts CONNECT, not just where they happen to sit. Move the
# parent, the child follows. Reach for joints when building assemblies — they
# scale better than raw .move() because the relationship survives changes.
from build123d import *
plate = Box(20, 20, 5)
RigidJoint("mount", to_part=plate, joint_location=Location((0, 0, 2.5)))

pin = Box(2, 2, 10)
RigidJoint("base", to_part=pin, joint_location=Location((0, 0, -5)))

# Snap pin's "base" joint to plate's "mount" joint. pin is now positioned
# so its joint coincides with the plate's. Move plate later → pin follows.
plate.joints["mount"].connect_to(pin.joints["base"])
show(plate, "plate")
show(pin, "pin")""",
    ),
    Section(
        text="""\
## Joint types
RigidJoint(label, to_part, joint_location)              # fixed (no DOF)
RevoluteJoint(label, to_part, axis, angular_range)      # hinge (1 rotation)
LinearJoint(label, to_part, axis, linear_range)         # slider (1 translation)
CylindricalJoint(label, to_part, axis, ...)             # rotate + translate same axis
BallJoint(label, to_part, joint_location, angular_range) # 3 rotations, 0 translations

# For movable joints, pass position/angle to connect_to() to set the configuration:
#   plate.joints["hinge"].connect_to(arm.joints["pivot"], angle=45)
#   rail.joints["slot"].connect_to(carriage.joints["slide"], position=10)""",
    ),
    Section(
        label="grid_locations",
        text="""\
## Pattern placement: GridLocations — N×M array of features
# Inside a BuildPart, GridLocations(x_spacing, y_spacing, x_count, y_count)
# acts as a context that places every operation inside it at every grid point.
# This is the idiomatic "4 holes in a 2x2 grid" — never write a manual for loop.
from build123d import *
with BuildPart() as p:
    Box(40, 40, 5)
    with GridLocations(20, 20, 2, 2):
        Hole(radius=2, depth=5)
result = p.part
show(result, "plate_with_grid_holes")""",
    ),
    Section(
        label="polar_locations",
        text="""\
## Pattern placement: PolarLocations — radial arrays (bolt circles)
# PolarLocations(radius, count, start_angle=0, angular_range=360) places features
# evenly around a circle. Each item is also rotated to match its angular position.
from build123d import *
with BuildPart() as p:
    Cylinder(20, 5)
    with PolarLocations(15, 6):  # 6 holes evenly around radius 15
        Hole(radius=1.5, depth=5)
result = p.part
show(result, "flange")""",
    ),
    Section(
        label="manual_locations",
        text="""\
## Pattern placement: Locations — explicit coordinate list
# When the placements aren't a regular array, list them. Each tuple is (x, y, z).
# Locations also accepts Location objects for full position+rotation control.
from build123d import *
with BuildPart() as p:
    Box(40, 20, 5)
    with Locations((10, 5, 0), (-10, -5, 0), (0, 0, 0)):
        Hole(radius=1, depth=5)
result = p.part
show(result, "plate_with_3_holes")""",
    ),
    Section(
        label="position_tangent_at",
        text="""\
## Chaining curves with @ (position) and % (tangent)
# `edge @ t` returns the position at parameter t (0..1 along the edge).
# `edge % t` returns the tangent direction at t. Use these to build the next
# curve from the previous one's endpoint+direction without repeating coordinates.
from build123d import *
with BuildLine() as l:
    l1 = Line((0, 0), (10, 0))
    l2 = JernArc(start=l1 @ 1, tangent=l1 % 1, radius=5, arc_size=90)
    l3 = Line(l2 @ 1, (l2 @ 1) + (10, 0))
result = l.line
show(result, "chained_path")""",
    ),
    Section(
        label="op_sweep",
        text="""\
## sweep — drag a profile along a path
# Use for tubing, mouldings, anything with constant cross-section along a curve.
from build123d import *
with BuildLine() as path:
    Line((0, 0, 0), (0, 0, 20))
with BuildSketch() as profile:
    Circle(2)
result = sweep(profile.sketch, path=path.line)
show(result, "tube")""",
    ),
    Section(
        label="op_loft",
        text="""\
## loft — bridge between two or more profiles
# Each BuildSketch becomes a section; loft() connects them in order. Profiles
# can differ in shape (rectangle to circle = transition piece).
from build123d import *
with BuildPart() as p:
    with BuildSketch(Plane.XY) as bottom:
        Rectangle(20, 20)
    with BuildSketch(Plane.XY.offset(20)) as top:
        Circle(5)
    loft()
result = p.part
show(result, "transition")""",
    ),
    Section(
        label="op_mirror",
        text="""\
## mirror — reflect a shape across a plane
# Build half a symmetric part, then mirror. Faster and less error-prone than
# building both halves. mirror() does not union — combine with + or BuildPart.
from build123d import *
half = Box(10, 5, 3).move(Location((5, 0, 0)))   # off-centre half
result = half + mirror(half, about=Plane.YZ)
show(result, "symmetric")""",
    ),
    Section(
        label="op_offset",
        text="""\
## offset — inset/outset a sketch or shell a solid
# In BuildSketch, offset(amount=...) grows or shrinks the boundary.
# On a 3D shape, offset_3d / shell creates a hollow version (walls of thickness).
from build123d import *
with BuildSketch() as s:
    Rectangle(10, 10)
    offset(amount=2)            # +2 outward; negative inward
result = thicken(s.sketch, amount=1)   # turn into a 3D shell-base
show(result, "offset_plate")""",
    ),
    Section(
        label="op_thicken",
        text="""\
## thicken — turn a 2D face into a solid by adding thickness
# Common for shell parts where you sketch the surface and add wall thickness.
from build123d import *
sketch = Rectangle(20, 10)
result = thicken(sketch, amount=3)
show(result, "thickened_plate")""",
    ),
    Section(
        label="mode_private",
        text="""\
## Mode.PRIVATE — helper geometry that doesn't join the part
# Useful for construction shapes you want to reference (e.g. for selectors
# or measurements) without their volume contributing to the final part.
# Default Mode.ADD unions; Mode.SUBTRACT cuts; Mode.PRIVATE does neither.
from build123d import *
with BuildPart() as p:
    Box(20, 20, 5)
    helper = Cylinder(3, 5, mode=Mode.PRIVATE)   # for reference only
    # `helper` exists as a Solid you can query; p.part doesn't include it
result = p.part
show(result, "part_unchanged_by_private")""",
    ),
    Section(
        text="""\
## MCP server conventions
- Name the final shape 'result' OR call show() — both trigger current_shape auto-detection
- show(shape, "name")      registers object, prints vol + face count as immediate confirmation
- named_face(shape, "top") returns the highest-Z face; also: bottom/front/back/left/right""",
    ),
    Section(
        text="""\
## Common gotchas
- After every -, +, & : call measure() and check topology.faces — a failed boolean leaves counts unchanged
- fillet/chamfer radius too large → OCC kernel exception; reduce radius or select fewer edges
- Cylinder/Sphere are centred at origin; use .move() or align= to reposition
- Locations() inside BuildPart shifts the construction origin — it does NOT move the whole part
- Pass p.part (the Shape) to show(), not p (the BuildPart context)
- revolve() needs the profile offset from the revolution axis — a profile touching the axis produces a solid, one crossing it fails""",
    ),
]


def _build123d_version_banner() -> str:
    """Reflect the actually-installed build123d version so callers know exactly
    which API surface these examples target. The version may differ from the
    pyproject.toml pin if the user manually overrode it."""
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


def build_quickref_text() -> str:
    return _build123d_version_banner() + "\n\n" + "\n\n".join(s.text for s in SECTIONS)


RUNNABLE_EXAMPLES: list[tuple[str, str]] = [
    (s.label, s.text) for s in SECTIONS if s.label is not None
]
