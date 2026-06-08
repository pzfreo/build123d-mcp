"""Single source of truth for the build123d presentation cookbook.

Task-indexed: each example answers a real "how do I make a design-discussion
diagram look right?" question. Aimed at colour-coded plan views with filled
feature highlights, a legend, dimensions, and reference axes — the kind of
output you put in a chat, doc, or proposal review.

Differs from build123d://drafting:
  drafting     — engineering drawings for fabrication (DXF handoff, tolerance
                 dims, GD&T feature control frames / datum / surface-finish
                 symbols, multi-view sheets). Two-colour output.
  presentation — discussion diagrams (per-group colour, filled features,
                 legends, axes, titles). Multi-colour output via ExportSVG
                 layers, run from a script outside the MCP sandbox.

Both pipelines share the build123d-native truth property: every line of
part geometry traces back to the 3D model, so a constant change in the
model flows through to every diagram automatically.

Sections with a label are runnable code blocks executed by
tests/test_presentation_cookbook.py — they must end with `result = ...`
or `show(...)` so current_shape is set.
"""

from dataclasses import dataclass


@dataclass
class Section:
    text: str
    label: str | None = None


SECTIONS: list[Section] = [
    Section(
        "BUILD123D PRESENTATION COOKBOOK\n"
        "===============================\n"
        "Code-first design-discussion diagrams: colour-coded plan views with\n"
        "filled features, dimensions, legends, axes, and labels.\n"
        "\n"
        "When to reach for this cookbook (vs build123d://drafting):\n"
        "  - Audience is humans reviewing a design, not a fabricator.\n"
        "  - You want colour-coded groups, filled feature highlights,\n"
        "    a legend, axes for spatial reference, a title.\n"
        "  - The output is for a chat, a doc, a proposal — not a fab handoff.\n"
        "\n"
        "When to reach for build123d://drafting instead:\n"
        "  - Output is an engineering drawing for fabrication.\n"
        "  - You need tolerance dims, GD&T symbols, title block, multi-view\n"
        "    sheets (the drafting helpers cover all of these).\n"
        "  - Two-colour (black part + blue dims) is enough.\n"
        "\n"
        "Pipeline:\n"
        "  1. Build the 3D parts as usual.\n"
        "  2. Project each to a 2D view via shape.project_to_viewport(...).\n"
        "  3. Compose dimensions in build123d.drafting (Draft scaled to your\n"
        "     part size — recipe 1 below; the most important recipe in the doc).\n"
        "  4. Add filled feature highlights and a legend (recipes 3, 4).\n"
        "  5. Add reference axes if useful (recipe 5).\n"
        "  6. Multi-layer SVG via ExportSVG with per-group colours (recipe 2).\n"
        "  7. Rasterise to PNG with resvg if needed (recipe 8).\n"
        "\n"
        "Note on the sandbox: ExportSVG.write() is blocked inside the MCP\n"
        "execute() sandbox, so the multi-layer SVG path requires a small\n"
        "script in your repo that uses build123d directly. The script can\n"
        "still re-use the same constants and assertions as your canonical\n"
        "model — single source of truth is preserved."
    ),
    Section(
        label="scaled_draft",
        text="""\
## Recipe 1: scale Draft to your part size — most important recipe
# Draft defaults are tuned for A4 fabrication drawings:
#   font_size=5.0, arrow_length=3.0, line_width=0.5,
#   pad_around_text=2.0, extension_gap=2.0
# On a part smaller than ~50 mm wide, those defaults make witness lines
# render as thick filled rectangles, arrowheads occlude their labels,
# and witness extension gaps consume more space than the dimension itself.
# Setting font_size alone is NOT enough — you need to scale every parameter.
# Rule of thumb: divide each default by (50 / part_width_mm).
from build123d import *

# Example: a 25-mm-wide part — divide every parameter by 2
draft = Draft(
    font_size=0.9,         # was 5.0
    arrow_length=1.0,      # was 3.0
    line_width=0.05,       # was 0.5  ← biggest visual offender (witness lines)
    pad_around_text=0.4,   # was 2.0
    extension_gap=0.5,     # was 2.0
    decimal_precision=1,
)

# Sanity check: produce a small dim with the scaled config
result = ExtensionLine(
    border=[(0, 0, 0), (10, 0, 0)],
    offset=3,
    draft=draft,
    label="10",
)
show(result, "scaled_dim")""",
    ),
    Section(
        label="layered_svg_export",
        text="""\
## Recipe 2: per-group colour via ExportSVG layers
# render_view(objects="a:red,b:blue") falls back to the 3D pipeline for 2D
# Compounds — output looks chunky and 3D-ish.  For per-group colour, use
# ExportSVG directly with one layer per logical group.
#
# This requires running build123d *outside* the MCP sandbox (the sandbox
# blocks exporter.write()), but you can still verify the assembled
# composition inside the sandbox by show()-ing the final Compound.
from build123d import *

plate = Box(20, 8, 2)
visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))

draft = Draft(font_size=0.9, line_width=0.05, arrow_length=1.0,
              pad_around_text=0.4, extension_gap=0.5, decimal_precision=1)
length_dim = ExtensionLine(border=[(-10, -4, 0), (10, -4, 0)],
                           offset=3, draft=draft, label="20")

# Compose the layered SVG (write() is sandbox-blocked; run from a script)
exporter = ExportSVG(margin=8)
exporter.add_layer("part",
                   line_color=Color(0.10, 0.10, 0.10),
                   line_weight=0.18)
exporter.add_layer("dims",
                   line_color=Color(0.05, 0.20, 0.65),
                   fill_color=Color(0.05, 0.20, 0.65),  # closes "doubled line" arrows
                   line_weight=0.03)
exporter.add_shape(visible, layer="part")
exporter.add_shape(length_dim, layer="dims")
# exporter.write("plate.svg")  # blocked in MCP sandbox; run from your script

result = Compound(children=list(visible) + [length_dim])
show(result, "layered_demo")""",
    ),
    Section(
        label="filled_feature_highlight",
        text="""\
## Recipe 3: filled feature highlights
# Engineering drawings show holes as outlines.  Discussion diagrams benefit
# from filling holes with a pale tint so the eye picks them out.
# Pattern: a 2D Circle Sketch at each feature centre, on its own ExportSVG
# layer with line_color = fill_color = pale_tint, line_weight=0.02.
# Critical: add fills BEFORE part outlines in the exporter so outlines
# render on top.
from build123d import *

# A small plate with two holes — stand-in for a part with mounting features
plate = (Box(20, 10, 2)
         - Cylinder(2, 2).move(Location((-5, 0, 0)))
         - Cylinder(1, 2).move(Location(( 6, 0, 0))))
visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))

# 2D Circles at hole centres
insert_fill = Pos(-5, 0, 0) * Circle(2.0)   # ø4 insert pocket
peg_fill    = Pos( 6, 0, 0) * Circle(1.0)   # ø2 locating peg

# In a real ExportSVG pipeline, each fill goes on its own pale-tint layer:
#   exporter.add_layer("insert_fill",
#                      line_color=Color(0.99, 0.78, 0.72),  # pale coral
#                      fill_color=Color(0.99, 0.78, 0.72),
#                      line_weight=0.02)
#   exporter.add_shape(insert_fill, layer="insert_fill")
#   ... (then add the part outline on top)

result = Compound(children=list(visible) + [insert_fill, peg_fill])
show(result, "filled_features_demo")""",
    ),
    Section(
        label="legend_with_swatches",
        text="""\
## Recipe 4: legend with colour swatches
# A corner colour key.  Each row is a Line on its parent group's colour
# layer (so the swatch picks up the layer's stroke colour automatically),
# plus a Text on a neutral 'labels' layer.
#
# Critical: Text() defaults to align=(Align.CENTER, Align.CENTER) which
# silently makes legend text overlap its swatch (you set the location
# expecting a left edge; you actually get a centre).  Use Align.MIN.
from build123d import *

LEGEND_X, LEGEND_Y  = -11.5, 11.0
SWATCH_LEN, ROW_GAP = 2.5, 1.6
TEXT_GAP, FONT      = 0.4, 0.9

def swatch(y):
    return Line((LEGEND_X, y, 0), (LEGEND_X + SWATCH_LEN, y, 0))

def label(y, text):
    return Location((LEGEND_X + SWATCH_LEN + TEXT_GAP, y - FONT/2, 0)) * Text(
        text, font_size=FONT, align=(Align.MIN, Align.CENTER),
    )

entries = [
    (LEGEND_Y - 0 * ROW_GAP, "lower plate"),
    (LEGEND_Y - 1 * ROW_GAP, "upper cap"),
    (LEGEND_Y - 2 * ROW_GAP, "roller"),
]
swatches = [swatch(y) for y, _ in entries]
labels   = [label(y, t) for y, t in entries]

# In the real ExportSVG pipeline, each swatch goes on its parent's colour
# layer; labels go on a neutral 'labels' layer:
#   exporter.add_shape(swatches[0], layer="lower")    # picks up black
#   exporter.add_shape(swatches[1], layer="upper")    # picks up red
#   exporter.add_shape(swatches[2], layer="roller")   # picks up grey
#   exporter.add_shape(Compound(children=labels), layer="labels")

result = Compound(children=swatches + labels)
show(result, "legend_demo")""",
    ),
    Section(
        label="reference_axes",
        text="""\
## Recipe 5: coordinate axes with ticks
# build123d.drafting doesn't ship axes.  Compose with Line + Text on a
# subtle 'axes' layer.  Default Text alignment (CENTER) is correct for
# axis tick labels (centred under each tick), unlike the legend case.
from build123d import *

AXIS_Y_POS, AXIS_X_POS = 12, -10        # axis positions in world coords
TICK_LEN, LABEL_GAP    = 0.4, 1.0
AXIS_FONT              = 0.8
X_TICKS = list(range(-5, 30, 5))
Y_TICKS = list(range(-5, 6, 5))

axis_parts = [Line((-10, -AXIS_Y_POS, 0), (28, -AXIS_Y_POS, 0))]
for tx in X_TICKS:
    axis_parts.append(Line((tx, -AXIS_Y_POS, 0),
                           (tx, -AXIS_Y_POS - TICK_LEN, 0)))
    axis_parts.append(
        Location((tx, -AXIS_Y_POS - TICK_LEN - LABEL_GAP, 0))
        * Text(str(tx), font_size=AXIS_FONT)   # centre-align is correct here
    )

axis_parts.append(Line((AXIS_X_POS, -7, 0), (AXIS_X_POS, 7, 0)))
for ty in Y_TICKS:
    axis_parts.append(Line((AXIS_X_POS, ty, 0),
                           (AXIS_X_POS - TICK_LEN, ty, 0)))
    axis_parts.append(
        Location((AXIS_X_POS - TICK_LEN - LABEL_GAP - 0.4, ty, 0))
        * Text(str(ty), font_size=AXIS_FONT)
    )

result = Compound(children=axis_parts)
show(result, "axes_demo")""",
    ),
    Section(
        label="title_and_subtitle",
        text="""\
## Recipe 6: title and subtitle
# Place Text at the top of the drawing.  Use Align.CENTER so the title
# self-centres on its anchor x-coordinate.  Pick a font_size proportional
# to the part width — for a 25 mm-wide part, font_size ≈ 1.3 reads well.
from build123d import *

title = Location((9, 14.5, 0)) * Text(
    "Upper cap fixing — Option A1",
    font_size=1.3, align=(Align.CENTER, Align.CENTER),
)
subtitle = Location((9, 13.0, 0)) * Text(
    "verified by clearance() — wall 2.0mm",
    font_size=0.8, align=(Align.CENTER, Align.CENTER),
)

result = Compound(children=[title, subtitle])
show(result, "title_demo")""",
    ),
    Section(
        text="""\
## Recipe 7: layer ordering in the exporter
# Z-order in the SVG matches the order shapes are added to ExportSVG.
# Add layers AND shapes in this order, bottom to top of the visual stack:
#
#   1. Fills           (large pale areas — under everything)
#   2. Part outlines   (primary content)
#   3. Dimensions      (over the parts; blue stands out)
#   4. Axes            (subtle background reference)
#   5. Title / labels  (top of the stack)
#
# Following this order lets fills sit beneath outlines, lets dim labels
# read clearly over the part, and keeps the title prominent.
#
# Concretely:
#   exporter.add_shape(insert_fill, layer="insert_fill")   # 1. fill
#   exporter.add_shape(peg_fill,    layer="peg_fill")
#   exporter.add_shape(lower_plan,  layer="lower")          # 2. outlines
#   exporter.add_shape(upper_plan,  layer="upper")
#   exporter.add_shape(dims,        layer="dims")           # 3. dims
#   exporter.add_shape(axes,        layer="axes")           # 4. axes
#   exporter.add_shape(title_block, layer="title")          # 5. title"""
    ),
    Section(
        text="""\
## Recipe 8: rasterise the SVG to PNG with resvg_py
# build123d emits SVG with mm dimensions.  resvg_py.svg_to_bytes errors
# with "SVG has an invalid size" unless dpi is provided — without dpi,
# resvg can't map physical units (mm) to pixels.  The error message is
# misleading; the fix is one parameter:
#
#   import resvg_py
#   PNG.write_bytes(bytes(resvg_py.svg_to_bytes(
#       svg_string=SVG.read_text(),
#       dpi=300,                          # ← required for mm-unit SVGs
#   )))
#
# Run this from your repo's diagram script — the MCP sandbox blocks
# resvg, so it lives outside the sandbox alongside your ExportSVG call."""
    ),
    Section(
        text="""\
## Decision: which output for which audience
# - Fabrication handoff (DXF to a CNC shop, dimensioned drawing for a
#   vendor): use build123d://drafting.
# - Chat / doc / proposal review (colour-coded, filled features,
#   legend, axes): use this presentation cookbook.
# - Quick eyeball check inside the MCP: render_view's auto-2D path gives
#   clean black-and-blue engineering output for a single 2D Compound.
#
# In all cases, the part geometry comes from the 3D model — never re-type
# coordinates by hand.  That's the truth-from-3D property build123d gives
# you for free; matplotlib doesn't, and that's why it leaks bugs."""
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


def build_presentation_cookbook_text() -> str:
    return _build123d_version_banner() + "\n\n" + "\n\n".join(s.text for s in SECTIONS)


RUNNABLE_EXAMPLES: list[tuple[str, str]] = [
    (s.label, s.text) for s in SECTIONS if s.label is not None
]
