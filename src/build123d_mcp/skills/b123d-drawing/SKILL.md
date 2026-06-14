# Create Engineering Drawing from build123d Geometry

Use this skill when asked to create or fix an engineering drawing for a
build123d component. Drawings export to a project `drawings/` output directory.

**There are two paths — start with the automatic one.**

1. **Automatic (`make_drawing`)** — one call turns a part (or STEP file) into a
   four-view SVG + DXF with dimensions, centrelines, and an ISO 7200 title
   block. Use this first for almost every part.
2. **Builder (`build_drawing`)** — the same pipeline, but it hands back a live
   `Drawing` you can edit (add/remove dimensions, add section/auxiliary views)
   *before* export. Use this when the automatic drawing needs tweaks.
3. **Manual pipeline** — build every view and annotation by hand. Only needed
   for cases the builder cannot express (e.g. a true cut section). Documented at
   the end as a fallback.

Requires `build123d-drafting-helpers >= 0.7.0` (automatic hole callouts,
patterns, location dims, and section views).

---

## Step 0 — Understand the part first

Before drawing anything, build and inspect the geometry:

```
mcp__build123d-mcp__execute  — build the part in the session
mcp__build123d-mcp__measure  — confirm volume, bbox, face count
mcp__build123d-mcp__render_view (save_to='/tmp/preview.png') — visual sanity check
```

In the build step, register the part under a stable name with `show(part, "part")`
— the manual pipeline's `suggest_view_layout(object_name="part")` and lint step
reference it by that name.

Note the bounding-box extents and whether the part is rotationally symmetric.
This tells you whether the automatic drawing will capture the key features, and
drives any manual layout decisions later.

---

## Step 1 — Generate the drawing automatically (start here)

In the same `execute()` session where you built the part:

```python
from build123d_drafting import make_drawing

svg, dxf = make_drawing(
    part,                       # an in-session build123d object, OR a "path/to/part.step"
    out="drawings/bracket",     # output stem; ".svg"/".dxf" are appended
    title="BRACKET",            # ISO 7200 document title
    number="DWG-042",           # ISO 7200 document identifier
    tolerance="ISO 2768-f",     # general tolerance
    drawn_by="Your Name",
)
```

`make_drawing` chooses the scale + ISO page size, projects front/plan/side/iso
views, and annotates automatically — then lints and writes both SVG and DXF.
Pass an in-memory object directly (no STEP round-trip) or a STEP path.

As of helpers v0.7.0 the automatic annotation covers **prismatic parts in
full**: every recognised hole gets a grouped callout ("4× ø10 THRU",
counterbore/depth symbols), bolt circles get "EQ SP ON øD BC" callouts with a
pitch-circle centreline, linear arrays get pitch dims, every hole gets a
centre mark and baseline X/Y location dims from the min-X/Y datum corner, and
blind/counterbored holes trigger an automatic SECTION A–A. Turned parts get
OD/length dims, centrelines, and bore leaders. **Do not clear and re-place
these annotations** — edit only what is wrong or missing.

Then verify (Step 3). For most parts you are done here.

What `make_drawing` does **not** do: functional datum selection, GD&T,
tolerances by fit, thread callouts, spiral/freeform profile dimensions,
cross-axis hole *location* dims, pattern callouts on turned flanges, and
section hatching. If the lint reports `feature_not_dimensioned` /
`feature_count_mismatch`, something was skipped for page-space reasons — add
just that callout in Step 2.

The default location datum is the part's minimum-X/Y corner; if the
functional datum differs, re-anchor those dims in Step 2 rather than
rebuilding the sheet.

---

## Step 2 — Customise with the Drawing builder

When the automatic drawing is close but needs edits, swap `make_drawing` for
`build_drawing`. It runs the identical pipeline but returns a live `Drawing`
**before** export, so your edits land in the output. For exact signatures and
keyword names of every class and function used below (`Dimension`, `Leader`,
`TitleBlock`, `Drawing` methods, ...), read the `build123d://drafting-api`
resource — it is generated from the installed library, so it always matches:

