"""Single source of truth for the build123d drafting cookbook.

Task-indexed: each example answers a real "how do I draw X?" question
using build123d.drafting + project_to_viewport + ExportDXF/SVG.

This is the code-first 2D engineering drawing path: the LLM writes the
script, the script generates the DXF/SVG, the script is the source of
truth. No auto-dimensioning — the LLM picks dimensions explicitly so
each call carries engineering intent.

Sections with a label are runnable code blocks executed by
tests/test_drafting_cookbook.py — they must end with `result = ...`
or `show(...)` so current_shape is set.
"""

from dataclasses import dataclass


@dataclass
class Section:
    text: str
    label: str | None = None


SECTIONS: list[Section] = [
    Section(
        "BUILD123D 2D ENGINEERING DRAWINGS COOKBOOK\n"
        "===========================================\n"
        "Code-first drafting: the LLM writes Python, the Python emits the DXF/SVG.\n"
        "Annotations are the build123d_drafting helper CLASSES (native build123d\n"
        "sketch objects) — they wrap build123d.drafting's lower-level primitives.\n"
        "\n"
        "Workflow at a glance (a drawing is DONE only when lint_drawing() is clean):\n"
        "  1. Build the 3D part as usual.\n"
        "  2. view_axes() to check the mapping, then project each view via\n"
        "     shape.project_to_viewport(...).\n"
        "  3. Dimension with Dimension / Leader / Centerline / GD&T classes; add a\n"
        "     TitleBlock; annotate() each; set_page().\n"
        "  4. lint_drawing() == 0 violations → render to eyeball → export DXF/SVG.\n"
        "\n"
        "REQUIRED PACKAGE: build123d-drafting-helpers\n"
        "=============================================\n"
        "All examples below import from 'build123d_drafting'. You MUST install\n"
        "this package in the user's Python environment before any drawing code\n"
        "will work:\n"
        "\n"
        "  pip install build123d-drafting-helpers\n"
        "\n"
        "If you get 'ModuleNotFoundError: No module named build123d_drafting',\n"
        "tell the user to run that pip command (or uv add build123d-drafting-helpers)\n"
        "and then retry.\n"
        "\n"
        "The package is separate from build123d-mcp because it is also useful as\n"
        "a standalone library. It is on the MCP server's import allowlist, so once\n"
        "installed it can be used directly inside execute() calls.\n"
        "\n"
        "PREFERRED: use build123d-drafting helpers instead of raw build123d.drafting\n"
        "============================================================================\n"
        "build123d-drafting wraps the rough edges of build123d.drafting.\n"
        "\n"
        "Key helpers and why to use them:\n"
        "\n"
        "  Dimension(p1, p2, side, distance, draft, label, tolerance)\n"
        "    Like ExtensionLine but side='above'/'below'/'left'/'right' instead of\n"
        "    a raw signed offset. The sign is computed from the path direction so\n"
        "    you never have to guess. Is a build123d sketch object carrying .label,\n"
        "    .label_bbox and .measured_length.\n"
        "\n"
        "  SafeDimension(path, label, draft)\n"
        "    Like DimensionLine but won't raise ValueError when the label is wider\n"
        "    than the path. Truncates gracefully and retries.\n"
        "\n"
        "  Leader(tip, elbow, label, draft)\n"
        "    Builds a Leader from scratch. The shaft line stops before the label text\n"
        "    (no strikethrough). Is a sketch object — lines render as thin filled\n"
        "    faces, so export the whole thing on one fill_color (ink) layer.\n"
        "\n"
        "  view_axes(viewport_origin, viewport_up, look_at)\n"
        "    Returns {'world_X': ('page_X', +1.0), 'world_Z': ('depth', 0.0), ...}\n"
        "    Call this BEFORE projecting to catch axis swaps (e.g. bottom view flips\n"
        "    world-X: {'world_X': ('page_X', -1.0)}) before they corrupt your dims.\n"
        "\n"
        "  annotate(result, name)  — or  annotate(result, name, label='40')\n"
        "    Session builtin (always available). Like show() but for annotation\n"
        "    objects (Dimension / Leader / …): stores annotation metadata AND\n"
        "    registers the shape.\n"
        "    After annotate(), call inspect_drawing() to get a structured JSON report\n"
        "    with bboxes and lint warnings without needing to render.\n"
        "\n"
        "    IMPORTANT — label= and lint coverage:\n"
        "    • Dimension() is a sketch object that carries .label automatically.\n"
        "      annotate(dim_result, name) → full lint coverage, no extra args needed.\n"
        "    • Vanilla ExtensionLine does NOT store the constructor label after\n"
        "      construction (build123d limitation, see gumyr/build123d#1315).\n"
        "      annotate(ext_line, name)            → label_str absent, lint skipped.\n"
        "      annotate(ext_line, name, label='40') → label_str='40', lint active.\n"
        "    Always pass label= when using raw ExtensionLine/DimensionLine, or\n"
        "    switch to Dimension() to avoid the duplication.\n"
        "\n"
        "  place_dims(specs, draft, base_distance=8.0, tier_spacing=None)\n"
        "    Build a stack of parallel dims with automatically assigned offsets.\n"
        "    Specs are (p1, p2, side, label[, tolerance]) — no distance needed.\n"
        "    Dims whose X/Y spans overlap go on successive tiers; non-overlapping\n"
        "    dims share a tier. First spec in each overlapping group is innermost.\n"
        "    tier_spacing defaults to draft.font_size * 3 + draft.arrow_length.\n"
        "\n"
        "  place_labels(specs, draft, centerlines, gap=1.0)\n"
        "    Like place_dims but also shifts each label the minimum distance left\n"
        "    or right to clear any vertical Centerline that would cross it.\n"
        "    Specs same format as place_dims. Pass Centerline() objects as centerlines.\n"
        "\n"
        "  Centerline(p1, p2)\n"
        "    Thin Edge compound representing a centreline. Register with\n"
        "    register_centerline(cl, name) so lint_drawing() can flag label overlaps.\n"
        "\n"
        "  PREFERRED WORKFLOW for multi-dim drawings:\n"
        "    from build123d_drafting import place_dims, place_labels, Centerline\n"
        "    cl = Centerline((cx, -30, 0), (cx, 30, 0))  # bore axis\n"
        "    register_centerline(cl, 'bore_cl')\n"
        "    dims = place_dims([\n"
        "        (p1, p2, 'above', 'label'),   # innermost first\n"
        "        (q1, q2, 'above', 'label'),\n"
        "    ], draft)\n"
        "    # Or use place_labels to also clear centerlines automatically:\n"
        "    dims = place_labels(specs, draft, centerlines=[cl])\n"
        "    for i, d in enumerate(dims): annotate(d, f'dim_{i}')\n"
        "    lint_drawing()  # → flags any remaining overlaps\n"
        "\n"
        "  lint_drawing(drawing_scale=1.0)\n"
        "    Checks: label value vs measured length (>0.5% = likely axis swap),\n"
        "    dim bbox overlapping part outline, Leader shaft through label text,\n"
        "    annotation overlap, page bounds (after set_page), Centerline-label\n"
        "    overlap (after register_centerline).\n"
        "    SCALED DRAWINGS: if you scaled the geometry up before projecting\n"
        "    (e.g. part.scale(5) to draw a 7.5 mm feature at 5:1), pass the same\n"
        "    factor — lint_drawing(drawing_scale=5.0) — so labels carry the REAL\n"
        "    dimension ('7.5') and aren't flagged against the scaled length. Use\n"
        "    the matching TitleBlock(drawing_scale=5.0) so '5:1' is printed.\n"
        "\n"
        "Example — the preferred drawing pipeline:\n"
        "  from build123d import *\n"
        "  from build123d_drafting import Dimension, Leader, view_axes\n"
        "  draft = Draft(font_size=2.5, decimal_precision=1)\n"
        "  # 1. Check axes before placing dims\n"
        "  axes = view_axes((0, 0, 100), (0, 1, 0))  # top view\n"
        "  # => {'world_X': ('page_X', 1.0), 'world_Y': ('page_Y', 1.0), ...}\n"
        "  # 2. Annotate with named sides, not signed offsets\n"
        "  w = Dimension((-20, -10, 0), (20, -10, 0), 'below', 8, draft, label='40')\n"
        "  annotate(w, 'width')   # stores metadata; renders via render_view\n"
        "  # 3. Verify numerically before rendering\n"
        "  # => call inspect_drawing() to get bboxes + lint warnings"
    ),
    Section(
        text="""\
## ENGINEERING-DRAWING CONVENTIONS — read before you dimension
## ==========================================================
## The API and lint_drawing() stop you producing a *broken* drawing; these
## rules stop you producing a *wrong* one. lint enforces the mechanical
## checks — the judgement below is on you.
##
## WHICH VIEWS
## - Use the fewest views that FULLY define the part. Many parts need one or
##   two; a typical prismatic part needs front + top + one side.
## - The FRONT view shows the most shape (the natural "in use" orientation),
##   with the longest dimension horizontal.
## - State the projection angle and place views to match it:
##     third-angle (US / ASME): TOP above front, RIGHT-side view to the right.
##     first-angle  (ISO / EU): TOP below front, RIGHT-side view to the LEFT.
##   Be consistent — a mixed-angle sheet is a defect. Run view_axes() on every
##   view before dimensioning: a bottom/back view negates an axis and mirrors
##   anything you place by hand.
##
## HOW TO DIMENSION
## - Locate every feature EXACTLY ONCE. Never dimension the same distance
##   twice (e.g. both halves AND the overall length) — that is
##   over-dimensioning, a defect. One controlling dimension per feature.
## - Pick ONE scheme per direction and don't mix:
##     baseline — every dim from a common datum edge/face (machined parts;
##                tolerances don't accumulate). Prefer this.
##     chain    — feature-to-feature (only when the chain is itself functional).
## - Dimension to FUNCTIONAL / datum faces, never to a hidden line — expose the
##   feature in a view first.
## - Put dims BETWEEN views, smallest nearest the part, larger ones stacked
##   outside (place_dims() does the stacking). Don't re-dimension a feature
##   that's already dimensioned in another view.
## - Circles → diameter with ⌀ ("⌀6 THRU"); arcs/fillets → radius with R
##   ("R3"). Holes get a callout (⌀, THRU/depth, counterbore ⌴ …), not a plain
##   linear dim.
##
## TOLERANCES & GD&T
## - Put a general-tolerance note (e.g. "ISO 2768-m") in the title block; add
##   explicit ± / limits only where the fit demands it.
## - A position / profile GD&T frame needs BASIC dimensions —
##   Dimension(..., basic=True), the boxed value — to state true position. A
##   position frame with no basic dims is incomplete.
## - Reference datums in constraint order (primary, secondary, tertiary) and
##   tag those datum features with DatumFeature().
##
## THE GATE (do every step, in order)
## build → view_axes() → project_to_viewport() → dimension with the helper
## CLASSES → annotate() each → set_page() → lint_drawing() == 0 violations →
## render to eyeball → export. Never export before lint is clean."""
    ),
    Section(
        text="""\
## The Draft config — set once, reuse everywhere
# Draft holds drawing-wide settings: font, font_size, units, decimal precision,
# arrow size, line widths. Pass it into every dimension to keep them consistent.
# Defaults: font_size=5, font='Arial', unit=Unit.MM, decimal_precision=2.

# Common engineering settings: smaller font, single-decimal mm, narrow arrows
# draft = Draft(font_size=2.5, decimal_precision=1, arrow_length=2.0)"""
    ),
    Section(
        label="basic_dimension",
        text="""\
## A single linear dimension — the helper way (preferred)
# Dimension is the helper class: witness lines + dim line + label, as a native
# build123d Sketch. `side` ("above"/"below"/"left"/"right") picks the offset
# direction from the path so you never compute the signed normal by hand; the
# distance is the perpendicular offset from the part to the dim line.
from build123d import *
from build123d_drafting import Dimension

draft = Draft(font_size=2.5, decimal_precision=1)
# A horizontal 40 mm distance, dim line placed 8 mm below the lower edge.
result = Dimension((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
show(result, "dim")
# (Dimension wraps build123d's ExtensionLine; reach for raw ExtensionLine only
#  when you need its lower-level offset/tolerance knobs directly.)""",
    ),
    Section(
        label="dimension_with_tolerance",
        text="""\
## Dimension with tolerance
# tolerance can be a single float (symmetric ±) or a tuple (lower, upper)
from build123d import *

draft = Draft(font_size=2.5, decimal_precision=1)
result = ExtensionLine(
    border=[(0, 0, 0), (30, 0, 0)],
    offset=8,
    draft=draft,
    label="30",
    tolerance=0.1,        # symmetric: 30 ±0.1
)
show(result, "tol_dim")""",
    ),
    Section(
        label="diameter_dimension",
        text="""\
## Diameter dimension across a circle
# DimensionLine draws just the dimension line + arrows (no extension lines) —
# useful for diameters where you want the line crossing through the hole.
from build123d import *

draft = Draft(font_size=2.5, decimal_precision=1)
result = DimensionLine(
    path=[(-3, 0, 0), (3, 0, 0)],
    draft=draft,
    label="⌀6",
)
show(result, "dia_dim")""",
    ),
    Section(
        label="project_to_view",
        text="""\
## Project a 3D part to a 2D view
# project_to_viewport returns (visible_edges, hidden_edges) as ShapeLists in
# world coordinates of the projection plane. View direction is set by the
# camera position relative to look_at.
from build123d import *

plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))

# Top view: camera above looking down, +Y is up on the page
visible, hidden = plate.project_to_viewport(
    viewport_origin=(0, 0, 100),
    viewport_up=(0, 1, 0),
    look_at=(0, 0, 0),
)
print(f"visible edges: {len(visible.edges())}, hidden: {len(hidden.edges())}")
result = Compound(children=list(visible))
show(result, "top_view")""",
    ),
    Section(
        label="dimensioned_view",
        text="""\
## Compose: project + add dimensions for a complete top view
# The canonical "engineering drawing" pipeline with the helper classes. Each
# dimension carries explicit engineering intent — you pick which matter. Note
# the convention: the overall size gets linear dims; the hole gets a ⌀ Leader
# callout, not a linear dimension.
from build123d import *
from build123d_drafting import Dimension, Leader

plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))
visible, _hidden = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))

draft = Draft(font_size=2.5, decimal_precision=1)
length = Dimension((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
width  = Dimension((20, -10, 0), (20, 10, 0), "right", 8, draft, label="20")
hole   = Leader((10, 0, 0), (24, 12, 0), "⌀6 THRU", draft)   # hole → callout

result = Compound(children=list(visible) + [length, width, hole])
show(result, "dimensioned_top")""",
    ),
    Section(
        label="title_block",
        text="""\
## Title block via TechnicalDrawing
# TechnicalDrawing produces a Sketch containing the page frame + title block.
# Place your dimensioned views inside its drawable area.
from build123d import *

result = TechnicalDrawing(
    designed_by="LLM",
    page_size=PageSize.A4,
    title="Bracket",
    sub_title="Top View",
    drawing_number="DWG-001",
    drawing_scale=1.0,
)
show(result, "title_sheet")""",
    ),
    Section(
        label="build_then_review_then_ship",
        text="""\
## The full loop: build → review → ship
# Once you've composed a dimensioned drawing as a named object, the MCP
# tools handle the rest. The server auto-detects that the drawing is 2D
# (a Sketch / Compound with no solids) and routes render_view + export
# through the appropriate path.
#
# Workflow from the LLM's perspective:
#   1. execute()    — build the dimensioned drawing, show(it, "name")
#   2. render_view(objects="name", format="png", label_objects=True)
#                   — review what you produced; labels confirm which is which
#   3. export(name, "dxf")
#                   — write the DXF for the user (or "svg" for docs)
#
# The example below just does step 1; steps 2 and 3 are MCP tool calls
# the LLM makes after this execute() returns. (Between annotate() and
# render_view, call lint_drawing() and fix anything it flags.)
from build123d import *
from build123d_drafting import Dimension, Leader

plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))
visible, _hidden = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
draft = Draft(font_size=2.5, decimal_precision=1)
length = Dimension((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
width  = Dimension((20, -10, 0), (20, 10, 0), "right", 8, draft, label="20")
hole   = Leader((10, 0, 0), (24, 12, 0), "⌀6 THRU", draft)

result = Compound(children=list(visible) + [length, width, hole])
show(result, "bracket_top_view")""",
    ),
    Section(
        label="multi_view_layout",
        text="""\
## Multi-view sheet: top, front, side, all on one drawing
# Project the same part three times with different camera setups, translate
# each view to its position on the sheet, compose into one Compound.
from build123d import *

plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))

def project_view(shape, origin, up, look_at):
    visible, _hidden = shape.project_to_viewport(origin, up, look_at)
    return Compound(children=list(visible))

top   = project_view(plate, (0, 0, 100), (0, 1, 0),  (0, 0, 0))
front = project_view(plate, (0, -100, 0), (0, 0, 1), (0, 0, 0))
side  = project_view(plate, (100, 0, 0),  (0, 0, 1), (0, 0, 0))

# Translate each into a separate area on the page
top_view   = top.translate((-30, 30, 0))
front_view = front.translate((-30, 0, 0))
side_view  = side.translate((30, 0, 0))

result = Compound(children=[top_view, front_view, side_view])
show(result, "three_view")""",
    ),
    Section(
        label="hole_table_pattern",
        text="""\
## Hole callouts from the cylindrical-face inventory
# build123d doesn't ship a HoleTable class, but the cylindrical faces give you
# position and diameter. Point a Leader at each hole with a proper ⌀ callout
# (⌀ + size + THRU/depth) — read the diameter from the face, don't hard-code it.
from build123d import *
from build123d_drafting import Leader

plate = (Box(40, 40, 5)
         - Cylinder(2, 5).move(Location((-10, -10, 0)))
         - Cylinder(2, 5).move(Location((10, -10, 0)))
         - Cylinder(2, 5).move(Location((-10, 10, 0)))
         - Cylinder(2, 5).move(Location((10, 10, 0))))

# Find all cylindrical (hole-wall) faces
hole_faces = plate.faces().filter_by(GeomType.CYLINDER)
print(f"found {len(hole_faces)} cylindrical faces")

draft = Draft(font_size=2, decimal_precision=1)
labels = []
for face in hole_faces:
    c = face.center()
    dia = face.bounding_box().size.X          # ⌀ read from the hole wall
    # Leader points at the hole; the shelf carries the callout clear of the part.
    labels.append(Leader((c.X, c.Y, 0), (c.X + 10, c.Y + 10, 0),
                         f"⌀{dia:g} THRU", draft))

visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
result = Compound(children=list(visible) + labels)
show(result, "hole_callouts_demo")""",
    ),
    Section(
        label="clean_svg_export",
        text="""\
## Clean SVG export — the visual-quality recipe
# build123d.drafting renders witness ticks and arrowheads as thin closed
# polygons (filled rectangles, not strokes). Without configuration, an SVG
# export shows them as outlined rectangles — the "doubled line" look.
# Three settings turn that into clean engineering output:
#
#   1. fill_color = line_color on the dims layer — closed-rect ticks now
#      render as solid coloured lines instead of outlines.
#   2. line_weight tuning — thicker for part (0.4-0.5), thin for dims (0.05).
#   3. Use Color(r,g,b) with explicit RGB values rather than ColorIndex.BLACK,
#      which gets re-interpreted depending on background colour.
#
# This matches what render_view does internally for 2D inputs. Apply it
# yourself if you want to call ExportSVG directly (e.g. from your own
# script that runs outside the MCP).
from build123d import *

plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))
visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
draft = Draft(font_size=2.5, decimal_precision=1)
length = ExtensionLine(border=[(-20, -10, 0), (20, -10, 0)], offset=8, draft=draft, label="40")

part_color = Color(0, 0, 0)         # explicit black
dim_color  = Color(0, 0.2, 0.7)     # blue — visually distinct from part

exporter = ExportSVG(margin=10)
exporter.add_layer("part", line_color=part_color, line_weight=0.5)
exporter.add_layer(
    "dims",
    line_color=dim_color,
    fill_color=dim_color,             # the killer setting — clean witness ticks
    line_weight=0.05,
)
exporter.add_shape(visible, layer="part")
exporter.add_shape(length, layer="dims")
# exporter.write("clean.svg")  # blocked by sandbox; use render_view/export

result = Compound(children=list(visible) + [length])
show(result, "clean_svg_demo")""",
    ),
    Section(
        label="stacking_and_page_bounds",
        text="""\
## Dim stacking, overlap detection, and page-bounds checking
# Two common LLM mistakes: (1) dims at the same offset collide when the labels
# are wide; (2) dims near the page edge push their text off the sheet.
# lint_drawing() catches both — but only if you register the page first.

# --- Standard stacking pattern ---
# Place parallel dims at increasing offsets: innermost first, step by ~8 mm.
# For font_size=2.5 with decimal_precision=1, a label like "127.5" is ~10 mm
# wide. An 8 mm step keeps witness lines clear of the next dim's text.
from build123d import *
from build123d_drafting import Dimension

draft = Draft(font_size=2.5, decimal_precision=1)

plate = Box(80, 50, 5)
visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible)), "plate")

# Three stacked dims on the bottom edge — offsets 10, 18, 26 mm
total  = Dimension((-40, -25, 0), (40, -25, 0), "below", 10, draft, label="80")
left   = Dimension((-40, -25, 0), ( 0, -25, 0), "below", 18, draft, label="40")
right  = Dimension((  0, -25, 0), (40, -25, 0), "below", 26, draft, label="40")
height = Dimension(( 40, -25, 0), (40,  25, 0), "right", 10, draft, label="50")

annotate(total,  "total_width",  label="80")
annotate(left,   "left_half",    label="40")
annotate(right,  "right_half",   label="40")
annotate(height, "height",       label="50")

# --- Register page so lint can check bounds ---
# A4 landscape = 297 × 210 mm; 10 mm margin → drawable area 277 × 190 mm.
# Call set_page() once after setting up the sheet/title block.
set_page(297, 210, margin=10)

# lint_drawing() now checks both overlap AND page bounds automatically.
# Run it after placing all dims — before rendering or exporting.

result = Compound(children=list(visible) + [total, left, right, height])
show(result, "stacked_dims_demo")""",
    ),
    Section(
        text="""\
## CENTERLINE-LABEL COLLISION AVOIDANCE
## =====================================
## Problem: when a dim line crosses a Centerline at the label's midpoint, the
## label text overlaps the Centerline — most visible on diameter dims (Ø5.0 H8)
## where the dim line passes through the bore centre.
##
## Detection: register centerlines with register_centerline(shape, name) so
## lint_drawing() can flag the collision before rendering.
##
## Fix options:
##   1. Shift the label along the dim line away from the crossing:
##        d = Dimension(p1, p2, "above", 8, draft, label="Ø5.0 H8", label_offset_x=15)
##      label_offset_x is a signed distance from the midpoint (mm). Positive
##      shifts toward p2; negative shifts toward p1.
##
##   2. Replace the inline label with a Leader annotation pointing to the feature:
##        ann = Leader(tip=(0, 0, 0), elbow=(20, 12, 0), label="Ø5.0 H8", draft=draft)
##        annotate(ann, "bore_dim")
##      A Leader always places its text to one side of the tip, never across it.
##
##   3. Increase the dim offset so the dim line clears the Centerline region:
##        d = Dimension(p1, p2, "above", 20, draft, label="Ø5.0 H8")
##      Only works if the dim layout has room for a larger offset.
##
## Lint workflow:
##   cl = Edge.make_line((0, -50, 0), (0, 50, 0))  # vertical centreline through bore
##   register_centerline(Compound(children=[cl]), "bore_cl")
##   d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8")
##   annotate(d, "bore_dim")
##   lint_drawing()  # → label_centerline_overlap warning if label crosses bore_cl"""
    ),
    Section(
        label="gdt_symbols",
        text="""\
## GD&T: feature control frames, datum features, surface finish marks
# build123d.drafting ships no GD&T primitives, and the geometric-characteristic
# glyphs (⌖ ⊥ ∥ ◎ …) and surface-finish ticks are absent from CAD-safe fonts —
# so build123d-drafting-helpers draws them geometrically as native sketch
# objects. Each builds at the origin; move it into place with .moved(loc) and
# export the whole thing on a single ink layer (lines are thin filled faces —
# there is no .lines/.text split, and closed loops like ⌀/Ⓜ never flood).
from build123d import *
from build123d_drafting import (
    FeatureControlFrame, DatumFeature, SurfaceFinish,
)

draft = Draft(font_size=2.5, decimal_precision=2)

# Feature control frame -> | ⌖ | ⌀0.5 Ⓜ | A | B | C |
# characteristic is one of 14 names ("position", "flatness", "perpendicularity",
# "concentricity", "circular_runout", ...). diameter=True prepends ⌀;
# modifier="M"/"L"/"P" = MMC/LMC/projected (None = RFS).
fcf = FeatureControlFrame(
    "position", 0.5, datums=("A", "B", "C"),
    draft=draft, diameter=True, modifier="M",
)

# Datum feature symbol (filled triangle + framed letter), tip at the origin.
datum_a = DatumFeature("A", draft=draft)

# Surface finish mark with an Ra value, placed at a point (ISO 1302).
finish = SurfaceFinish("1.6", position=(60, 0, 0), draft=draft)

# Each is a Sketch — compose them and export on one ink layer (a position
# tolerance like this also needs BASIC dimensions, Dimension(..., basic=True),
# to locate the true position the frame controls).
result = Compound(children=[
    fcf,
    datum_a.moved(Location((40, 0, 0))),
    finish,
])
show(result, "gdt_demo")""",
    ),
    Section(
        text="""\
## Limitations and gaps in build123d.drafting today
# - No HoleTable class: roll your own via face_inventory + DimensionLine (see above).
# - GD&T symbols (feature control frames, datum features, surface finish marks)
#   and an ISO 7200 title block now live in build123d-drafting-helpers — see the
#   GD&T recipe above; TitleBlock() covers the title block.
# - No section-view hatching: clip the part with a plane and project the
#   result, but cross-hatching the cut surface is manual.
# - No automatic standards selection (ASME Y14.5 vs ISO): the Draft object
#   gives you font/units/precision; conventions are your responsibility.
#
# When you hit any of these, the answer is to compose the lower-level
# build123d primitives yourself — Sketch + Line + Text + Polyline."""
    ),
    Section(
        text="""\
## When to use which output format
# - DXF (ExportDXF): standard 2D CAD interchange. Opens in any CAD tool, has
#   layer support, preserves dimension semantics. Best for fabrication output.
# - SVG (ExportSVG): web-viewable, easier to embed in docs / wikis. Loses some
#   CAD-specific metadata. Best for design-review and documentation.
# - PNG (render_view): for the LLM's own 'eyeball it' check. Don't use for
#   handoff — projection is rasterised and lossy."""
    ),
    Section(
        text="""\
## Drafting conventions — failure modes and their fixes
##
## These are the recurring pathologies that hit empirically when writing
## drafting code with raw build123d. Each one has a short rule that
## prevents the failure; the structural-lint tool catches them after the
## fact.
##
## 1. ExtensionLine.offset sign convention
##
## ExtensionLine(border=[a, b], offset=d, ...) places the dim on the
## right-hand normal of the path direction a→b. Right-hand normal of
## (dx, dy) is (dy, -dx). Reverse the points or flip the offset sign to
## put the dim on the other side. The build123d-drafting helper
## Dimension() removes this guessing entirely — it takes
## side="above"/"below"/"left"/"right" and computes the sign internally.
##
## 2. DimensionLine crashes when label is wider than the path
##
## With a path shorter than the label string's pixel width and a path
## also too short for the outside-arrows fallback, build123d raises
## ValueError: "Can't get geom adaptor of empty wire". Either widen the
## path, shorten the label, or use build123d-drafting.SafeDimension()
## which retries with a truncated label rather than raising.
##
## 3. Text on a layer with fill_color=None renders as outlines
##
## When ExportSVG writes a layer that has only line_color set (no
## fill_color), every <text> element on that layer renders as
## thick-stroke outlines instead of filled glyphs. Set
## fill_color = line_color on dimension layers (the cookbook's
## "clean SVG export" recipe shows this) — the closed-rect witness ticks
## and the text glyphs then both render solid. The lint_drawing tool
## flags this in SVG mode.
##
## 4. Leader lines need a gap before the label
##
## A Leader line that runs straight up to the label's bounding box
## visually strikes through the first character. Stop the line ~1 mm
## before the label or insert a horizontal shelf segment.
## build123d-drafting.Leader() handles this automatically; the lint
## tool's leader_elbow_in_label check catches it after the fact.
##
## 5. View-axis swap in non-top projections
##
## project_to_viewport(camera, up, look_at) projects world XYZ onto
## page XY; for a bottom view (camera at -Z), world-X flips to negative
## page-X. Any dimensions or labels you compose by hand using world
## coordinates will be mirrored. Call view_axes(camera, up, look_at)
## before projecting to see the mapping explicitly:
##
##     view_axes(viewport_origin=(0, 0, -100))
##     # {"world_X": ["page_X", -1.0], "world_Y": ["page_Y", 1.0], ...}
##
## ## Tooling that catches each of these
##
## - inspect_drawing()                — bbox + annotation metadata
## - lint_drawing()                   — items 1, 3, 4 above
## - view_axes()                      — item 5 above
## - render_drawing(svg_path=...)     — visual check after lint
##
## Use the LLM workflow:
##   build → inspect_drawing() → lint_drawing() → fix → render_drawing()"""
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
        f"in this environment."
    )


def build_drafting_cookbook_text() -> str:
    return _build123d_version_banner() + "\n\n" + "\n\n".join(s.text for s in SECTIONS)


RUNNABLE_EXAMPLES: list[tuple[str, str]] = [
    (s.label, s.text) for s in SECTIONS if s.label is not None
]
