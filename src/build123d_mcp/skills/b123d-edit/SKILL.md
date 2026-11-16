# Edit Existing build123d Geometry (b123d-edit)

Use this skill when the task is to change an existing build123d model or script:
"make this bracket 5 mm taller", "move the holes inward", "add a counterbore",
"remove this rib", "make it fit this mating part", or "repair the code so the
intended edit remains parametric".

The goal is not to sculpt the final B-rep. The goal is to make an explicit,
reviewable code edit that preserves design intent, then prove the changed model
is valid and the intended delta happened.

## Step 0 - Establish the Baseline

Before editing, capture what exists.

1. Read the source script or recover the session code with `script()`.
2. Run the current model in the MCP session with `execute()`.
3. Register the edited target with `show(part, "baseline")` or a meaningful name.
4. Capture numeric evidence:

   ```python
   print(measure(part))
   ```

5. Run the MCP `validate()` tool on the registered baseline. For tasks whose
   deliverable is an exported STEP, use `bank_candidate()` with the real output
   path and `snapshot_name="valid_baseline"`. It writes privately, runs the
   written-and-reimported gate, and promotes both the file and snapshot only on
   PASS. For other CAD formats, export the unchanged baseline to a safe
   throwaway path and read the export gate result. For script-only work, a
   successful rebuild plus `validate()` is the baseline proof unless the task
   later requires a file export.
6. If the baseline fails the relevant proof (`validate()` for script-only work;
   `validate()` plus the throwaway export gate for exported-solid work), stop
   and switch to `build123d://skill/repair`. Until a repaired baseline passes
   that same proof, the only geometry-changing operations allowed are minimal
   baseline fixups. Do not move, cut, extend, remove, or replace the requested
   target feature while the baseline is still invalid. Do not combine a feature
   edit with geometry repair unless the requested edit is the repair.
7. After the baseline passes the relevant proof, save a snapshot such as
   `save_snapshot("valid_baseline")` if `bank_candidate()` did not already do
   so. Only then begin the requested feature or parameter edit.

For file-based work, keep the source of truth in the Python file. The MCP
session is the proving ground, not the only copy of the edit.

## Step 1 - Translate the Request Into Code Intent

Classify the edit before changing code:

- **Parameter edit**: dimensions, radii, offsets, thickness, counts, or angles.
  Prefer changing named parameters over replacing literals deep inside geometry.
- **Feature edit**: add/remove/move a hole, boss, rib, pocket, fillet, chamfer,
  counterbore, or pattern. Prefer editing the feature construction block.
- **Assembly/edit-in-context**: move or resize one part relative to another.
  Use the MCP `compare()` tool with `kind="fit"` or `kind="align"` and named
  objects to verify fit. Inside `execute()`, the lower-level Python helpers
  `clearance(a, b)` and `align_check(a, b)` remain available for branching in
  code.
- **Validity edit**: change construction so the output remains manifold.
  Use `validate()`, `locate_gate_defects()`, `find_bored_bosses()`,
  `repair_advice()`, and the repair skill only for diagnostics and patterns.
  `find_bored_bosses()` is useful before lengthening any boss that carries
  a bore: it reports candidate bore axes, cap faces, split
  caps, and construction warnings. `repair_advice()` is especially useful when
  the intended edit is generic but topology-sensitive, such as extending a
  bored boss, moving an annular shoulder, or fixing an export-roundtrip sliver
  after a boolean.

If the requested edit is ambiguous, ask for the missing design intent before
guessing: which face, which hole pattern, which mating clearance, or which
dimension should be held constant.

## Step 2 - Find the Right Code to Change

Edit the highest-level expression that owns the feature.

Good targets:

- named parameters at the top of the script
- helper functions such as `make_plate(width, height, thickness)`
- one feature block, for example the loop that creates a bolt pattern
- one transform, for example `Location((x, y, z))` for a moved subpart

Avoid:

- editing generated STEP/STL output instead of the source code
- replacing a parametric feature with a one-off imported solid
- applying broad OCP healing for a requested design change
- changing unrelated dimensions because they make the gate pass

Use `measure()` face inventory, `find_holes()`, `find_hole_patterns()`,
`find_bosses()`, `find_bored_bosses()`, and `find_countersinks()` to identify
the feature in code. For spatial edits, render with labels or use
`render_view(highlights=...)` to confirm the face/edge index before changing
construction.

For an imported B-rep, call `inspect_features(target="part")` for a compact
inventory, then expand likely families with `families="holes,bosses,fillets"`.
Records and selected face descriptors use the imported model's coordinates.
Importing as `name="part"` binds the exact registered object as `part` inside
`execute()`. Responses show `execute_name` or a `get_object("part")` fallback
for naming collisions. A repair/edit registered with `show(candidate, "part")`
rebinds `part` to that result; snapshot restore rebinds registered objects too.