```python
from build123d_drafting import build_drawing, Leader

dwg = build_drawing(part, out="drawings/bracket", title="BRACKET",
                    number="DWG-042", tolerance="ISO 2768-f", drawn_by="Your Name")

# Available on dwg:
#   dwg.views        {"front","plan","side","iso"} → (visible, hidden) compounds
#   dwg.annotations  mutable list of annotation objects
#   dwg.draft / dwg.scale / dwg.page_w / dwg.page_h
#   dwg.look_at / dwg.dist   (scaled-space building blocks for custom cameras)
#   dwg.at(view, x, y, z)    → page point (px, py, 0) mapped from world coordinates

# Add a dimension/leader the automatic pass missed:
dwg.add(Leader(tip=dwg.at("front", 10, 0, 5), elbow=(8, 40, 0),
               label="ø4 BORE", draft=dwg.draft), "ldr_bore")

# Drop an automatic annotation by name. Auto names: dim_height, dim_od, dim_width,
# centerline_front, centerline_side, ldr_z0…, dim_step_0…, title_block.
dwg.remove("dim_od")

# Re-lint after edits, then export:
issues = dwg.lint()                       # list of LintIssue; [] when clean
svg, dxf = dwg.export("drawings/bracket")
```

`make_drawing(...)` is exactly `build_drawing(...).export()`.

**Add a section or auxiliary view** with `add_view()`. Give it a (pre-cut)
shape, a camera in **scaled** space (compose it from `dwg.look_at` and
`dwg.dist`, the same convention the standard views use), an up vector, and a
page position; it returns that view's coordinate helper (`ViewCoordinates`):

```python
look = dwg.look_at
bottom = (look[0], look[1], look[2] - dwg.dist)        # camera below the part
vc = dwg.add_view("bottom", part, bottom, (0, 1, 0), (260.0, 60.0))
px, py = vc.pp(world_x, world_y, world_z)              # annotate using the helper
```

Drop to the **manual pipeline** (below) only when even the builder cannot
express what you need — most commonly a true cut/section where you must boolean
the part against a cutting box yourself, or a fully bespoke multi-part sheet.

---

## Step 3 — Verify

```
mcp__build123d-mcp__render_drawing(svg_path='drawings/part_name.svg', save_to='/tmp/dwg.png')
```

Review the rendered PNG before moving on: check the views are upright and
complete, dimensions are legible and not colliding, and the title block reads
correctly. `view_annotation_overlap` warnings from the lint step usually show up
here as cramped leaders — fix them with the builder (Step 2) if they matter.

Then preserve the label metadata and check the exported file itself:

```
mcp__build123d-mcp__save_drawing_annotations(svg_path='drawings/part_name.svg')
mcp__build123d-mcp__inspect_drawing(svg_path='drawings/part_name.svg')
```

build123d renders text as glyph paths, so label strings are irrecoverable from
a finished SVG — `save_drawing_annotations` writes a `.dims.json` sidecar that
`inspect_drawing(svg_path=...)` reads back, letting you (or a later session)
inspect page size, layers, and annotation content without rebuilding anything.

The sidecar only captures annotations registered **in this session** via
`annotate()`. If the SVG was produced by a standalone script (Step 4), the
annotations live in that script's process and the tool will tell you nothing
was written — re-exporting the sidecar is then the script's job.

---

## Step 4 — Save a standalone regeneration script (default)

Unless the user opts out, also write a clean, committable script to
`scripts/drawings/<part>.py` that regenerates the drawing in a single run. The
drawing should live in version control as reproducible code, not only as output
artifacts. This must be a tidy reproducible script — **not** a dump of your
exploratory `execute()` session.

If the project already has a conflicting name at that path (e.g. a
`scripts/drawings.py` module), do **not** create a `scripts/drawings/`
directory alongside it — pick a non-colliding path such as
`scripts/<part>_drawing.py` and follow the project's existing script layout.

Pick the case that matches how you obtained the geometry:

**A — Drawing from a STEP file** → use `generate_script()`. It writes an
editable `build_drawing` script (including the customise-before-export seam)
that reloads the STEP from disk:

```python
from build123d_drafting import generate_script

generate_script(
    "path/to/part.step",
    out="scripts/drawings/bracket",   # → writes scripts/drawings/bracket.py
    title="BRACKET", number="DWG-042",
    tolerance="ISO 2768-f", drawn_by="Your Name",
)
```

