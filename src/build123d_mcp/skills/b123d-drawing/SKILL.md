# Create Engineering Drawing from build123d Geometry

Use this skill when asked to create or fix an engineering drawing for a
build123d component. The drawings live in a project `scripts/drawings/`
directory and export to a `drawings/` output directory.

---

## Step 0 — Understand the part first

Before writing any drawing code, use the MCP server to build and inspect the geometry:

```
mcp__build123d-mcp__execute  — build the part in the session
mcp__build123d-mcp__measure  — confirm volume, bbox, face count
mcp__build123d-mcp__render_view (save_to='/tmp/preview.png') — visual sanity check
```

Note the bounding-box extents. These drive layout decisions below.

---

## Step 1 — Choose views (third-angle projection)

Standard four-view layout. Page size is chosen in Step 2 based on part extents
(A4 landscape 297 × 210 mm for most parts; A3 landscape 420 × 297 mm for large ones).

| View | Camera position (scaled space) | Up vector | Role |
|------|-------------------------------|-----------|------|
| Front (along -Y) | `(cxs, cys − DIST, czs)` | `(0, 0, 1)` | primary dims |
| Side (along +X)  | `(cxs + DIST, cys, czs)` | `(0, 0, 1)` | depth/bore |
| Plan (along +Z)  | `(cxs, cys, czs + DIST)` | `(0, 1, 0)` | footprint |
| Isometric        | `(cxs+ID, cys+ID, czs+ID)`         | `(0, 0, 1)` | pictorial, no dims |

where `cxs = cx * SCALE`, `cys = cy * SCALE`, `czs = cz * SCALE`,
`DIST = bbox_max * SCALE + 100` (orthographic cameras always outside the scaled bbox),
and `ID = DIST / _m.sqrt(3)` (iso camera at the same distance along the equal-axis diagonal).

**Critical:** view direction = `look_at − camera`. For a pure orthographic projection the
camera's off-axis coordinates must equal the scaled centroid — using `(0, -DIST, 0)` instead
of `(cxs, cys - DIST, czs)` introduces a silent tilt whenever the centroid is off-axis.

The iso camera uses equal `+ID` offsets on all three axes for a standard equal-axis view.
Negate one axis (e.g. `(cxs-ID, cys+ID, czs+ID)`) to flip the pictorial orientation when
a key feature is otherwise hidden.

Axis mapping verification and sheet-position layout are both done in Step 2 once SCALE
is known — see the `view_axes` and `suggest_view_layout` calls there.

---

## Step 2 — Choose page size and scale, then project

Pick `SCALE` and page dimensions so the scaled longest dimension fits comfortably
within the usable page width (≈ page width − 20 mm margins). Use the bounding-box
`x_size / y_size / z_size` values from `measure()` in Step 0.

```python
# Extract geometry from part — drives all layout decisions below.
_bb   = part.bounding_box()
x_size = _bb.max.X - _bb.min.X
y_size = _bb.max.Y - _bb.min.Y
z_size = _bb.max.Z - _bb.min.Z
cx = (_bb.min.X + _bb.max.X) / 2
cy = (_bb.min.Y + _bb.max.Y) / 2
cz = (_bb.min.Z + _bb.max.Z) / 2
bbox_max = max(x_size, y_size, z_size)

# Rule of thumb: scaled bbox_max should be ≤ 60 % of usable page length
if   bbox_max * 2.0 <= 170:
    SCALE, PAGE_W, PAGE_H = 2.0, 297.0, 210.0   # A4 2:1  — small parts
elif bbox_max * 1.0 <= 170:
    SCALE, PAGE_W, PAGE_H = 1.0, 297.0, 210.0   # A4 1:1
elif bbox_max * 1.0 <= 260:
    SCALE, PAGE_W, PAGE_H = 1.0, 420.0, 297.0   # A3 1:1  — larger parts
else:
    SCALE, PAGE_W, PAGE_H = 0.5, 420.0, 297.0   # A3 1:2  — very large parts
```

Compute sheet positions using the `suggest_view_layout` MCP tool — it accounts for the
title block footprint and warns when views collide with it or with each other:

```
TB_W = 150.0  # title block width (mm) — must match the TitleBlock width= arg in Step 4

mcp__build123d-mcp__suggest_view_layout(
    object_name="part",        # name passed to show() in Step 0
    page_w=PAGE_W, page_h=PAGE_H, scale=SCALE,
    title_block_w=TB_W,
    title_block_h=24,          # ISO 7200 title block height with revision + legal_owner rows
)
```

Check `result["warnings"]` — if any view overlaps the title block or another view, the
tool says so and may suggest a smaller scale or larger page. Address warnings before
continuing. Then extract the positions:

