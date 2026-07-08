# Repair Invalid Geometry with build123d (b123d-repair)

Use this skill when `validate()` or the `export()` gate reports FAIL on a shape —
an imported STEP that arrives broken, or a solid your own construction damaged —
and the goal is a watertight, manifold, BRepCheck-valid solid that passes the
export gate **without changing the geometry** beyond the defect itself.

Every recipe here was proven on a real defective part; each entry says when it
applies, when it fails, and what to try next. Work the ladder in order — the
cheap fixes first — and verify every attempt with the **export gate**, not
`validate()` alone (Step 2 explains why).

This skill is deliberately an agent-authored repair workflow, not an automatic
healer. The MCP server should help you write better build123d/OCP code by
answering:

- `validate()` / `export()` — is the current or written shape structurally
  acceptable?
- `locate_gate_defects()` — where is the failing face, edge, or mesh defect?
- `repair_advice()` — which field-proven, generic repair/edit recipe fits this
  defect and requested goal, with acceptance checks and stop conditions?
- this repair skill — which generic repair pattern fits that defect class?

The geometry-changing repair itself should be explicit code in `execute()`,
with named variables, printed measurements, `save_snapshot()` / rollback
points, and a visible volume/bbox/gate audit. Do not delegate the repair to an
opaque MCP tool that silently manipulates the B-rep and returns a shape; that
prevents the agent from reasoning about design intent and makes benchmark
success hard to distinguish from accidental geometry surgery.

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
   - `face(s) failed to tessellate at a finer mesh deflection`
     **with `brep_valid: true`** → a tolerance-sensitive mesh-only defect:
     the face meshes at the default gate deflection but fails when a downstream
     consumer asks for a finer mesh — rung 5, using the re-patch/re-sew variant.
   - `vertex(es) where a tessellated edge endpoint misses its BREP vertex`
     **with `brep_valid: true`** → a mesh-only defect too, but a different
     one from the two above: a previously-patched/healed face's boundary is
     topologically closed (so BRepCheck and even `mesh open edge(s)` can both
     read clean) but geometrically off-vertex by a fraction of a millimetre —
     rung 5, using its re-patch-at-tighter-tolerance variant, not the
     drop-and-sew one.
2. **Get coordinates.** `locate_gate_defects()` returns the failing edge/face's
   3D position and B-rep identity — repair that exact spot, never chase the
   defect blind. Read its top-level `diagnosis` block too: `primary_kind`,
   `diagnostic_classes`, and `repair_families` tell you which rung to try first,
   while each defect's `next_step` says what explicit `execute()` repair to
   write and reminds you to verify the written STEP with `export()`.
   If the defect belongs to a known hard pattern, call `repair_advice()` with the
   gate output as `error_text`, the requested edit as `goal`, and the defect
   coordinates as `context`. Use the returned recipe as a checklist for your own
   `execute()` code; do not treat it as a geometry-mutating tool.
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

Every rung below wraps its raw OCCT result with `as_solid()`, not a bare
`Part(...)` call — `Part(raw_shape)` has been observed to silently report
`volume=0` on a genuinely healed shape (`IsDone()` True, geometry fine), and a
bare `Solid(raw_shape)` throws `Standard_TypeMismatch` whenever the shape is a
`Compound` rather than a plain `Solid` — which is the normal case for anything
built with build123d's own operators (even a plain `Box()`), not just imported
STEP files. Define this once and reuse it for every rung:

```python
from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_COMPOUND
from OCP.TopoDS import TopoDS, TopoDS_Iterator
from OCP.ShapeFix import ShapeFix_Solid

def as_solid(shape):
    """Wrap a raw TopoDS_Shape (Solid, Compound, or sewn Shell) with a correct volume."""
    st = shape.ShapeType()
    if st == TopAbs_SOLID:
        return Solid(TopoDS.Solid_s(shape))
    if st == TopAbs_SHELL:
        return Solid(ShapeFix_Solid().SolidFromShell(TopoDS.Shell_s(shape)))
    if st != TopAbs_COMPOUND:
        raise RuntimeError(f"no solid or shell found in shape of type {st}")

    # Walk direct children only (TopoDS_Iterator, not a recursive TopExp_Explorer),
    # recursing into nested compounds — so a solid sitting next to a stray shell/
    # face is rejected as mixed content instead of silently ignored.
    solids, shells, mixed = [], [], False
    stack = [shape]
    while stack:
        it = TopoDS_Iterator(stack.pop())
        while it.More():
            child = it.Value()
            cst = child.ShapeType()
            if cst == TopAbs_SOLID:
                solids.append(TopoDS.Solid_s(child))
            elif cst == TopAbs_COMPOUND:
                stack.append(child)
            elif cst == TopAbs_SHELL:
                shells.append(TopoDS.Shell_s(child))
            else:
                mixed = True
            it.Next()

    if mixed or (solids and shells):
        raise RuntimeError(f"mixed topology in a {st} — expected only solids or only shells")
    if len(solids) == 1:
        return Solid(solids[0])
    if len(solids) > 1:
        raise RuntimeError(
            f"expected 1 solid, found {len(solids)} in a {st} — "
            "the operation likely didn't fully merge"
        )
    if len(shells) == 1:
        return Solid(ShapeFix_Solid().SolidFromShell(shells[0]))
    if len(shells) > 1:
        raise RuntimeError(
            f"expected 1 shell, found {len(shells)} in a {st} — sewing likely split the part"
        )

    raise RuntimeError(f"no solid or shell found in shape of type {st}")
```

