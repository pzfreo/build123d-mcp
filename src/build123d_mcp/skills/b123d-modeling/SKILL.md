# Build 3D Geometry with build123d

Use this skill when asked to model, build, or modify a 3D part or assembly with
build123d — from a text description, a technical drawing (image or PDF),
dimensions in a spec, or an existing STEP/STL file.

**Use the build123d-mcp MCP server tools, not standalone Python scripts run with
the shell.** The server keeps a persistent build123d session, so you build in
small verified steps — `execute()` → `measure()` → `render_view()` — instead of
writing one large script and hoping. A one-shot script gives no numeric feedback
between features, and a single error throws away everything. (The one exception,
very heavy builds, is covered in Step 5.)

---

## Step 0 — Start the session

1. `reset()`, then `execute("from build123d import *")`.
2. Read the `build123d://quickref` resource before writing build code — it has
   accurate API syntax for the current build123d version.
3. If the part uses fasteners, bearings, or threads, read
   `build123d://bd_warehouse` and probe catalogue sizes before scripting.

## Step 1 — Extract the spec before modeling

Do not model while reading the input. First convert it into a parameter block.

**From a technical drawing** (image or PDF):

- Identify the views — front / plan (top) / side — and the projection
  convention; the title block states first- or third-angle, the SCALE
  (e.g. 2:1), and the units/tolerance standard.
- Printed dimension callouts are real part dimensions. Never measure the image,
  and never multiply a printed dimension by the drawing scale.
- Hidden (dashed) lines are internal features — holes, pockets, bores.
  Centrelines (long-short chain) mark hole centres and symmetry axes. Section
  hatching shows solid material in a cut view.
- Symbols: Ø diameter (not radius), R radius, M6/M8… threads,
  ⌴ counterbore, ⌵ countersink, typical hole note `4× Ø6.6 THRU`.
- Cross-check every feature in at least two views before trusting it — a circle
  in plan with no matching hidden lines in front is probably a boss, not a hole.

Then write the spec as named parameters in your first `execute()`:

```python
# All dims in mm, from drawing DWG-042 rev B
LENGTH, WIDTH, HEIGHT = 80.0, 50.0, 12.0
HOLE_DIA, HOLE_INSET = 6.6, 8.0      # 4x Ø6.6 THRU, 8 from each edge
FILLET_R = 3.0                        # vertical corners only
```

If the drawing leaves a critical dimension ambiguous — a value missing, or two
views disagreeing — ask the user which value to use. Do not guess silently.

## Step 2 — Build incrementally

- One feature (or one boolean) per `execute()` call. Small steps are easy to
  debug; a 60-line block that fails tells you nothing about which line broke.
- Register the part under a stable name as soon as it exists:
  `show(part, "part")`. `show()` prints volume and face count immediately,
  confirming the shape is non-empty.

**Author for editability — a design to edit, not a shape to render.** A
syntactically valid script that hard-codes every number is a *shape*, not a
*design*: no one can change the hole spacing without rebuilding from scratch.
Follow the design-state conventions (Arko-T §4.3) so an edit is a one-number
change:

- **Named parameter block at the top, with units** — `plate_thickness = 5.0  # mm`
  — never inline magic constants.
- **Consistent construction order** — base sketch/solid → secondary features
  (holes, ribs, pockets) → finishing (fillets, chamfers, shell).
- **Canonical feature idioms** so a feature name maps to the obvious construction
  pattern; reuse the same idiom for the same feature.
- **Derive coordinates from parameters** (expressions / references / selectors),
  not hand-computed magic positions — so moving one datum moves everything bound
  to it. See `build123d://quickref` Pattern 3 for a worked example.

Finish by running `design_audit()` (Step 6) to prove the parameters are robust.

## Step 3 — Verify numerically, then visually

`measure()` is the source of truth; renders confirm appearance, not geometry.

**Compose in code, don't copy numbers.** The analysis functions are callable *inside*
`execute()` and return real Python objects, so filter and compute in code instead of
reading a number out of one tool result and re-typing it into the next call:
`measure(part)["volume"]`, `[h for h in find_holes(part) if h.location[0] < 5]`,
`clearance(a, b)["clearance"]`, `align_check(a, b)["delta"]`. Also available:
`cross_sections`, `find_bosses`, `find_countersinks`, `find_hole_patterns`. They take a
shape (default: current shape); `measure`/`clearance`/`cross_sections` stay bounded on
large shapes. The standalone MCP tools remain for one-shot queries.

