# Repair Invalid Geometry with build123d (b123d-repair)

Use this skill when `validate()` or the `export()` gate reports FAIL on a shape —
an imported STEP that arrives broken, or a solid your own construction damaged —
and the goal is a watertight, manifold, BRepCheck-valid solid that passes the
export gate **without changing the geometry** beyond the defect itself.

Every recipe here was proven on a real defective part; each entry says when it
applies, when it fails, and what to try next. Work the ladder in order — the
cheap fixes first — and verify every attempt with the **export gate**, not
`validate()` alone (Step 2 explains why).

---

## Step 0 — Diagnose before cutting

Do not apply repairs blind. First identify the defect class and its location.

1. **Read the gate output precisely.** The FAIL reason names the class, and the
   class picks the recipe:
   - `B-rep is not well-formed (BRepCheck failed)` → a malformed face
     (usually unorientable, zero-area, or reversed) — Step 1, rungs 1-4.
   - `N open edge(s) — not watertight` → unsewn/missing faces — rungs 4-5.
   - `N non-manifold edge(s)` → an edge shared by 3+ faces — usually a
     coincident-face construction defect; prefer re-construction (Step 4)
     over surgery.
   - `mesh open edge(s)` / `mesh non-manifold` / `face(s) failed to
     tessellate` **with `brep_valid: true`** → a mesh-only defect (a
     ~zero-area unmeshable face, or a self-touch) — rung 5.
2. **Get coordinates.** `locate_gate_defects()` returns the failing edge/face's
   3D position and B-rep identity — repair that exact spot, never chase the
   defect blind.
3. **Localize the face** when BRepCheck is the failure — build ONE analyzer
   over the whole solid, not one per face (`locate_gate_defects()` itself
   runs out-of-process specifically because per-face BRepCheck work "can run
   for minutes on a complex part"; reconstructing an analyzer per face inside
   `execute()` on a large import risks the same timeout):

   ```python
   from OCP.BRepCheck import BRepCheck_Analyzer
   analyzer = BRepCheck_Analyzer(part.wrapped)   # one pass over the whole solid
   suspects = [
       (i, f.geom_type, round(f.area, 6), f.center())
       for i, f in enumerate(part.faces())
       if not analyzer.IsValid(f.wrapped) or abs(f.area) < 1e-6
   ]
   print(suspects)   # match centers against locate_gate_defects() coordinates
   ```

4. **Decide: inherited or self-introduced?** If the shape was valid before your
   last operation, the defect is self-introduced — `restore_snapshot()` and
   rebuild that step using the avoidance rules in Step 4 instead of operating
   on the damaged result. Surgery is for defects you cannot construct away
   (imported files, or upstream steps too expensive to redo).

---

## Step 1 — The repair ladder (cheapest first)

Try each rung in order; verify with Step 2 after every attempt; stop at the
first rung whose result passes the export gate.

### Rung 1 — `ShapeFix_Shape` (seconds, non-destructive)

Fixes reversed faces (a boolean can flip a distant face — e.g. a hole-bottom
sphere reporting negative area) and zero-area sliver faces a boolean leaves
elsewhere on an imported shape.

```python
from OCP.ShapeFix import ShapeFix_Shape
fix = ShapeFix_Shape(part.wrapped)
fix.Perform()
healed = Part(fix.Shape())
```

Fails when: the face is genuinely unorientable (its own wire is inconsistent) —
ShapeFix reports success but the export gate still FAILs. Move to rung 2.

This rung operates on the whole shape, so it's the one place this ladder's
"only touch the defect" discipline needs a caveat: the standing rule to
**prefer targeted solid repair over broad shape healing** (global healing
can reorient faces or collapse volume) still applies here. Treat `healed`
as a candidate, not the result — the Step 2 volume/bbox check is what makes
this rung safe to try first; if it moves anything beyond the defect's own
faces, discard it and go straight to rung 3 (defeature), which is targeted
by construction.

### Rung 2 — a clean boolean heals the B-rep

A well-formed OCCT fuse forces a full re-computation of the shell and can heal a
BRepCheck-invalid import as a side effect. Fuse with a small solid that
interpenetrates the part somewhere harmless (or union two halves of the part).

Cautions, both observed in the field:
- **Booleans can silently DROP an invalid solid** — always check
  `healed.volume` is within a fraction of a percent of the original.
- On a **self-intersecting** import, build123d `Part` operators can inflate
  volume; use the raw OCCT API on the bare solid instead:

  ```python
  from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
  op = BRepAlgoAPI_Fuse(part.solids()[0].wrapped, tool.wrapped)
  op.Build()
  healed = Part(op.Shape())
  ```

### Rung 3 — defeature the malformed face

`BRepAlgoAPI_Defeaturing` removes the named face(s) and extends the neighbours
to close the gap — the right tool when the defect is a discrete face and its
neighbours are healthy. Also removes a feature's faces cleanly (grooves, bores)
without plugging.

```python
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
df = BRepAlgoAPI_Defeaturing()
df.SetShape(part.wrapped)
df.AddFaceToRemove(bad_face.wrapped)
df.Build()
healed = Part(df.Shape())
```

**Volume check is mandatory here**: defeaturing can silently *fill an internal
bore* whose wall included the removed face (observed: +14% volume — a ruined
part that passed the gate). Accept the heal only if the volume delta is on the
scale of the defect itself, not of a feature.