Never silently pick a solid/shell out of several, and never ignore other
topology sitting alongside one — a compound with more than one solid/shell, or
a mix of a solid plus a stray shell/face, almost always means the operation
didn't fully merge or a sew split the part, and picking one candidate
arbitrarily reintroduces the exact silent-partial-volume bug this helper
exists to eliminate. Sanity-check the wrapped result's volume against the
pre-heal volume every time regardless — whichever wrapper you use, a wrapping
bug or a genuinely bad heal both show up the same way (a wrong or zero
volume), so the check is what actually tells them apart.

Because build123d-mcp's `execute()` namespace persists across calls, a rung
whose attempt raises does **not** clear a previous rung's `healed` — reassign
`healed = None` before each attempt and check `healed is not None` before
trusting it, rather than assuming a bare reference reflects the rung you just
tried.

### Rung 1 — `ShapeFix_Shape` (seconds, non-destructive)

Fixes reversed faces (a boolean can flip a distant face — e.g. a hole-bottom
sphere reporting negative area) and zero-area sliver faces a boolean leaves
elsewhere on an imported shape.

```python
from OCP.ShapeFix import ShapeFix_Shape
fix = ShapeFix_Shape(part.wrapped)
fix.Perform()
healed = as_solid(fix.Shape())
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
  healed = as_solid(op.Shape())
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
healed = as_solid(df.Shape())
```

**Volume check is mandatory**: defeaturing can silently *fill an internal
bore* whose wall included the removed face (observed: +14% volume — a ruined
part that passed the gate). Accept the heal only if the volume delta is on the
scale of the defect itself, not of a feature.

Fails when: the sliver's neighbours can't extend to close the gap, the
defeature output fails the gate, or the (correctly-wrapped) healed volume
comes back zero or wildly different despite `IsDone()` reporting success —
defeaturing can silently produce a degenerate shape on a genuinely
non-planar/complex face, not just run slowly or need a longer timeout. Don't
retry with different tolerances; move to rung 4.

**If it's just slow, don't wait it out — kill it and move on.** A stubborn
face's `Build()` can run past the `execute()` timeout in-session, and running
it as a standalone script (Step 5) can *also* run for many minutes with no
output. Give a standalone attempt one bounded check (a `ps`/timeout, not open-
ended watching); a defeature that hasn't finished by then is exactly as
diagnostic as one that returned a bad result — kill the process and move to
rung 4 rather than continuing to wait. Field evidence: on the same class of
defect, sessions that killed a hung defeature within seconds and escalated
had budget left to reach a working repair; a session that let a standalone
defeature run past its timeout window before giving up did not.

### Rung 4 — face surgery for an unorientable sliver

Thin sliver faces (a degenerate fillet remnant, a band between near-coincident
arcs) are the classic BRepCheck killer on imported castings. Escalate through
these, in order:

1. **Rebuild directly from the face's own wire, if planar.** The cheapest,
   most literal fix — zero geometry change, since the boundary itself is
   untouched. Rebuild from the *outer* wire and re-add any inner wires
   explicitly — a face with a hole has more than one wire, and grabbing only
   "a" wire silently drops the others (verified: doing that on a plate with a
   drilled hole rebuilds a solid plate, `IsDone()` True, hole gone, no error):

   ```python
   from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
   from OCP.BRepTools import BRepTools_ReShape
   outer = bad_face.outer_wire()
   inner_wires = [w for w in bad_face.wires() if not w.wrapped.IsSame(outer.wrapped)]
   mk = BRepBuilderAPI_MakeFace(outer.wrapped, True)
   if not mk.IsDone():
       raise RuntimeError("MakeFace failed — non-planar wire, move to option 2")
   for w in inner_wires:
       mk.Add(w.wrapped)
   reshaper = BRepTools_ReShape()
   reshaper.Replace(bad_face.wrapped, mk.Face())
   healed = as_solid(reshaper.Apply(part.wrapped))
   ```

   Check `IsDone()` right after constructing `mk`, **before** adding any inner
   wires — calling `.Add()` on a builder that already failed doesn't raise a
   catchable exception, it segfaults the whole process (verified: reproduced
   directly on a non-planar wire, exit code 139). Only works when the wire is
   (at least close to) planar — on a genuinely non-planar/BSpline surface
   `MakeFace` raises `Standard_Failure ... NULL shape`, or `IsDone()` is False
   and the `raise` above fires before any `.Add()` call is reached. That
   failure **is** the signal to move to option 2, not a reason to retry this
   one.
