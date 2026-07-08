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
   save_snapshot("before_edit")
   print(measure(part))
   ```

5. Run the MCP `validate()` tool on the registered baseline. If the baseline
   already fails the gate, stop and switch to
   `build123d://skill/repair`. Do not combine a feature edit with geometry
   repair unless the requested edit is the repair.

For file-based work, keep the source of truth in the Python file. The MCP
session is the proving ground, not the only copy of the edit.

## Step 1 - Translate the Request Into Code Intent

Classify the edit before changing code:

- **Parameter edit**: dimensions, radii, offsets, thickness, counts, or angles.
  Prefer changing named parameters over replacing literals deep inside geometry.
- **Feature edit**: add/remove/move a hole, boss, rib, pocket, fillet, chamfer,
  counterbore, or pattern. Prefer editing the feature construction block.
- **Assembly/edit-in-context**: move or resize one part relative to another.
  Use `clearance()`, `align_check()`, and named objects to verify fit.
- **Validity edit**: change construction so the output remains manifold.
  Use `validate()`, `locate_gate_defects()`, `repair_advice()`, and the repair
  skill only for diagnostics and patterns. `repair_advice()` is especially
  useful when the intended edit is generic but topology-sensitive, such as
  extending a bored boss, moving an annular shoulder, or fixing an
  export-roundtrip sliver after a boolean.

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
`find_bosses()`, and `find_countersinks()` to identify the feature in code. For
spatial edits, render with labels or use `render_view(highlights=...)` to confirm
the face/edge index before changing construction.

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

Then run the MCP `validate("edited")` tool. For handoff or benchmark output,
run `export("edited.step", "step", object_name="edited")` so the written STEP is
checked too.

Then choose the evidence that matches the edit:

- envelope changed: compare `measure()["bbox"]`
- volume changed: compare `measure()["volume"]`
- hole changed: run `find_holes()` or `find_hole_patterns()`
- counterbore changed: run `find_countersinks()`
- fit changed: run `clearance(a, b)` or `align_check(a, b)`
- source-level edit may be brittle: run `design_audit()`
- shape should otherwise match a baseline: run `shape_compare("before", "edited")`

`export()` is the final gate because it checks the written and re-imported STEP.
A `validate()` pass in memory is useful but not the final acceptance proof.
If the edited shape fails this gate, call `repair_advice(error_text=..., goal=...)`
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

Verify with `clearance(shaft, bore_part)` or a measured section.

### Remove a Feature

Prefer removing the feature's construction block over adding material back with
a compensating boolean. If you must patch a removed cut, prove the final face and
volume match the intended design, then export-gate the result.

### Topology-Sensitive Edits

For edits that often create open shells, duplicate internal faces, or disjoint
solids, ask the MCP for a recipe before trying variants:

```text
repair_advice(
  error_text="<validate/export failure text>",
  goal="extend the square boss with rounded corners and central bore by 10mm",
  context="<locate_gate_defects or shape_compare notes>"
)
```

Typical matches include split bored-boss extension, export-roundtrip sliver
cleanup, mesh-fragile face repatching, and self-touch boolean redesign. The
result should guide explicit build123d/OCP code in `execute()`.

### Preserve Editability

If a requested edit forces many coordinated numeric changes, hoist them into
named parameters and derived expressions. Run `design_audit()` when the model
will likely be edited again.