Fails when: the sliver's neighbours can't extend to close the gap, or the
defeature output fails the gate. Move to rung 4.

### Rung 4 — face surgery for an unorientable sliver

Thin sliver faces (a degenerate fillet remnant, a band between near-coincident
arcs) are the classic BRepCheck killer on imported castings. Escalate through
these four, in order:

1. **Replace with a ruled face.** Build a `BRepFill` ruled face between the
   sliver's two long edges, drop the original, sew everything at small
   tolerance. Works when the sliver's bounding edges are clean but its surface
   is garbage.
2. **Drop + tolerant sew.** When the sliver's own wire is broken (a ruled/fill
   replacement comes out unorientable too), delete the face entirely and sew
   the neighbours with **tolerance greater than the sliver's width** (e.g.
   0.6 mm for a 0.5 mm sliver), then `ShapeFix_Solid` to orient the shell:

   ```python
   from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
   from OCP.ShapeFix import ShapeFix_Solid
   from OCP.TopoDS import TopoDS
   sew = BRepBuilderAPI_Sewing(0.6)          # tol > sliver width
   for f in part.faces():
       if not f.wrapped.IsSame(bad_face.wrapped):
           sew.Add(f.wrapped)
   sew.Perform()
   shell = TopoDS.Shell_s(sew.SewedShape())       # sewn result -> TopoDS_Shell
   solid = ShapeFix_Solid().SolidFromShell(shell)  # orient into a proper solid
   healed = Part(solid)
   ```

3. **Patch + small-tolerance sew.** On a *wide* sliver (~2-3 mm), drop+sew
   goes non-manifold (the big tolerance welds faces that shouldn't meet).
   Instead fill the sliver's boundary wire with a `BRepFill` patch face and
   sew at small tolerance.
4. **Cut it out.** When the sliver is intrinsic to the geometry (a curved band
   between two near-coincident arcs), in-memory fixes only *fake-heal* — the
   gate fails again after the export round-trip. Remove the region physically:
   boolean-subtract a thin box enclosing the sliver. This changes geometry by
   the sliver's own (near-zero) volume, which is the point of last resort.

### Rung 5 — mesh-gate-only failures (B-rep valid, mesh check FAILs)

A BRepCheck-valid import can still fail the gate on a ~zero-area **unmeshable**
face (often a degenerate BSpline): the tessellated boundary can't close. Drop
that face (plus any adjacent unorientable sliver), sew, and `ShapeFix_Solid`
to orient — rung 4's mechanics, triggered by the mesh reasons instead of
BRepCheck. A zero-area *torus* remnant that fails only the export gate heals
the same way.

---

## Step 2 — Verify every heal with the export gate

- **The export gate is authoritative.** An in-memory heal can be fake:
  `validate()` on the live shape passes, then the written-and-reimported STEP
  fails. Always `export()` to a throwaway path and read its gate verdict; on a
  huge shape `validate()`'s own mesh check can come back `mesh_check: "skipped"`
  (too large to stitch even out-of-process) with a "not verified" warning, so
  treat its PASS as a screen, not a verdict.
- **A heal must not change the geometry.** Compare `volume` and bounding box
  before/after: the acceptable delta is the scale of the defect (a sliver's
  near-zero volume), never a feature's. A heal that gains or loses real volume
  replaced your part with a different part.
- **Never keep iterating on an invalid solid.** If a rung's attempt fails the
  gate, `restore_snapshot()` back to the pre-attempt state before trying the
  next rung — stacked failed repairs compound.
- `save_snapshot()` before each rung so the above is one call.

---

## Step 3 — Editing an invalid import: heal FIRST, then edit

When the task is to *modify* an imported part whose STEP is already invalid,
do not interleave healing with editing:

1. Import, `validate()`, and if it FAILs, run this skill's ladder **first**.
2. When the healed import passes the export gate, `save_snapshot("valid_baseline")`
   **and** `export()` it to the real output path — from this moment a valid
   artifact exists on disk no matter what happens later.
3. Then perform the requested edit on the healed baseline, re-validating after
   each step as usual; re-export only on PASS.

If a heavy heal (a large ShapeFix or boolean on a big import) exceeds the
`execute()` timeout, run that one operation as a standalone script via the
shell, then `import_cad_file()` the result back — same pattern as heavy builds.

---

## Step 4 — Don't introduce the defect in the first place

Most self-made gate failures are one of these constructions; fix the
construction, not the corpse:

- **Exactly-coincident faces don't fuse.** A union whose faces are coplanar
  with the base's (a box bottom flush with a feature's bottom, a boss butted
  on a same-radius lobe, a thread root exactly on the bore wall) can pass
  `validate()` and FAIL the export gate with non-manifold or open edges.
  Interpenetrate: bury the added feature 1-2 mm into the base, make the
  footprint 2-3 mm larger, or extend past and trim with one planar cut.
- **Tangencies leave open edges.** A hole tangent to a coaxial hub wall, a
  boss grazing a torus fillet — nudge the position ~0.3 mm or extend the boss
  coaxial with the adjacent straight cylinder so the intersection is clean.
- **Patterned features must fuse into one solid.** If fusing a fin/rib field
  leaves two disjoint solids, bury each feature's base 1-2 mm so it
  interpenetrates.
- After every boolean, `measure()` and confirm face counts changed and volume
  moved in the right direction — a silently-failed boolean is tomorrow's gate
  failure.