2. **Fill the boundary with a new surface.** For a non-planar face, build a
   fresh surface spanning the *same* wire's edges instead of assuming
   planarity — `BRepFill_Filling`, adding every edge of the wire, handles an
   arbitrary N-edge boundary (a plain `BRepFill` ruled face only spans exactly
   2 edges — use that simpler form only when the sliver truly has just 2 long
   bounding edges). This still needs the wire itself to be intact (its edges
   form a closed loop) — only the *surface* is assumed garbage; if the wire is
   also broken, `filling.IsDone()` comes back False here too, and that's the
   signal to move to option 3, not to retry with different edge-continuity
   settings:

   ```python
   from OCP.BRepFill import BRepFill_Filling
   from OCP.GeomAbs import GeomAbs_C0
   from OCP.BRepTools import BRepTools_ReShape
   filling = BRepFill_Filling()
   for e in bad_face.edges():
       filling.Add(e.wrapped, GeomAbs_C0, True)
   filling.Build()
   if not filling.IsDone():
       raise RuntimeError("BRepFill_Filling failed — wire itself is broken, move to option 3")
   reshaper = BRepTools_ReShape()
   reshaper.Replace(bad_face.wrapped, filling.Face())
   healed = as_solid(reshaper.Apply(part.wrapped))
   ```

   `filling.IsDone()` can be True while `BRepCheck_Analyzer` still reports the
   raw filled face invalid — that's expected here; verify via a full re-sew
   (below) and the export gate, not the raw face check. A follow-up re-sew of
   every face at a small tolerance (0.005-0.05 mm) after the patch often
   closes the residual gap the fill alone leaves — start small and only widen
   the tolerance if it doesn't close:

   ```python
   from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
   sew = BRepBuilderAPI_Sewing(0.01)   # small tol — widen toward 0.05mm if needed
   for f in healed.faces():
       sew.Add(f.wrapped)
   sew.Perform()
   healed = as_solid(sew.SewedShape())
   ```