```python
# Extract positions from suggest_view_layout result.
# Re-run suggest_view_layout above if the part geometry changes before Step 2.
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
ID   = DIST / _m.sqrt(3)               # iso offset — same distance along equal-axis diagonal

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
coordinate helpers in Step 3.

---

## Step 3 — Coordinate helpers

Write one helper per view so annotation coords are derived from world geometry,
not hardcoded page numbers. Pattern (example for a view where world_Z → page_X):

```python
# Side view: world_Z → page_X (+1), world_Y → page_Y (+1), look_at Z=z_center
def SX(z): return SV_X + z * SCALE - z_center * SCALE
def SY(y): return SV_Y + y * SCALE
```

Verify a known extent (e.g. top of part) maps to a sensible page Y before using.

---

## Step 4 — Annotate with build123d_drafting

```python
from build123d_drafting import (
    Dimension, Leader, TitleBlock,
    annotate, draft_preset, lint_drawing, place_dims, set_page,
)

draft = draft_preset(font_size=2.5, decimal_precision=1)
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

**Title block** (always include — use real values for the part being drawn):

ISO 7200:2004 mandatory fields and their parameter mapping:

| ISO 7200 field | Parameter | Notes |
|----------------|-----------|-------|
| Field 1 — Legal owner | `legal_owner=` | Requires build123d-drafting-helpers ≥ 0.3.2. On older installs, prefix `part_name` instead: `"ACME Corp — BRACKET"` |
| Field 2 — Document description | `part_name` | Always present |
| Field 3 — Document identifier | `drawing_number` | Always present |
| Field 4 — Revision indicator | `revision=` | Requires ≥ 0.3.2. On older installs, append to `drawing_number`: `"DWG-001 Rev A"` |

```python
TB_W = 150.0
tb = TitleBlock(
    "PART NAME",          # ISO 7200 field 2 — document title
    "DWG-NNN",            # ISO 7200 field 3 — document identifier
    drawing_scale=SCALE,  # syncs title block cell text with lint_drawing(drawing_scale=SCALE)
    material="CZ121 BRASS",
    general_tolerance="ISO 2768-f",
    designed_by="Your Name",
    revision="A",         # ISO 7200 field 4 — revision indicator (≥ 0.3.2)
    legal_owner="COMPANY NAME",  # ISO 7200 field 1 — legal owner (≥ 0.3.2)
    width=TB_W,
    draft=draft,
).locate(Location((PAGE_W - TB_W - 10, 10, 0)))  # right-aligned: PAGE_W − block_width − margin
annotate(tb, "title_block")
```

If `TitleBlock()` raises `TypeError: unexpected keyword argument 'revision'`, the installed
version of build123d-drafting-helpers is older than 0.3.2. Only the first two positional
args differ — pack owner and revision into them; everything else stays the same:
```python
# Older API workaround — only these two args change:
tb = TitleBlock(
    "COMPANY — PART NAME",  # legal_owner prefix + part_name (replaces legal_owner= param)
    "DWG-NNN Rev A",        # drawing_number + revision indicator (replaces revision= param)
    # all other args identical to the primary block above
    drawing_scale=SCALE, material="CZ121 BRASS", general_tolerance="ISO 2768-f",
    designed_by="<DESIGNER>", date="<YYYY-MM-DD>", width=TB_W, draft=draft,
).locate(Location((PAGE_W - TB_W - 10, 10, 0)))
annotate(tb, "title_block")
```

Every annotation object **must** be passed to `annotate()` — otherwise lint and
export will not see it.

---

## Step 5 — Lint gate (run before export)

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

The `view_shape_names` list enables `view_annotation_overlap` and `view_overlap` checks
(requires build123d-drafting-helpers ≥ 0.3.1). Omit it if you only need label/overlap
checks and don't need view-boundary detection.

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

## Step 6 — Export SVG and DXF

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

## Step 7 — Verify the SVG with the MCP server

```
mcp__build123d-mcp__render_drawing(svg_path='drawings/part_name.svg', save_to='/tmp/dwg.png')
```

Send with `[SEND: /tmp/dwg.png]` for user review before moving on.

---

## Step 8 — Combine into a PDF (optional)

To assemble multiple drawing SVGs into a single multi-page PDF, rasterise each
SVG at 200 DPI using `resvg-py` and combine pages with `fpdf2`.

`PAGE_W` and `PAGE_H` are the values chosen in Step 2 (e.g. 297/210 for A4 or
420/297 for A3 landscape). Use them throughout so the PDF matches the drawing sheet.

```python
import resvg_py
from fpdf import FPDF

# PAGE_W, PAGE_H set in Step 2
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