- **After every boolean (`-`, `+`, `&`) call `measure()`** and check
  `topology.faces` changed. Unchanged face/edge counts mean the boolean
  silently failed.
- Check the bounding box against the drawing envelope, and the face-type
  inventory against the features: each plain drilled hole contributes one
  cylinder face whose diameter must match the callout (Ø6.6 hole → 6.6 mm
  cylinder in the inventory). Identical faces are aggregated — `4× Ø6.6 THRU`
  shows as ONE cylinder entry with `"count": 4`, so sum the counts, don't
  count entries.
- Render only after `measure()` agrees with the spec:
  `render_view(save_to="/tmp/part.png")`, then show /tmp/part.png to the user.
  Use `clip_plane`/`clip_at` to reveal internal features.
- Assemblies: `clearance("a", "b")` for fit (apart / touching / containing /
  interpenetrating, with volumes), `align_check()` for flush/concentric checks,
  and connect parts with Joints (RigidJoint / RevoluteJoint / …) rather than
  raw `.move()` — see `build123d://quickref`.
- Editing an imported reference: after changing a part loaded with
  `import_cad_file()`, `shape_compare("input", "edited")` localizes *where* the
  geometry changed and reports the exact added/removed volume and surface
  displacement — confirm the changed region and magnitude match the request, and
  that the rest stayed put. A tangential move (sliding a hole) shows no region;
  cross-check `find_holes` and the bbox/center deltas for those.
- Use `find_holes`' bore axis for holes on curved or BSpline faces. Face centers
  and bounding-box centers can be off-axis; an apparent "already at target"
  result is a prompt to re-measure against the axis.
- Avoid large point grids with `is_inside()` on big solids. They are slow and can
  hit the operation timeout; prefer `cross_sections()` or a targeted clipped
  render for interiors.

## Step 4 — Experiments and recovery

- `save_snapshot("before_fillet")` before any operation you might want to undo;
  `restore_snapshot()` brings the geometry back (Python variables are NOT
  restored — re-run assignments).
- For "what if?" questions, use the loop: snapshot → mutate → measure/render →
  restore. It is cheaper and more accurate than rebuilding.
- If an `execute()` times out, only that one step is dropped: the worker restarts
  and the session is **rebuilt from your prior `execute()` history** — variables,
  shapes and named objects come back (snapshots and geometry imported via other
  tools do not). Just retry the step, smaller. Very long sessions may rebuild only
  partially if replay runs out of budget; `script()` returns the executed history.

## Step 5 — Heavy builds (threads, gears, many fillets)

The `execute()` timeout (default 120 s) hard-limits a single call. First, split the
heavy step into smaller `execute()` calls (build up incrementally — a timed-out step
is dropped, not the session) and/or raise the ceiling with `--exec-timeout N` or
`BUILD123D_EXEC_TIMEOUT=N` (this also extends the import budget for heavy STEP files).

For additive edits, avoid exactly coincident faces: they often do not fuse into a
clean solid. Interpenetrate slightly, bury the added feature into the base, or
extend-and-trim with one planar cut. For imported solids, prefer targeted solid
repair over broad shape healing; global healing can reorient faces or collapse
volume. When a validity/export failure has a recognizable topology pattern,
call `repair_advice(error_text=..., goal=..., context=...)` before writing the
repair code. It returns generic recipes and stop conditions for the agent to
implement explicitly in `execute()`, rather than silently mutating the B-rep.