Request descriptors with `feature_ids="holes:12@<digest>"`; paste the returned
face dict/list into `select_faces(part, faces=...)` in `execute()`. This returns
live `ShapeList` faces by analytic geometry, without trusting STEP indices.
Every descriptor must uniquely match or raises a clear error. Keyword filters
return all matches, e.g. `select_faces(part, kind="cylinder", radius=4)`.
Default length tolerance is max(1e-5 mm, bbox diagonal * 1e-7), area tolerance
max(1e-5 mm², face area * 1e-7), fixed direction/angle 1e-5. `tol` overrides
length tolerance in mm; area tolerance is then 2*sqrt(face area)*tol + tol²
(mm²). It never relaxes direction checks. Cone `radius` is the reference radius
in the surface parameter frame; torus `radius` is the minor radius.
Descriptor lists must be nonempty and resolve to distinct faces, and cannot be
combined with keyword filters. A variable reassignment warns until registered
with `show`; registrations preserve unrelated user variables and report
`get_object(name)` as the access path when binding is refused.
IDs describe the current STEP bytes:
inspect again after changing geometry. Empty recognition is not permission to
substitute a nearby feature. Check axis, location and starting dimensions
against the request, and identify mating interfaces explicitly.

For a plain through hole with one cylindrical wall, use
`edit_feature(feature_id="holes:12@<digest>", diameter=8, object_name="part",
protected_refs='["holes:13@<digest>"]')`. It checks predicted material change, all other
holes and the envelope, and refuses dependent hole stacks or face mismatches.
For other features, use the manual workflow below and verify with `diff_step`.

## Step 3 - Make One Explicit Edit

Make a small source edit, then run the complete model.

For a parameter edit:

```python
# Before
plate_thickness = 6.0

# After
plate_thickness = 8.0
```

For a feature move, keep the relationship visible:

```python
hole_offset_x = old_hole_offset_x - 2.5
holes = [Pos(x, y) * Circle(hole_radius) for x, y in hole_centers]
```

For a fit edit, name the clearance and target:

```python
target_clearance = 0.4
pin_radius = bore_radius - target_clearance / 2
```

Do not stack several speculative edits. If the first edit fails, restore the
snapshot or revert the source hunk before trying the next approach.

## Step 4 - Prove the Delta

After each edit, verify both validity and intent.

Use this minimum loop:

```python
show(part, "edited")
print(measure(part))
```

Then run the MCP `validate("edited")` tool. For handoff or final STEP output,
run `bank_candidate("edited.step", object_name="edited", snapshot_name="final")`
so the written STEP replaces the safe floor only after its gate passes. Use
`export()` for diagnostic writes or non-STEP formats.

Then choose the evidence that matches the edit:

- envelope changed: compare `measure()["bbox"]`
- volume changed: compare `measure()["volume"]`
- hole changed: run `find_holes()` or `find_hole_patterns()`
- counterbore changed: run `find_countersinks()`
- fit changed: run `compare(a="a", b="b", kind="fit")` or
  `compare(a="a", b="b", kind="align", axis="Z", mode="flush")`
- source-level edit may be brittle: run `design_audit()`
- shape should otherwise match a baseline: run
  `diff_step(old="before", new="edited")`

For imported B-reps, use `diff_step(old="baseline", new="edited", render=True)`
to inspect adapted, orphaned and new features and unclaimed analytic face changes.
Verify the requested dimensions and protected interfaces explicitly; the diff's
analytic descriptors do not prove that every trim contour is unchanged. Retain
the validity/export gate and exact material checks where required.

For mesh-derived solids imported from 3MF or reconstructed from tessellated data,
add these checks to the loop:

- Record the baseline solid count and gate result before editing. Faceted input can
  contain inherited seams or near-coincident shells that only separate when a
  boolean forces OCC to rebuild topology.
- Do not place a cutter boundary exactly on the face being moved. Extend it a small,
  scale-appropriate distance into open space, then verify that the result contains
  no zero-thickness fragments. Treat `0.05 mm` as an observed example, not a
  universal tolerance.
- After every boolean, assert the intended solid count. If an unintended microscopic
  gap appears, diagnose it before considering a tolerant fuse; choose any fuse
  tolerance from the measured gap and verify protected dimensions afterwards.
- Analytic recognisers can legitimately return zero on faceted cylinders. Confirm
  whether the faces are analytic before interpreting an empty `find_holes()` or
  `find_bosses()` result as absence of the feature.
- Mesh booleans and before/after intersections can be unstable on dirty inputs.
  Cross-check the result's direct dimensions, solid count, and fit properties. For
  collision tests near facet noise, compare against an unchanged control and sweep
  a small allowance rather than trusting one absolute overlap volume.

The gate inside `bank_candidate()` is final because it checks the written and
re-imported STEP without overwriting an existing good output on failure. A
`validate()` pass in memory is useful but not the final acceptance proof. If the
edited shape fails this gate, call `repair_advice(error_text=..., goal=...)`
before improvising OCP surgery. Use the returned recipe as a checklist for a
visible `execute()` implementation, not as permission to make an opaque B-rep
mutation.