The generated script exports its SVG/DXF **next to itself** (the `out=_stem`
line uses the script's own path), e.g. `scripts/drawings/bracket.svg`. If you
want the outputs under `drawings/` instead, edit the final `dwg.export(...)`
line in the generated script.

**B — Drawing an in-session object** → `generate_script()` cannot embed a live
object (it reloads geometry from disk and raises `TypeError` on a `Shape`).
Write the script by hand so it is self-contained:

1. Reconstruct the part — import the project's part-building module if one
   exists (preferred), otherwise inline the minimal construction code.
2. Call `make_drawing` / `build_drawing` with the same parameters you used.

```python
#!/usr/bin/env python3
"""BRACKET — regenerates drawings/bracket.svg + .dxf in one run."""
from build123d_drafting import make_drawing
from myproject.bracket import build_bracket   # the part's source of truth

part = build_bracket()
make_drawing(part, out="drawings/bracket", title="BRACKET",
             number="DWG-042", tolerance="ISO 2768-f", drawn_by="Your Name")
```

If the part was built ad-hoc in the session with no importable source, export it
to STEP once and use case A instead — that keeps the script reproducible without
pasting scratch geometry code.

Tell the user where the script was saved.

---
---

# Manual pipeline (fallback)

Use this **only** when `make_drawing` / `build_drawing` cannot express the
drawing (true cut sections, bespoke layouts). It builds every view and
annotation by hand. Step 0 above still applies; the steps below replace
Steps 1–3.

## Manual 1 — Choose views (third-angle projection)

Standard four-view layout. Page size is chosen in Manual 2 based on part extents
(A4 landscape 297 × 210 mm for most parts; A3 landscape 420 × 297 mm for large ones).

| View | Camera position (scaled space) | Up vector | Role |
|------|-------------------------------|-----------|------|
| Front (along -Y) | `(cxs, cys − DIST, czs)` | `(0, 0, 1)` | primary dims |
| Side (along +X)  | `(cxs + DIST, cys, czs)` | `(0, 0, 1)` | depth/bore |
| Plan (along +Z)  | `(cxs, cys, czs + DIST)` | `(0, 1, 0)` | footprint |
| Isometric        | `(cxs+ID, cys+ID, czs+ID)`         | `(0, 0, 1)` | pictorial, no dims |

where `cxs = cx * SCALE`, `cys = cy * SCALE`, `czs = cz * SCALE`,
`DIST = bbox_max * SCALE + 100` (orthographic cameras always outside the scaled bbox),
and `ID = DIST / (3 ** 0.5)` (iso camera at the same distance along the equal-axis diagonal).

**Critical:** view direction = `look_at − camera`. For a pure orthographic projection the
camera's off-axis coordinates must equal the scaled centroid — using `(0, -DIST, 0)` instead
of `(cxs, cys - DIST, czs)` introduces a silent tilt whenever the centroid is off-axis.

The iso camera uses equal `+ID` offsets on all three axes for a standard equal-axis view.
Negate one axis (e.g. `(cxs-ID, cys+ID, czs+ID)`) to flip the pictorial orientation when
a key feature is otherwise hidden.

Axis mapping verification and sheet-position layout are both done in Manual 2 once SCALE
is known — see the `view_axes` and `suggest_view_layout` calls there.

---

## Manual 2 — Choose page size and scale, then project

Use `choose_scale()` from `build123d_drafting` — it applies the same thresholds as
the automated pipeline and returns `TB_W` (title-block width) too:

```python
from build123d_drafting import choose_scale

# Extract geometry from part — drives all layout decisions below.
_bb   = part.bounding_box()
x_size = _bb.max.X - _bb.min.X
y_size = _bb.max.Y - _bb.min.Y
z_size = _bb.max.Z - _bb.min.Z
cx = (_bb.min.X + _bb.max.X) / 2
cy = (_bb.min.Y + _bb.max.Y) / 2
cz = (_bb.min.Z + _bb.max.Z) / 2
bbox_max = max(x_size, y_size, z_size)

SCALE, PAGE_W, PAGE_H, TB_W = choose_scale(x_size, y_size, z_size)
```

Compute sheet positions using the `suggest_view_layout` MCP tool — it accounts for the
title block footprint and warns when views collide with it or with each other:

```
# TB_W comes from choose_scale() — do NOT hardcode 150.0, it is 120.0 on A4.

mcp__build123d-mcp__suggest_view_layout(
    object_name="part",        # name passed to show() in Step 0
    page_w=PAGE_W, page_h=PAGE_H, scale=SCALE,
    title_block_w=TB_W,
    title_block_h=24,          # ISO 7200 title block height with revision + legal_owner rows
)
```

If the part is not in the session (e.g. the import failed or timed out), pass
`extents=[x_size, y_size, z_size]` (+ optional `centroid=[x, y, z]`) instead of
`object_name` — the layout only needs the bounding box, not live geometry.

Check `result["warnings"]` — if any view overlaps the title block or another view, the
tool says so and may suggest a smaller scale or larger page. Address warnings before
continuing. Then extract the positions:

```python
# Extract positions from suggest_view_layout result.
# Re-run suggest_view_layout above if the part geometry changes before Manual 2.
FV_X, FV_Y   = <result["views"]["front"]["VIEW_X"]>, <result["views"]["front"]["VIEW_Y"]>
SV_X, SV_Y   = <result["views"]["side"]["VIEW_X"]>,  <result["views"]["side"]["VIEW_Y"]>
PV_X, PV_Y   = <result["views"]["plan"]["VIEW_X"]>,  <result["views"]["plan"]["VIEW_Y"]>
ISO_X, ISO_Y = <result["views"]["iso"]["VIEW_X"]>,   <result["views"]["iso"]["VIEW_Y"]>
```

Then project:

```python
part_scaled = part.scale(SCALE)

# Scaled centroid — used as look_at AND as the camera's off-axis coordinates.
# Off-axis coords must match look_at so view direction stays pure-orthographic.
cxs, cys, czs = cx * SCALE, cy * SCALE, cz * SCALE
look_at_s = (cxs, cys, czs)
DIST = bbox_max * SCALE + 100          # camera always outside the scaled bbox
ID   = DIST / (3 ** 0.5)               # iso offset — same distance along equal-axis diagonal

# Per-view camera positions: only the on-axis component differs.
# Remove entries for views you don't need.
VIEWS = {
    "front": ((cxs, cys - DIST, czs), (0, 0, 1)),  # looking along +Y
    "side":  ((cxs + DIST, cys, czs), (0, 0, 1)),  # looking along -X
    "plan":  ((cxs, cys, czs + DIST), (0, 1, 0)),  # looking down -Z
}
VIEW_POS = {
    "front": (FV_X, FV_Y),
    "side":  (SV_X, SV_Y),
    "plan":  (PV_X, PV_Y),
}

# Project and place each orthographic view; collect results by name.
view_proj = {}  # {name: (placed_vis, placed_hid_or_None)}
for view_name, (camera_pos, up) in VIEWS.items():
    vis, hid = part_scaled.project_to_viewport(camera_pos, up, look_at_s)
    if not list(vis):
        raise ValueError(f"project_to_viewport returned empty geometry for {view_name} camera {camera_pos}")
    vx, vy = VIEW_POS[view_name]
    placed     = Compound(children=list(vis)).locate(Location((vx, vy, 0)))
    placed_hid = Compound(children=list(hid)).locate(Location((vx, vy, 0))) if hid else None
    view_proj[view_name] = (placed, placed_hid)

# Iso uses the scaled part and a distance-safe offset on all three axes.
iso_vis, iso_hid = part_scaled.project_to_viewport(
    (cxs + ID, cys + ID, czs + ID), (0, 0, 1), look_at_s
)
iso   = Compound(children=list(iso_vis)).locate(Location((ISO_X, ISO_Y, 0)))
iso_h = Compound(children=list(iso_hid)).locate(Location((ISO_X, ISO_Y, 0))) if iso_hid else None
```

Now verify the axis mapping for each view. `cxs`/`cys`/`czs` are available here; substitute
the actual numeric values for `look_at`:

```
mcp__build123d-mcp__view_axes(viewport_origin=[cxs, cys-DIST, czs], viewport_up=[0,0,1], look_at=[cxs,cys,czs])
mcp__build123d-mcp__view_axes(viewport_origin=[cxs+DIST, cys, czs], viewport_up=[0,0,1], look_at=[cxs,cys,czs])
mcp__build123d-mcp__view_axes(viewport_origin=[cxs, cys, czs+DIST], viewport_up=[0,1,0], look_at=[cxs,cys,czs])
```

Copy the results into the script as comments — they are the source of truth for the
coordinate helpers in Manual 3.

---

## Manual 3 — Coordinate helpers

Write one pair of helpers per view so annotation coords are derived from world
geometry, not hardcoded page numbers. Use the `view_axes` results from Manual 2
to get the signs right — never assume:

```python
# Front view (camera along −Y): world_X → page_X (+1), world_Z → page_Y (+1)
def FX(x): return FV_X + (x - cx) * SCALE
def FZ(z): return FV_Y + (z - cz) * SCALE

# Side view (camera along +X): world_Y → page_X (+1), world_Z → page_Y (+1)
def SX(y): return SV_X + (y - cy) * SCALE
def SZ(z): return SV_Y + (z - cz) * SCALE
```

For ISO views (two world axes share one page axis), use `ViewCoordinates.pp()` instead:

```python
from build123d_drafting import ViewCoordinates, view_axes
# ViewCoordinates(axes, view_x, view_y, cx, cy, cz, scale)
iso_vc = ViewCoordinates(
    view_axes((cxs + ID, cys + ID, czs + ID), (0, 0, 1), look_at_s),
    ISO_X, ISO_Y, cx, cy, cz, SCALE,
)
page_x, page_y = iso_vc.pp(world_x, world_y, world_z)
```

Verify a known extent (e.g. top of part) maps to a sensible page Y before placing
any annotation.

---

## Manual 4 — Annotate with build123d_drafting

```python
from build123d_drafting import (
    Centerline, Dimension, Leader, TitleBlock,
    annotate, draft_preset, lint_drawing, place_dims, set_page,
)

draft = draft_preset(font_size=2.5, decimal_precision=1)
```

**Centrelines and bore leaders for rotationally symmetric parts** (Z-axis cylinders):

```python
# Z-axis diameters, largest first (or build the list by hand):
from build123d_drafting import analyse_cylinders, dedup_diams
z_diams = dedup_diams(analyse_cylinders(part)[0])
if z_diams:
    # Rotation-axis centreline in front and side views
    annotate(Centerline(
        (FX(cx), FZ(_bb.min.Z) - 5, 0),
        (FX(cx), FZ(_bb.max.Z) + 5, 0),
    ), "centerline_front")
    annotate(Centerline(
        (SX(cy), SZ(_bb.min.Z) - 5, 0),
        (SX(cy), SZ(_bb.max.Z) + 5, 0),
    ), "centerline_side")

    # Inner bore leaders — arrowhead on bore edge, elbow to the left
    left_edge = FX(_bb.min.X)
    elbow_x   = left_edge - 10          # ~10 mm gap to left of part outline
    for i, d in enumerate(z_diams[1:4]):
        tip_z = FZ(cz) + (i - 1) * 10  # stagger vertically
        annotate(Leader(
            tip=(FX(cx - d / 2), tip_z, 0),
            elbow=(elbow_x, tip_z, 0),
            label=f"ø{d:.1f}" if d != int(d) else f"ø{int(d)}",
            draft=draft,
        ), f"bore_z{i}")
```

**Stacked dimensions** (use `place_dims` — it handles offset stacking automatically):

```python
dims = place_dims([
    ((x0, y_base, 0), (x1, y_base, 0), "below", "14.8"),
    ((x0, y_base, 0), (x2, y_base, 0), "below",  "7.1"),
], draft)
for i, d in enumerate(dims):
    annotate(d, f"dim_name_{i}")
```

**Leaders** (diameter callouts, part labels):

```python
ldr = Leader(
    tip=(page_x, page_y, 0),
    elbow=(elbow_x, elbow_y, 0),
    label="ø4.0 BEARING",
    draft=draft,
)
annotate(ldr, "ldr_bearing_d")
```

**Title block** (always include — use real values for the part being drawn).
ISO 7200:2004 mandatory fields:

| ISO 7200 field | Parameter |
|----------------|-----------|
| Field 1 — Legal owner | `legal_owner=` |
| Field 2 — Document description | `part_name` (first positional) |
| Field 3 — Document identifier | `drawing_number` (second positional) |
| Field 4 — Revision indicator | `revision=` |

```python
tb = TitleBlock(
    "PART NAME",          # ISO 7200 field 2 — document title
    "DWG-NNN",            # ISO 7200 field 3 — document identifier
    drawing_scale=SCALE,  # syncs title block cell text with lint_drawing(drawing_scale=SCALE)
    material="CZ121 BRASS",
    general_tolerance="ISO 2768-f",
    designed_by="Your Name",
    revision="A",                 # ISO 7200 field 4 — revision indicator
    legal_owner="COMPANY NAME",   # ISO 7200 field 1 — legal owner
    width=TB_W,
    draft=draft,
).locate(Location((PAGE_W - TB_W - 10, 10, 0)))  # right-aligned: PAGE_W − block_width − margin
annotate(tb, "title_block")
```

Every annotation object **must** be passed to `annotate()` — otherwise lint and
export will not see it.

---

## Manual 5 — Lint gate (run before export)

Use the MCP tool — it reads annotations and view shapes directly from session state:

```
# show() each placed view compound under a stable name so the MCP tool can find them.
show(view_proj["front"][0], "front_placed")
show(view_proj["side"][0],  "side_placed")
show(view_proj["plan"][0],  "plan_placed")
show(iso,                   "iso_placed")

mcp__build123d-mcp__lint_drawing(
    drawing_scale=SCALE,
    view_shape_names=["front_placed", "side_placed", "plan_placed", "iso_placed"],
)
```

The `view_shape_names` list enables `view_annotation_overlap` and `view_overlap` checks.
Omit it if you only need label/overlap checks and don't need view-boundary detection.

```python
# Alternatively, call the helper directly in execute() for more control:
all_anns = list(dims) + [ldr1, ldr2, tb]
set_page(PAGE_W, PAGE_H, margin=10)
view_shapes = [placed for placed, _ in view_proj.values()] + [iso]
issues = lint_drawing(all_anns, drawing_scale=SCALE, view_shapes=view_shapes)
if issues:
    for iss in issues: print(f"  [{iss.severity}] {iss.code}: {iss.message}")
else:
    print("Lint: OK")
```

Do not export until lint is clean (or all issues are understood and accepted).

Common lint failures and fixes:
- `label_axis_swap` — dimension endpoints are swapped (X↔Y); check coord helper signs
- `label_mismatch` — label string doesn't match the geometric distance; recheck scale
- `page_bounds` — annotation is outside the 297×210 margin; adjust view position
- `view_annotation_overlap` — an annotation's bbox overlaps a view outline; move the dim
- `view_overlap` — two view outlines overlap each other; re-run `suggest_view_layout` at a smaller scale or larger page

---

## Manual 6 — Export SVG and DXF

```python
part_color = Color(0, 0, 0)
hid_color  = Color(0.5, 0.5, 0.5)
dim_color  = Color(0, 0.2, 0.7)

svg_exp = ExportSVG(margin=10)
svg_exp.add_layer("part",   line_color=part_color, line_weight=0.5)
svg_exp.add_layer("hidden", line_color=hid_color,  line_weight=0.25,
                  line_type=LineType.HIDDEN)
svg_exp.add_layer("dims",   line_color=dim_color,  fill_color=dim_color,
                  line_weight=0.05)
for placed, _ in view_proj.values():
    svg_exp.add_shape(placed, layer="part")
svg_exp.add_shape(iso, layer="part")
for _, placed_hid in view_proj.values():
    if placed_hid:
        svg_exp.add_shape(placed_hid, layer="hidden")
if iso_h:
    svg_exp.add_shape(iso_h, layer="hidden")
for ann in all_anns:
    svg_exp.add_shape(ann, layer="dims")
svg_exp.write(str(output_dir / "part_name.svg"))

# Mandatory: fix the SVG viewBox so the full ISO page is preserved, not cropped
# to the content bounding box (build123d ExportSVG default).
from build123d_drafting import fix_svg_page_size
fix_svg_page_size(str(output_dir / "part_name.svg"), PAGE_W, PAGE_H)
```

Then export DXF (same layer structure; omit `line_color`, `fill_color`, and `line_type` args):

```python
from build123d import ExportDXF

dxf_exp = ExportDXF()
dxf_exp.add_layer("part",   line_weight=0.5)
dxf_exp.add_layer("hidden", line_weight=0.25)
dxf_exp.add_layer("dims",   line_weight=0.05)
for placed, _ in view_proj.values():
    dxf_exp.add_shape(placed, layer="part")
dxf_exp.add_shape(iso, layer="part")
for _, placed_hid in view_proj.values():
    if placed_hid:
        dxf_exp.add_shape(placed_hid, layer="hidden")
if iso_h:
    dxf_exp.add_shape(iso_h, layer="hidden")
for ann in all_anns:
    dxf_exp.add_shape(ann, layer="dims")
dxf_exp.write(str(output_dir / "part_name.dxf"))
```

---

## Manual 7 — Verify the SVG with the MCP server

```
mcp__build123d-mcp__render_drawing(svg_path='drawings/part_name.svg', save_to='/tmp/dwg.png')
```

Review the rendered PNG before moving on.

---

## Manual 8 — Combine into a PDF (optional)

To assemble multiple drawing SVGs into a single multi-page PDF, rasterise each
SVG at 200 DPI using `resvg-py` and combine pages with `fpdf2`.

`PAGE_W` and `PAGE_H` are the values chosen in Manual 2 (e.g. 297/210 for A4 or
420/297 for A3 landscape). Use them throughout so the PDF matches the drawing sheet.

```python
import resvg_py
from fpdf import FPDF

# PAGE_W, PAGE_H set in Manual 2
fmt = "A4" if PAGE_W < 400 else "A3"

png_bytes = resvg_py.svg_to_bytes(svg_path=str(svg_path), dpi=200)

pdf = FPDF(orientation="L", unit="mm", format=fmt)
pdf.add_page()
pdf.image(tmp_png_path, x=0, y=0, w=PAGE_W, h=PAGE_H)
pdf.output("drawings/output.pdf")
```

build123d `ExportSVG` writes Y-up coordinates; the `viewBox` origin encodes
where content sits on the sheet. To recover the correct Y position for PDF
(Y-down, top-left origin):

```python
vb_y = float(svg_root.get("viewBox").split()[1])   # negative in Y-up drawing coords
assert vb_y <= 0, f"Unexpected positive viewBox Y: {vb_y} — check ExportSVG output"
pdf_y = PAGE_H - abs(vb_y)
```

If the assert fires, the SVG was generated with a different coordinate convention;
inspect the `viewBox` before placing the image.

---

## Manual 9 — Save a standalone regeneration script (default)

The same default as Step 4 applies: unless the user opts out, save a clean,
committable `scripts/drawings/<part>.py` that regenerates the drawing in one run.
For the manual pipeline there is no `generate_script()` shortcut, so assemble the
script by hand — reconstruct the part (import its source module, or load the STEP)
followed by the projection / annotation / export steps above. Keep it tidy and
reproducible, not a paste of the exploratory session.

---

## Layout rules of thumb

- Leave ≥ 12 mm between any two view outlines.
- Dimension lines below/left of the view they measure; leader elbows clear the geometry.
- Isometric goes in the corner least occupied by orthographic views (usually bottom-left or far right).
- Title block: bottom-right, 150–170 mm wide, Y anchor ≈ 11 mm from bottom.
- Don't put dimensions on the isometric — it is a pictorial only.

---

## Axis sign quick-reference

`view_axes` output drives all coordinate helpers. Common cases:

| Camera (VIEWS key) | Up | world_X | world_Y | world_Z |
|--------------------|----|---------|---------|---------|
| -Y (front) | +Z | page_X (+1) | — | page_Y (+1) |
| +X (side)  | +Z | — | page_X (+1) | page_Y (+1) |
| +Z (plan)  | +Y | page_X (+1) | page_Y (+1) | — |
| -X (alt)   | +Z | — | page_X (-1) | page_Y (+1) |

Signs flip when the camera is on the negative axis — always verify with
`view_axes` rather than assuming.