**Extending a boss, or relocating any planar/annular face along its own normal
(raising a bore's counterbore opening, moving a shoulder) is a common instance
of this trap, with a better fix than interpenetrating.** These features have a
constant cross-section at the join, so unioning a new cylinder, tube or ring
on top — butt-joined or buried with overlap — often leaves the old face stuck
as a duplicate internal face the fuse won't dissolve (`validate()` FAILs with
"N mesh non-manifold edge(s) — faces meet >2-ways", the two pieces stay
separate solids, or a `validate()` PASS is followed by an `export()` failure
after the STEP round-trip re-checks orientation on the old face's remnants).
Extrude the feature's *own* face instead of adding a separately-built
primitive — a face extruded from the part's own boundary shares the exact
underlying geometry, so a follow-up fuse dissolves it cleanly where a
coincidentally-matching new primitive would not, even when it's positioned to
match exactly:

If the boss/annular edit still fails, pass the gate text and edit goal to
`repair_advice()`. The `split_bored_boss_extension` and
`export_roundtrip_sliver_cleanup` recipes capture the safer fallback: measure
the true feature axis, build an explicit full-profile extension, recut the bore
continuously, then remove only near-zero old-plane slivers if the written STEP
roundtrip exposes them.

- **One-sided** (extend/relocate toward a target on one end only — the more
  common case): `BRepFeat_MakePrism` turns the face into a prism *feature*
  that extends the existing topology in place, so there is no second solid to
  fuse at all. Take the direction from the seat's own normal — never hardcode
  an axis, since a boss off the part's dominant axis (tilted, horizontal, on a
  cast body) needs its own direction, and a wrong one silently collapses the
  result to zero volume with `IsDone()` still True:

  ```python
  from OCP.BRepFeat import BRepFeat_MakePrism
  from OCP.gp import gp_Dir
  # seat = the planar face to move (an annulus if it rims a bore or coaxial hole)
  n = seat.normal_at(seat.center())
  mk = BRepFeat_MakePrism(part.wrapped, seat.wrapped, seat.wrapped, gp_Dir(n.X, n.Y, n.Z), 1, True)
  mk.Perform(delta)  # delta = distance to move along the axis
  if not mk.IsDone():
      raise RuntimeError("BRepFeat_MakePrism failed — seat face may not belong to part's topology")
  extended = Solid(mk.Shape())
  ```