## Step 5 - Keep Regression Evidence

Before finishing, report:

- what source location changed
- which design intent was preserved
- before/after dimensions or feature records
- validation/export result
- any known tradeoff or unverified requirement

If a change fixes a bug, add a focused test or a small reproduction script when
the repository has a test harness. The test should assert the user-visible
geometry or gate result, not just that the script runs.

## Common Edit Patterns

### Resize Without Moving the Reference Face

When increasing thickness or height, decide which face stays fixed. For a part
whose bottom stays on Z=0, shift the solid or sketch origin so the added material
goes only upward.

```python
height = 18.0
part = Pos(0, 0, height / 2) * Box(width, depth, height)
```

### Move a Hole Pattern

Keep the pattern generator intact and edit its center/spacing.

```python
pattern_center = (new_x, new_y)
hole_centers = [
    (pattern_center[0] + dx, pattern_center[1] + dy)
    for dx, dy in offsets
]
```

Verify with `find_hole_patterns()` or by printing sorted hole locations.

### Add Clearance

Model clearance as a named parameter. Do not hide it in a radius literal.

```python
clearance_diametral = 0.5
bore_radius = shaft_radius + clearance_diametral / 2
```

Verify with `compare(a="shaft", b="bore_part", kind="fit")` or a measured section.

### Remove a Feature

Prefer removing the feature's construction block over adding material back with
a compensating boolean. If you must patch a removed cut, prove the final face and
volume match the intended design, then export-gate the result.

For imported fillet or chamfer removal, treat the job as defeaturing when the
rounded faces are explicit analytic patches. Identify the exact torus, cone, or
small blend faces, remove those faces, and let adjacent cylinders/planes extend
or merge. A valid defeatured result with fewer/cleaner faces is usually preferable
to a topology-preserving cutter that leaves extra cylindrical bands or seam faces.
Accept the defeature only after the written export gate passes and the delta
proof shows the removed material is localized to the requested fillet/chamfer.

### Move an Imported Face Set Locally

Use this when there is no editable source feature and a broad cut would damage
other blends or a closed interior cavity. The helpers are explicit operations
inside `execute()`, not automatic editing tools. Establish a valid baseline first.

1. Call `inspect_features("part", families="bosses,fillets")`, then request the
   selected content-bound feature IDs to obtain face descriptors. Check the
   starting height, axis and fillet radius.
2. Copy the returned face arrays for the cap and its top fillet into
   `cap_descriptors` and `top_blend_descriptors` inside `execute()`. Select with
   those descriptors; do not replace them with broader keyword filters:


   ```python
   cap = select_faces(part, faces=cap_descriptors)
   top_blend = select_faces(part, faces=top_blend_descriptors)
   assert len(cap) == 1 and len(top_blend) == 1
   edited = translate_faces(part, cap + top_blend, (0, 0, -5))
   ```

3. Register a review candidate with `show(edited, "candidate")`; call
   `diff_step(old="part", new="candidate")` and verify the intended boss height,
   retained R3 blend and unchanged interior cavity/features. After accepting the
   delta, call `show(edited, "part")`, then bank/export through the written gate.

`translate_faces` preserves the rigidly moved patches and extends/trims adjacent
planes/cylinders. Select tangent blends together with a moving wall/cap; a pocket
wall and its two corner fillets can move together without changing their radii.
`offset_faces(part, selected, 2)` instead applies an outward-normal offset (e.g.
a cylindrical wall changes radius). Both return a new gated Solid, register
nothing and preserve their input. They refuse unsupported neighbours/connecting
curves, face collapse, tangent offset propagation and self-intersection. A cone,
sphere or torus neighbour is refused if its unchanged support cannot contain the
translated boundary. Restore the baseline and change the source construction
when the explicit local operation is refused; do not accept collateral edits.

### Topology-Sensitive Edits

For edits that often create open shells, duplicate internal faces, or disjoint
solids, ask the MCP for a recipe before trying variants:

```text
repair_advice(
  error_text="<validate/export failure text>",
  goal="lengthen the flanged boss and its through bore by 8mm",
  context="<locate_gate_defects or diff_step(old='before', new='candidate') notes>"
)
```

Typical matches include split bored-boss extension, export-roundtrip sliver
cleanup, mesh-fragile face repatching, and self-touch boolean redesign. The
result should guide explicit build123d/OCP code in `execute()`.

Before extending a bored boss, run `find_bored_bosses()` on the baseline. If it
reports `is_split_cap=true` or multiple `cap_faces`, do not extrude one cap face:
construct a full extension sleeve/profile from the measured bore axis and
opening plane, then re-cut the bore continuously through old plus new material.
Reject candidates that fill the bore, create a second bore segment, or leave
more than one solid.

### Preserve Editability

If a requested edit forces many coordinated numeric changes, hoist them into
named parameters and derived expressions. Run `design_audit()` when the model
will likely be edited again.