3. **Replace with a triangulated micro-face patch.** When option 2 *also*
   fails to produce a valid filled face (a genuinely uncooperative BSpline
   that resists reconstruction as one surface), stop trying to rebuild it as
   a single surface — tessellate the bad face and rebuild it as many small
   *planar* triangular faces instead. Flat triangles can't be non-planar or
   unorientable, so this sidesteps the underlying pathology entirely, at the
   cost of faceting the small patch area (a bounded, tessellation-tolerance-
   sized shape error, not the near-zero delta the earlier options give — this
   is the first option in this rung that isn't a geometry-exact repair):

   ```python
   from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace, BRepBuilderAPI_Sewing
   from OCP.BRepCheck import BRepCheck_Analyzer
   from OCP.gp import gp_Pnt
   verts, tris = bad_face.tessellate(0.1)
   tri_faces = []
   for a, b, c in tris:
       pa, pb, pc = verts[a], verts[b], verts[c]
       poly = BRepBuilderAPI_MakePolygon(gp_Pnt(pa.X, pa.Y, pa.Z), gp_Pnt(pb.X, pb.Y, pb.Z), gp_Pnt(pc.X, pc.Y, pc.Z), True)
       mk = BRepBuilderAPI_MakeFace(poly.Wire())
       if mk.IsDone():
           tri_faces.append(mk.Face())   # skip degenerate triangles silently
   healed = None
   for tol in (0.005, 0.05, 0.1, 0.25):   # start small; widen only if it doesn't close
       sew = BRepBuilderAPI_Sewing(tol)
       for f in part.faces():
           if not f.wrapped.IsSame(bad_face.wrapped):
               sew.Add(f.wrapped)
       for tf in tri_faces:
           sew.Add(tf)
       sew.Perform()
       candidate = as_solid(sew.SewedShape())
       if BRepCheck_Analyzer(candidate.wrapped).IsValid():
           healed = candidate
           break
   if healed is None:
       raise RuntimeError("triangulated patch never closed — move to option 4")
   ```

   Sweep the sew tolerance rather than committing to one value — which
   tolerance closes the gap depends on how coarse the tessellation came out,
   and is not worth predicting up front. Verify with the volume/bbox check as
   usual, but hold this option to a looser bar: a delta on the order of the
   bad face's own area times the tessellation deflection is expected and
   acceptable here, not a red flag the way it would be for options 1-2.

4. **Drop + tolerant sew.** When the wire itself is broken (options above
   fail), delete the face entirely and sew the neighbours with **tolerance
   greater than the sliver's width** (e.g. 0.6 mm for a 0.5 mm sliver), then
   `ShapeFix_Solid` to orient the shell:

   ```python
   from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
   sew = BRepBuilderAPI_Sewing(0.6)          # tol > sliver width
   for f in part.faces():
       if not f.wrapped.IsSame(bad_face.wrapped):
           sew.Add(f.wrapped)
   sew.Perform()
   healed = as_solid(sew.SewedShape())        # sewn result is a Shell; as_solid orients it
   ```

5. **Patch + small-tolerance sew.** On a *wide* sliver (~2-3 mm), drop+sew
   goes non-manifold (the big tolerance welds faces that shouldn't meet).
   Instead fill the sliver's boundary wire with a filling face and re-sew at
   small tolerance (option 2's two snippets, in order).
6. **Cut it out.** When the sliver is intrinsic to the geometry (a curved band
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

A **vertex-deflection** failure is a different mesh-only case: the offending
face is not unmeshable, it's *mispatched* — a prior repair (a sliver sew, a
tolerance-fudged patch) left its boundary topologically closed but landing a
fraction of a millimetre off its own BREP vertex, so it reads as closed to
BRepCheck and even to the open-edge count, yet a CAD scorer's own mesh sanity
check still rejects it. `locate_gate_defects()` gives the vertex's exact
coordinates. Do not drop this face — the mismatch is a patch-quality problem,
not an unmeshable one (option 4's drop-and-sew is the wrong tool here): re-patch
(rung 4 option 2 or 3) or re-sew at small tolerance (option 5) that exact face
at a tighter tolerance than whatever repair left it in this state, then
re-verify with the export gate.

A **refined untriangulated face** (`mesh_refined_untriangulated_face` from
`locate_gate_defects()`) is similarly a tolerance-sensitive face-quality problem:
the base mesh can hide it, but a finer downstream tessellation cannot. Treat the
reported face as a fragile sliver or low-quality patch. Prefer re-patching the
same boundary with rung 4 option 2/3 or re-sewing that local face at a tighter,
controlled tolerance before using destructive drop-and-sew. Then verify with the
export gate, because the failure may only appear after STEP round-trip and finer
tessellation.

---

## Step 2 — Verify every heal with the export gate

- **The export gate is authoritative.** An in-memory heal can be fake:
  `validate()` on the live shape passes, then the written-and-reimported STEP
  fails. Always `export()` to a throwaway path and read its gate verdict; on a
  huge shape `validate()`'s own mesh check can come back `mesh_check: "skipped"`
  (too large to stitch even out-of-process) with a "not verified" warning, so
  treat its PASS as a screen, not a verdict.
- **A heal must not change the geometry — except when it's designed not to.**
  Compare `volume` and bounding box before/after: for most rungs the
  acceptable delta is the scale of the defect (a sliver's near-zero volume),
  never a feature's, and a heal that gains or loses real volume replaced your
  part with a different part. Rung 4 option 1 is the deliberate exception — it
  rebuilds a face from its own existing wire, so volume/topology/bbox are
  *supposed* to come back identical; there, `show()`'s "shape was rebound but
  volume/topology/bbox unchanged" warning is expected and does not mean the
  heal failed, so judge that option by whether the export gate now passes,
  not by whether anything numeric changed. For every other rung, that same
  warning does mean the attempt changed nothing at all — a rewrap, not a
  repair — so the original defect is still there even though no error was
  raised; move to the next rung rather than trusting it.
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
- **When a feature can be built either as an addition or a removal, prefer the
  removal.** Raising/relocating a face by *unioning new material onto an
  existing boundary* runs straight into the coincident-face trap above — the
  new material must fuse exactly flush with what's already there. The same
  visible result reached by *cutting material away* instead (e.g. treating a
  face move as an internal step/ledge to deepen rather than an external boss
  to add) only has to remove material cleanly, with no flush-fuse boundary to
  get wrong. When a spec is genuinely ambiguous between an additive and a
  subtractive reading, the subtractive one is the safer default.
- **Tangencies leave open edges.** A hole tangent to a coaxial hub wall, a
  boss grazing a torus fillet — nudge the position ~0.3 mm or extend the boss
  coaxial with the adjacent straight cylinder so the intersection is clean.
- **Patterned features must fuse into one solid.** If fusing a fin/rib field
  leaves two disjoint solids, bury each feature's base 1-2 mm so it
  interpenetrates.
- After every boolean, `measure()` and confirm face counts changed and volume
  moved in the right direction — a silently-failed boolean is tomorrow's gate
  failure.