- **Symmetric** (both ends of a boss grow by the same total amount): no raw
  OCCT needed — `extrude()` each end-cap face along its own normal by half the
  total growth, then fuse both onto the same running result (fusing the
  second extension onto the original `part` instead of the already-extended
  result would silently discard the first end's growth):

  ```python
  half = delta / 2   # delta = TOTAL growth; each end grows by half
  top_ext = extrude(top_seat, amount=half)       # top_seat, bottom_seat = the boss's two end-cap faces
  bottom_ext = extrude(bottom_seat, amount=half)
  extended = part.fuse(top_ext).fuse(bottom_ext)
  ```

Either way, extruding the actual face preserves the exact cross-section — a
coaxial bore or hole extends along with the OD — so verify by re-measuring the
moved face's new position, not just that `validate()` passed; a `validate()`
PASS on this construction can still fail the stricter `export()` round-trip
check, which is the signal to switch from add-and-fuse to this technique if
you haven't already.

**Removing a small feature (a fillet, chamfer, or sliver) is the subtractive
mirror of the same trap.** A boolean cut broad enough to reach the feature
almost always removes more than the feature itself — the cutter's own profile
rarely matches the feature's exact geometry, so it shaves material off
adjacent faces too and can trigger unrelated topology merges elsewhere in the
part. Defeature the exact face(s) instead — `BRepAlgoAPI_Defeaturing` removes
only the named face and extends its neighbours to close the gap, changing
nothing beyond the feature's own volume:

```python
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
from OCP.TopoDS import TopoDS
df = BRepAlgoAPI_Defeaturing()
df.SetShape(part.wrapped)
df.AddFaceToRemove(bad_face.wrapped)   # bad_face = the fillet/chamfer face itself
df.Build()
removed = Solid(TopoDS.Solid_s(df.Shape()))
```

Verified on a boss with a root fillet: a broad annular cut removed **13×** the
fillet's own volume (it also shaved the adjacent straight boss wall) and grew
the face count; defeaturing the exact torus face removed only the fillet's
volume and *reduced* the face count, since the neighbouring faces extend and
merge instead of adding new ones. If the volume delta from a cut is much
larger than the feature you meant to remove, that mismatch is the signal to
switch to this technique, not to trim the cutter's dimensions further.

Only if a single unavoidable operation (IsoThread, a multi-body fillet, a very
high-face-count boolean) still can't fit, drop out of the session for that one op:

1. Probe the API in-session with small `execute()` calls.
2. Write the build as a script and run it with your shell tool.
3. `import_cad_file("part.step", "part")` to bring the result back in.
4. Verify as usual: `measure("part")`, `render_view(objects="part")`.

## Step 6 — Finish

1. Final `measure()` against the spec: envelope, volume sanity, hole inventory.
2. **`validate("part")` before exporting.** A STEP/STL that is not a watertight,
   manifold, single solid is rejected outright by CAD scorers and downstream
   tooling (CADGenBench scores it zero) — no matter how close the geometry is.
   A `FAIL` here almost always means the current shape is a leftover 2D sketch,
   an open shell, an un-fused compound (`Part() + ...`), or a degenerate boolean
   result; fix it and re-validate until it passes.
   `validate()` runs the same exact mesh check as export — in-process for small
   parts, out-of-process for large ones so it can't stall the session. A shape too
   big to stitch even there comes back `mesh_check: "skipped"` with a "mesh not
   verified" warning (not a silent pass) — treat that as a cue to test-export.
   `export()` re-checks the written STEP and is the authoritative verdict, so an
   occasional `validate()` PASS → `export()` warning is still expected on
   coincident faces, near-tangent joins, or a huge imported B-rep; test-export to
   a throwaway path before finalizing.
3. `export("part.step", "step", object_name="part")` — STEP for CAD interchange,
   STL for printing. If the project slices with
   [estampo](https://github.com/estampo/estampo) (`estampo.toml` present),
   add/update the `[[parts]]` entry for the exported file and run `estampo run`
   instead of stopping at export. To seed the entry's overrides, generate an
   estampo.toml fragment from the printability report with your shell tool:

   ```bash
   python -c "
   import augura
   from build123d import import_step
   report = augura.analyze(import_step('part.step'))
   print(augura.to_estampo_toml(report))
   "
   ```

   The fragment sets `enable_support`, `brim_type`, and advisory
   `[slicer.overrides]` comments — review and merge it into the `[[parts]]`
   entry rather than pasting blindly (see estampo's skill).
4. Unless the user opts out, save a clean regeneration script to
   `scripts/<part>.py`: the parameter block, the build steps, and the export
   call. Follow the project's existing script layout, and pick a non-colliding
   name if one exists. The part should live in version control as code, not
   only as a STEP artifact. Keep dimensions in a named parameter block at the
   top (`plate_thickness = 5.0  # mm`), not inline literals — then
   `design_audit()` can surface those parameters and perturb each ±10% to flag
   *brittle* ones (a nudge that fails the validity gate), so you ship an
   editable design, not just a valid shape.
5. If the part will be FDM printed, run `analyze_printability("part")` and
   report overhangs / thin walls / bed-fit findings.
6. For an engineering drawing of the finished part, switch to the b123d-drawing
   skill (or the `build123d://skill/drawing` resource).

---

## Pitfalls

- `Box(...) + Cylinder(...)` returns a **ShapeList**, not a fused Part — it has
  no `.volume` or `.faces()`. Fuse with `Part() + Box(...) + Cylinder(...)` or
  `box.fuse(cyl)`.
- Fillet failures usually mean the radius is too large for the local geometry
  or the selected edges are non-manifold — reduce the radius or select fewer
  edges.
- Selector indices are not stable across rebuilds. Use
  `resolve("part", ".faces().sort_by(Axis.Z)[-1]", label="top")` to confirm a
  selector grabs the entity you think it does.
- For fillet/chamfer edge selection on turned parts, use the built-in
  `find_edges(shape, geom="circle", radius=4.25, at_z=10.2)` instead of
  hand-rolled filtering — it prints the match count, radii, and Z levels so a
  wrong selection is visible before the fillet runs.
- Errors from `execute()` come back with a failure classification and fix hint —
  read them before retrying; `last_error()` has the line number and excerpt.
- `show()` stores by reference: mutating a shape after `show()` changes the
  stored object too. Re-`show()` under a new name to keep a frozen copy.
