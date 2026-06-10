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
- Build from the parameters, never from magic numbers — the part must
  regenerate when a dimension changes.
- Register the part under a stable name as soon as it exists:
  `show(part, "part")`. `show()` prints volume and face count immediately,
  confirming the shape is non-empty.

## Step 3 — Verify numerically, then visually

`measure()` is the source of truth; renders confirm appearance, not geometry.

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

## Step 4 — Experiments and recovery

- `save_snapshot("before_fillet")` before any operation you might want to undo;
  `restore_snapshot()` brings the geometry back (Python variables are NOT
  restored — re-run assignments).
- For "what if?" questions, use the loop: snapshot → mutate → measure/render →
  restore. It is cheaper and more accurate than rebuilding.
- If an `execute()` times out, the worker restarts and **all session state is
  lost** — re-run your setup. Keep the build reproducible (Step 6's script) so
  this costs one paste, not a rebuild from memory. `script()` returns the
  executed history if you need to reconstruct.

## Step 5 — Heavy builds (threads, gears, many fillets)

The `execute()` timeout (default 120 s) hard-limits a single call. For builds
with expensive booleans (IsoThread, multi-body fillets, very high face counts):

1. Probe the API in-session with small `execute()` calls.
2. Write the build as a script and run it with your shell tool.
3. `import_cad_file("part.step", "part")` to bring the result back in.
4. Verify as usual: `measure("part")`, `render_view(objects="part")`.

The ceiling can be raised with `--exec-timeout N` or `BUILD123D_EXEC_TIMEOUT=N`
(this also extends the import budget for heavy STEP files).

## Step 6 — Finish

1. Final `measure()` against the spec: envelope, volume sanity, hole inventory.
2. `export("part.step", "step", object_name="part")` — STEP for CAD interchange,
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
3. Unless the user opts out, save a clean regeneration script to
   `scripts/<part>.py`: the parameter block, the build steps, and the export
   call. Follow the project's existing script layout, and pick a non-colliding
   name if one exists. The part should live in version control as code, not
   only as a STEP artifact.
4. If the part will be FDM printed, run `analyze_printability("part")` and
   report overhangs / thin walls / bed-fit findings.
5. For an engineering drawing of the finished part, switch to the b123d-drawing
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
