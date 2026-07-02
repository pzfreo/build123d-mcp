# build123d-mcp — LLM Reference

build123d-mcp is an MCP server that wraps the [build123d](https://github.com/gumyr/build123d) Python CAD library. It gives you tools to build 3D geometry incrementally, render views, measure dimensions, export files, snapshot state, inspect session state, and verify dependencies.

## Sandbox — what your `execute()` code can and can't do

This server enforces a Python-level sandbox on every `execute()` call. Three layers run before your code:

1. **Import allowlist** — only `build123d`, `bd_warehouse`, `math`, `numpy`, `inspect`, the safe stdlib subset (collections, itertools, functools, copy, typing, dataclasses, enum, re, json, base64, hashlib, …), and curated geometric OCP submodules are importable. Filesystem (`os`, `pathlib`, `shutil`), networking (`socket`, `urllib`, `requests`), and shell access (`subprocess`) are blocked. The full allowlist is in the error message of any blocked import.
2. **Restricted builtins** — `open`, `eval`, `exec`, `compile`, `breakpoint`, `input`, and the introspection helpers `getattr`/`vars` are removed (string arguments to them would bypass the dunder-attribute block). `hasattr` and `dir()` are **allowed**: `hasattr` returns only a bool so it cannot extract `__class__`/`__subclasses__`, and `dir()` only lists names already in scope. (Run with `--no-sandbox` in a trusted environment to lift all of these.)
3. **Execution timeout** — wall-clock limit (default 120 s).

If a script truly needs an extra package (e.g. `scipy.optimize` to size a parametric part), the server operator can extend the allowlist via `--allow-imports scipy,pandas` — the LLM doesn't control this, but a blocked-import error message names the attempted module so the user can decide whether to add it.

Practical implications for your code:

- Never `import os`, `import pathlib`, `import subprocess`, `import socket`, `import urllib`, `import requests` — all blocked.
- Never call `eval()`, `exec()`, `open()` — blocked at the builtin level.
- Never write `obj.__class__.__bases__[0]` or similar dunder-attribute access — AST-blocked.
- Use `inspect.signature(ClassName)` and `inspect.getdoc()` for API discovery; both are allowed.
- File I/O happens through MCP tools (`export`, `import_cad_file`, `render_view(save_to=)`) — never directly.

If you see "Import of 'X' is not allowed" or "Call to 'Y' is not allowed", the user code hit a sandbox layer; don't try to bypass it, just use the MCP tools or change approach.

---

## Key concept: persistent session

All tool calls share a single Python namespace. Variables and shapes you create with `execute` persist across subsequent calls. Use this to build geometry step by step, checking your work after each step.

### Multi-object sessions

Use `show(shape, name)` inside `execute` to register named objects. Calling `show()` also sets `current_shape`, so subsequent `measure()`/`export()`/`render_view()` calls work immediately without an explicit `result` assignment. All tools that accept `object_name` operate on a named object instead of the implicit `current_shape`. This is essential for assemblies where you need to inspect, measure, or export individual parts.

```python
frame = Box(60, 40, 8)
show(frame, "frame")

axle = Cylinder(5, 50)
show(axle, "axle")
```

---

## Tools

### `version`
Return installed versions of the server, key dependencies, and companion packages importable inside `execute()` (`bd_warehouse` for threads/fasteners/gears/bearings, `augura` for printability analysis).

**No inputs.**

**Returns:** one `name: version` line per package

---

### `execute`
Run build123d Python code in the persistent session.

**Input:** `code` (string) — valid Python source

**Returns:** captured stdout/stderr, or `"OK"` if silent, or an error message on exception.

The server auto-detects the current shape. Prefer assigning your final shape to `result`, or use `show(shape, name)` for named objects:
```python
result = Box(10, 20, 30)
```
```python
with BuildPart() as bp:
    Box(10, 20, 30)
    Cylinder(radius=3, height=30, mode=Mode.SUBTRACT)
result = bp.part
```

**On error:** the previous `current_shape` is preserved; the namespace is not wiped. Failed code cannot silently advance session state.

---

### `session_state`
Return a structured JSON snapshot of the full session.

**No inputs.**

**Returns:** JSON with:
- `current_shape` — geometry metrics (volume, faces, edges, vertices, bbox) or `null`
- `objects` — dict of name → metrics for all shapes registered via `show()`
- `snapshots` — list of saved snapshot names
- `variables` — summary of non-shape Python namespace variables (type + value/length for scalars and collections)

Use this to orient at the start of a session, after a restore, or after a multi-step build to confirm what geometry and variables are active. Replaces the removed `list_objects` tool.

---

### `health_check`
Verify that render and export dependencies are working end-to-end.

**No inputs.**

**Returns:** JSON with per-capability `ok`/`error` status:
- `render_png` — VTK raster render
- `render_svg` — build123d HLR line projection
- `export_step` — STEP file export
- `export_stl` — STL mesh export
- `ok` — `true` only if all capabilities pass

Run at session start if you suspect a missing dependency (headless display, missing VTK wheels, etc.).

---

### `render_view`
Render one or more shapes and return a file path to the rendered image.

**Inputs:**
- `direction` (string, default `"iso"`) — `top`, `front`, `side`, `iso`
- `objects` (string, default `""`) — comma-separated names from `show()` to render; empty = all registered objects, or `current_shape` if none registered. Optionally suffix a name with `:color` to override the auto-assigned colour, e.g. `"frame:blue,axle:red"`
- `quality` (string, default `"standard"`) — `standard` or `high`; high uses finer tessellation to eliminate artefacts on curved surfaces
- `clip_plane` (string, default `""`) — `x`, `y`, or `z`; clips each mesh at its bounding-box midpoint to expose internal geometry (bores, wall thickness)
- `clip_at` (float, optional) — absolute world coordinate for the clip plane instead of the midpoint
- `azimuth` / `elevation` (float, default `0.0`) — camera rotation in degrees applied after the direction preset
- `format` (string, default `"png"`) — `png`, `svg`, `dxf`, or `both` (= png + svg). DXF returns the projected polylines as parseable 2D CAD geometry. **Auto-detects 2D inputs**: when the named object is a Sketch or Compound with no solids (a dimensioned drawing built via `build123d.drafting`), `format="png"` rasterises it via ezdxf+matplotlib so the LLM can review the drawing the same way it reviews 3D parts. `label_objects` works for 2D too — adds an MTEXT label at each named object's centroid.
- `save_to` (string, default `""`) — optional path to also write the file(s) to disk
- `label_objects` (bool, default `False`) — label each named object from `show()` at its centroid in the PNG. Useful for assemblies where the LLM needs to confirm which shape is which by name.
- `highlights` (list of dict, default `None`) — label specific faces, edges, or vertices in the PNG. Each entry is `{"object": "name", "type": "face"|"edge"|"vertex", "index": int, "label": "text"}` where `index` matches the position in `shape.faces()` / `.edges()` / `.vertices()`. The referenced object must already be registered with `show()` and included in the rendered set; an unregistered object raises an error naming what to register. Use this to verify "edge 5 is the one I want to fillet" before committing to the operation. Labels are PNG-only — SVG output emits a `label_warnings` notice.

**Returns:** `[SEND: /tmp/build123d_xxx.png]` text marker; the file is delivered directly to the client.

Each named object is rendered in a distinct colour. Call this after each significant change to verify geometry visually.

---

### `measure`
Return a complete geometric summary of a shape in a single call.

**Input:**
- `object_name` (string, default `""`) — named object from `show()`; empty = `current_shape`
- `density` (float, default `0`) — material density in g/cm³; adds `density_g_cm3` + `mass_g` and scales `inertia` to true mass moments (g·mm²)
- `material` (string, default `""`) — density preset: `steel`, `stainless`, `aluminum`/`6061`, `brass`, `copper`, `titanium`, `abs`, `pla`, `petg`, `nylon`; mutually exclusive with `density`

**Returns:** JSON with:
- `volume` (mm³), `area` (mm²); plus `density_g_cm3` and `mass_g` when a density was given
- `topology` — `faces`, `edges`, `vertices`; fastest way to confirm a boolean succeeded (a failed cut leaves counts unchanged)
- `bounding_box` — per-axis min/max, size, and `center`
- `center_of_mass` — volumetric centroid
- `inertia` — 6-component tensor: `Ixx/Iyy/Izz/Ixy/Ixz/Iyz`; `inertia_units` states `g·mm²` (density given) or `mm⁵` (volume inertia)
- `face_inventory` — faces classified as `Plane/Cylinder/Cone/Sphere/Torus/BSpline` with area and type-specific params (e.g. cylinder diameter and axis). Identical faces collapse into one entry with a `count` (4 identical drilled holes → one Cylinder entry, `count: 4`), and non-analytic sliver faces (thread fades) fold into a single `slivers_folded` summary entry

Prefer `measure()` over `render_view()` for verifying geometry — numbers are unambiguous.

---

### `validate`
Check whether a shape would pass a CAD validity gate before exporting it. The gate mirrors what CAD scorers and downstream tools require — a well-formed (BRepCheck), watertight, manifold solid with non-zero volume.

**Input:** `object_name` (string, default `""`) — named object from `show()`; empty = `current_shape`

**Returns:** a `PASS`/`FAIL` line plus JSON: `passes_gate`, `n_solids`, `volume`, `watertight_manifold`, `open_edges`, `nonmanifold_edges`, `mesh_nonmanifold_edges`, `brep_valid`, `reasons` (fatal failure causes), and `warnings` (non-fatal advisories — e.g. multiple disjoint solid bodies, which pass the gate but hurt the topology score on a single-part task). Watertightness/manifoldness is judged by the edge→face map (not build123d's `is_manifold`, which false-negates on imported solids) plus a welded tessellated-mesh check that catches self-touching / coincident faces — valid B-reps that a CAD scorer still rejects.

A `FAIL` means a STEP/STL export would be rejected outright (a CAD scorer like CADGenBench scores it zero) — typically a leftover 2D sketch or open shell as the current shape, an un-fused compound, or a degenerate boolean result. Run this immediately before `export()` on any part you intend to submit or hand off; `export()` re-runs the gate and warns on a 3D export that would fail.

---

### `design_audit`
Audit the session program as a **design, not just a shape**: surface its named numeric parameters (Θ) and test how robust each is to editing. Where `validate` proves you built a valid *shape* (the executed geometry `g`), this probes whether you built an editable *design* (the parameterisation `z` that produced it) — the Arko-T §6 distinction.

**Inputs:**
- `epsilon` (float, default `0.1`) — relative nudge per parameter, `0 < epsilon < 1` (0.1 = ±10%)
- `max_params` (int, default `8`) — cap on the number of parameters audited

**How it works:** parses the assembled program (see `script`) for top-level numeric assignments (`plate_thickness = 5.0`), then for each parameter rebuilds the whole program with that value nudged ±epsilon and runs the `validate` gate on each result. The rebuild+gate loop runs **out of process, hard-bounded by the op budget** (a rebuild or the gate's tessellation can hit an un-interruptible native OCC call that SIGALRM can't stop), persisting results incrementally so a kill still salvages a partial report; the live session is never mutated. On hosts that block child processes it degrades to an in-process run.

**Returns:** JSON with `parameters` (name/value/type), `baseline` (the unperturbed rebuild's gate verdict), `inline_literal_count`, `audit` (per parameter: `perturbations` with `delta_pct`, `new_value`, `cause` on failure, `volume_delta_pct`; plus a per-parameter **`verdict`** and one-line **`reason`**), `summary` (counts per verdict + truncation), and a `note`. Each parameter is classified — **not** every failed rebuild is "brittle":

- **`brittle`** — certain fragility: a small change fails the validity gate or the solid can't form. *Verify first.*
- **`coupling`** — a dependent feature (fillet/chamfer/shell/offset) failed when this parameter alone changed. **Ambiguous** — either parameter coupling (a fixed feature no longer fits) *or* a genuinely fragile feature dimension if this parameter drives that feature; disambiguate with a co-edit.
- **`not_a_design_parameter`** — perturbing it breaks a geometry selection ("found 0"): likely a measured selector/anchor constant — **but** could be real degeneracy.
- **`inconclusive`** — not decidable: a perturbed rebuild timed out (raise `--exec-timeout`), the validity gate errored, or the parameter is reassigned at top level.
- **`robust`** — survives ±ε.

`summary` reports counts per verdict plus **`needs_review` = brittle + coupling + not_a_design_parameter** — because coupling / not_a_design_parameter are ambiguous, **read `needs_review`, not `brittle` alone**; `brittle == 0` does not mean "all good". Each parameter carries a one-line `reason`.

If no named parameters are found, the program uses inline magic constants and the note advises hoisting them into a parameter block. Bounded by a wall-clock budget and `max_params` — returns a partial report rather than risking a worker timeout.

---

### `verify_spec`
Check the built solid against a **declared design-intent spec** — the "did I build what was requested?" gate. Where `validate` proves the solid is *valid* and `design_audit` probes its *parameters*, `verify_spec` proves requested-vs-built: it checks each requested feature/constraint against the actual geometry and returns an **evidence-tiered conformance report** (implements Arko-T's feature-realization idea, rec #3).

**Inputs:**
- `spec` (string) — the design-intent spec as inline JSON, **or**
- `spec_path` (string) — path to a `.json` spec file (via the output-path policy)
- `object_name` (string, default `""`) — named object from `show()`; empty = current shape

**Spec keys:** `envelope_mm {x/y/z: [lo,hi]}` (bbox size in range), `solid {count, valid}`, `volume_mm3 {min,max}`, `parameters: [{name, min, max}]` (top-level numeric assignment in range), and `features: [...]`:
- `{kind:"hole", count, diameter_mm, depth_mm, through:bool, counterbore:{diameter_mm, depth_mm}|true|false, spotface:{...}}` — any subset of attributes; all frame-independent (absolute position is not matched). `counterbore`/`spotface`: `true` requires one present, `false` requires it absent, an object matches its dims. Note a `depth_mm` on `counterbore`/`spotface` is matched against the **recognizer-measured** depth, which can differ from a drawing callout — match on `diameter_mm` when unsure.
- `{kind:"hole_pattern", pattern:"bolt_circle"|"linear_array", holes, bcd_mm (bolt_circle) | pitch_mm (linear_array), diameter_mm}`
- `{kind:"boss", diameter_mm, height_mm}`
- `{kind:"countersink", count, major_diameter_mm, drill_diameter_mm, included_angle_deg, depth_mm}` — any subset; conical screw-head recesses (see `find_countersinks`). A shallow lead-in/deburr chamfer also registers as a small countersink.

`min_wall_mm` and `targets: [{name, verifiable:false}]` are reported UNVERIFIED (deferred / out of scope), not silently dropped. Feature kinds beyond hole/hole_pattern/boss (pocket, fillet, chamfer, rib, …) need new recognizers and currently read UNVERIFIED.

**Returns:** JSON `{conformance: [{requirement, status: PASS|FAIL|UNVERIFIED, tier, actual/found/hint}], summary: {pass, fail, unverified, checked, conforms}, note}`. Each line carries its **evidence tier** — `measured` (kernel query), `structural` (validity gate), `recognised` (heuristic feature recognition), `unverified` (no checker / deferred / declared unverifiable). `conforms` = **no FAILs and ≥1 requirement actually checked** (`summary.checked`) — a spec that verifies nothing (all keys unrecognised/deferred/unverifiable) reports `conforms:false` with a warning, never a vacuous true; UNVERIFIED requirements are never counted as met. Dimensions match within `max(0.1 mm, 1%)`; counts exact; an unrecognised feature `kind` is UNVERIFIED, never a false FAIL. A malformed spec returns a clean error naming the bad field. Not a certification. A spec is a **reusable contract** — re-run after any edit as a regression/acceptance gate to catch collateral breakage. See `docs/design-conformance-proposal.md`.

---

### `clearance`
Spatial relationship between two named shapes — distance, containment, and overlap in one call.

**Inputs:** `object_a`, `object_b` (string) — names from `show()`

**Returns:** JSON with:
- `clearance` (mm) — interpretation depends on `status` (see below)
- `status` — `apart` | `touching` | `containing` | `interpenetrating`
- `containment` — `a_in_b` | `b_in_a` | `neither`
- `intersection_volume` (mm³) — overlap between the two shapes
- `a_volume_outside_b`, `b_volume_outside_a` (mm³) — how much of each shape escapes the other

Status semantics:
- **apart**: surfaces don't touch; `clearance` = gap distance
- **touching**: surfaces meet exactly; `clearance` = 0, `intersection_volume` = 0
- **containing**: one shape fully inside the other; `clearance` = wall thickness in the worst direction (smallest gap from the inner shape's surface to the outer hull). Use this to verify a pocket/hole/bore fits inside a plate with adequate wall.
- **interpenetrating**: shapes overlap and neither is fully inside the other — the wall-piercing case. `intersection_volume` shows how much they overlap; `a_volume_outside_b` shows how much of A pokes outside B.

Examples:
- Verifying a hole has 1 mm wall thickness: `clearance(hole, plate)` → `status=containing, clearance≥1.0`
- Catching a hole that pierces the back of a plate: `clearance(hole, plate)` → `status=interpenetrating, a_volume_outside_b>0`
- Checking two assembly parts don't collide: `clearance(part_a, part_b)` → `status=apart` and `clearance > required_gap`

---

### `cross_sections`
Compute cross-sectional areas at evenly spaced planes along an axis.

**Inputs:**
- `object_name` (string, default `""`) — named object from `show()`; empty = `current_shape`
- `axis` (string, default `"Z"`) — `X`, `Y`, or `Z`
- `num_slices` (int, default `10`) — number of planes (minimum 2)

**Returns:** JSON array of `{position, area}` pairs.

Useful for detecting internal voids, wall-thickness variation, or verifying a shape's cross-section profile against a reference.

---

### `export`
Export a shape to a file.

**Inputs:**
- `filename` (string) — target path; extension auto-appended if missing
- `format` (string, default `"step"`) — `"step"`, `"stl"`, `"dxf"`, `"svg"`, or comma-separated like `"step,stl"` or `"dxf,svg"`. 3D solids → step/stl; 2D Sketches/dimensioned drawings → dxf/svg. Mixing dimensions across that boundary errors with a clear pointer at the right tool.
- `object_name` (string, default `""`) — named object from `show()`; `"*"` to export all named objects as a combined assembly; empty = `current_shape`

**Returns:** path(s) of exported file(s), plus a sanity line echoing the exported shape's volume/bbox/face count (bbox/edge count for 2D) — confirms the right, non-degenerate object was written

STEP preserves exact geometry for downstream CAD tools. STL is for mesh-based workflows (3D printing, slicers, GitHub preview).

STEP exports carry session names as labels so downstream CAD tools see structured assemblies with named bodies:
- `export("part.step", object_name="bracket")` — body labelled `bracket`
- `export("asm.step", object_name="*")` — Compound labelled `assembly` containing each named child (`bracket`, `pin`, etc.)

Labels are set on copies — your session shapes are not mutated.

---

### `interference`
Check whether two named shapes intersect.

**Inputs:** `object_a`, `object_b` (string) — names from `show()`

**Returns:** JSON with `interferes` (bool), `volume` (mm³ of overlap), and `bounds` of the interference region.

---

### `shape_compare`
Compare two named shapes by geometry metrics **plus a localized surface-deviation diff**.

**Inputs:** `object_a`, `object_b` (string) — names from `show()`

**Returns:** JSON with:
- volume delta, bbox delta, topology delta (faces/edges/vertices), and centre offset (as before);
- `max_deviation` — the largest *real* surface change (noise-floored against an independent shared-deflection tessellation, so a re-export no-op reads ~0);
- `changed` / `regions` — the localized region(s) that moved: centroid and bbox, plus exact `added_volume` / `removed_volume`;
- `magnitude_method` — how to read `max_deviation`: `exact_boolean` (exact surface displacement *and* exact volumes), `exact_volume_mesh_displacement` (exact added/removed volume, mesh-estimated displacement — e.g. a cut/flush-fill), or `mesh_estimate` (boolean skipped or failed);
- `unchanged_elsewhere` (bool) and `warnings`.

The exact B-rep boolean is clipped to the located region and runs in a hard-bounded subprocess (in-process fallback where child processes are blocked); on a large/spread edit it gracefully falls back to the flagged mesh estimate rather than overrunning the op budget.

For editing this is **model↔input verification, not a score**: confirm the changed region(s) and the add/remove volumes match the request. Note a *tangential* move (sliding a hole) or a sub-resolution edit on a very large part produces no detected region — `unchanged_elsewhere` then means "no change above the detection floor", not a guarantee; cross-check the volume/bbox/center deltas and `find_holes`.

Useful for verifying a procedural build matches a reference, or quantifying how a modification changed the geometry.

---

### `import_cad_file`
Import a STEP or STL file as a named object in the session.

**Inputs:**
- `path` (string) — absolute or relative path to the file (`.step`, `.stp`, or `.stl`)
- `name` (string, default `""`) — name to register under; defaults to the filename stem

**Returns:** volume, topology, and bounding box of the imported shape. The shape becomes both the named object and `current_shape`.

Use with `shape_compare()` to verify a procedural build against a reference.

---

### `search_library`
Search the part library by keyword.

**Input:** `query` (string, default `""`) — keywords matched against name, description, tags, category; empty returns all parts

**Returns:** name, category, description, tags, and full parameter specs (types, defaults, descriptions)

*Requires server started with `--library PATH` or `BUILD123D_PART_LIBRARY` env var.*

---

### `load_part`
Load a named part from the library into the session.

**Inputs:**
- `name` (string) — part name from `search_library()`
- `params` (string, default `""`) — optional JSON object of parameter overrides, e.g. `'{"od": 8.0, "length": 20.0}'`; unspecified params use their defaults

**Returns:** confirmation; the part is registered as a named object and becomes `current_shape`.

*Requires server started with `--library PATH` or `BUILD123D_PART_LIBRARY` env var.*

---

### `last_error`
Return details of the last failed `execute()` call.

**No inputs.**

**Returns:** JSON with exception type, message, and (for runtime/syntax errors) line number and a 5-line excerpt around the failing line. Returns `{"error": null}` if the last `execute()` succeeded or none has failed yet.

Call immediately after an `execute()` error to get the exact failing line without re-reading the submitted code.

---

### `repair_hints`
Get targeted fix suggestions for an `execute()` error message.

**Input:** `error_text` (string) — the full error string from `execute()` or `last_error()`

**Returns:** matched hints from the repair library covering common build123d mistakes (wrong Location syntax, missing `.part`, CadQuery idioms, blocked imports, degenerate booleans, fillet edge selection, etc.).

Note: `execute()` already appends relevant hints inline on error — use `repair_hints()` for additional suggestions or when working with a stored error string.

---

### `workflow_hints`
Return guidance on using these tools effectively.

**No inputs.**

**Returns:** plain text guide covering orient-first, measure-before-render, boolean verification, shape naming, checkpointing, cross-sections, and part library usage.

Call at the start of a session or whenever unsure which tool to reach for next.

---

### `save_snapshot`
Save a named checkpoint of the current geometric state.

**Input:** `name` (string) — snapshot label

**Returns:** confirmation listing what was captured

**What is saved:** `current_shape` and the `show()` object registry.
**What is NOT saved:** the Python variable namespace. After a restore, any intermediate Python variables created after the snapshot are still in scope — but `current_shape` and all `show()` objects revert to the snapshot state.

---

### `restore_snapshot`
Restore geometric state from a previously saved snapshot.

**Input:** `name` (string) — snapshot label

**Returns:** confirmation listing restored geometry, or an error if the name does not exist.

---

### `diff_snapshot`
Compare two snapshots by geometry metrics.

**Inputs:**
- `snapshot_a` (string) — baseline snapshot name
- `snapshot_b` (string, default `""`) — comparison snapshot; defaults to current session state
- `format` (string, default `"text"`) — `"text"` for human-readable output, `"json"` for structured

**Returns:** volume delta, topology changes, and added/removed/changed objects.

JSON format returns `{"a": {"label": ..., "current_shape": ..., "objects": ...}, "b": {...}}`.

---

### `reset`
Clear the session back to empty state, including all snapshots.

**No inputs.**

**Returns:** `"Session reset."`

---

## Recommended workflow

1. `version` — confirm server version
2. `health_check` — verify dependencies (optional; run if first session or suspect issues)
3. Read `build123d://quickref` — get accurate API syntax before writing any `execute()` code
4. `reset` — start clean
5. `execute` — imports and initial geometry; use `show()` for named parts
6. `measure` — verify geometry numerically (check `volume` and `topology.faces` after every boolean). **Do not proceed to render_view until measure passes** — a failed boolean leaves counts unchanged.
6a. For assemblies: `clearance` — check mating parts are `touching` or `apart`, not `interpenetrating`, before rendering.
7. `session_state` — confirm active shapes after any complex step
8. `render_view` — visually verify **only after measure (and clearance for assemblies) confirm geometry is correct** (try `iso` first; use `quality="high"` for curved surfaces)
9. `save_snapshot` — checkpoint before complex or risky operations
10. `execute` — add features; if something breaks, `restore_snapshot`
11. `diff_snapshot` — confirm what changed (use `format="json"` for programmatic checks)
12. Repeat 6–11 until satisfied
13. `export` — write STEP + STL in one call with `format="step,stl"`

For assemblies of two or more parts with a mechanical relationship (mounted, hinged, sliding), use build123d Joints (`RigidJoint`/`RevoluteJoint`/`LinearJoint`/`CylindricalJoint`/`BallJoint`) rather than positioning parts with `.move()`. The relationship survives later changes to the parent. See `build123d://quickref` for examples.

### Proposals — evaluating "what if?" without touching the canonical model

When asked to evaluate a possible modification ("would adding a hole here weaken the wall?", "what if we widened this slot to 6mm?"), use snapshots as a scratch layer. Don't redraw the geometry in matplotlib to evaluate it — that's lossy and disagrees with the model.

```
save_snapshot("before")            # cheap; geometry-only
execute("plate = plate - Cylinder(2, 5).move(Location((10, 0, 0)))")
clearance("hole_proxy", "plate")   # check wall thickness, piercing, etc.
cross_sections(plate, axis="Z")    # see internal voids at each Z
render_view(format="dxf")          # geometry as parseable polylines, not a redraw
restore_snapshot("before")          # canonical model untouched
```

The 3D mutation + 3D analysis loop is faster, more accurate, and uses the same primitives as the rest of the workflow.

---

---

## MCP Resources

Read-only resources that LLM clients can fetch without spending a tool-call round-trip:

| URI | MIME type | Contents |
|-----|-----------|----------|
| `build123d://quickref` | `text/plain` | build123d API quick reference: primitives, booleans, positioning, sketch-to-3D, selectors, fillets. Every example is tested on each release. Top of the resource shows the installed build123d version the examples were tested against. |
| `build123d://selectors` | `text/plain` | Task-indexed selector cookbook: get the top face, find circular edges, filter by area/length/radius, `Select.LAST` in builder context, fillet detection, and the operator shortcuts. Every example is tested. Top of the resource shows the installed build123d version. |
| `build123d://drafting` | `text/plain` | Code-first 2D engineering drawings cookbook: project a 3D part to a view, dimension with `ExtensionLine`/`DimensionLine`, add tolerances, compose a `TechnicalDrawing` title block, multi-view sheet layout, hole-table pattern, export to DXF. Uses build123d's existing `build123d.drafting` primitives — the LLM picks dimensions in code, the library renders them deterministically. |
| `build123d://session` | `application/json` | Live session state: current shape diagnostics, named objects, snapshots, and Python namespace variables. Equivalent to calling `session_state()`. |
| `build123d://bd_warehouse` | `text/plain` | Catalogue of pre-built parametric parts from bd_warehouse: bearings, fasteners, gears, pipes, sprockets, threads. Includes class names, descriptions, constructor signatures, and available sizes. |

Read these resources at session start to avoid tool round-trips for orientation data.

---

## Prompt

### `start-cad-session`
Prime a new CAD design session with the task description and workflow reminders.

**Input:** `description` (string) — what you want to build

**Returns:** a user message containing the task description plus an 8-step workflow reminder (reset → execute → measure → render → snapshot → export).

---

## build123d quick reference

```python
from build123d import *

# Primitives
Box(length, width, height)
Cylinder(radius, height)
Sphere(radius)
Cone(bottom_radius, top_radius, height)

# Boolean operations (use mode=)
Box(5, 5, 5, mode=Mode.SUBTRACT)   # subtract from current part
Box(5, 5, 5, mode=Mode.INTERSECT)  # intersect with current part

# Location / movement
Pos(x, y, z)
Rot(x_deg, y_deg, z_deg)

# Context manager pattern (recommended)
with BuildPart() as part:
    Box(10, 10, 10)
    with Locations(Pos(0, 0, 5)):
        Cylinder(radius=3, height=10, mode=Mode.SUBTRACT)
result = part.part

# Boolean between separate shapes
combined = part_a + part_b
cut = part_a - part_b
intersection = part_a & part_b
```

All units are millimetres by default.

---

## Common mistakes

- **No shape yet:** `render_view`, `measure`, and `export` all fail if no shape exists. Always `execute` geometry first.
- **Forgetting imports:** the namespace starts empty. Include `from build123d import *` in your first `execute` call.
- **Shape not detected:** if the server doesn't pick up your shape, assign it explicitly to `result` or use `show()`.
- **Dirty session:** unexpected results often mean leftover state. Call `reset` first, or `session_state` to inspect what's active.
- **Boolean succeeded but geometry is wrong:** always call `measure()` after a boolean and check `topology.faces` — a failed cut leaves counts unchanged.
- **Using render_view as geometric proof:** renders can look correct even when geometry is wrong. Use `measure()` to verify numerically first.
- **Assembling with raw `.move()` instead of joints:** placing parts by absolute position works once but breaks the moment anything changes — the child has no relationship to the parent. Use `RigidJoint`/`RevoluteJoint`/etc. so the relationship is preserved.
- **Failed execute advancing state:** it doesn't — failed code preserves the previous `current_shape`.
- **Library tools without --library:** `search_library` and `load_part` return an error if the server wasn't started with `--library PATH`.
