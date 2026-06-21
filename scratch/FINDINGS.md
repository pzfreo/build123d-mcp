# Investigation: aligning `_mesh_defects` with the CADGenBench mesh gate (#281)

Status: **root cause confirmed; faithful fix deferred** (a partial reimplementation
introduces its own false +/-, which is worse than the documented status quo).

## Target (from the full 81-fixture run)

Official mesh-invalid set: **240, 250**. Everything else (incl. 205, 214, 229,
242, 247, 249) is mesh-VALID. A correct `_mesh_defects` must report >0 for
240/250 and 0 for the rest.

## What was tried (all in `topo_stitch_prototype.py`)

Topology stitch: union-find over each shared edge's `Poly_PolygonOnTriangulation`
node indices (direction chosen by shared 3D endpoint), then count undirected
edges incident to >2 triangles.

| approach | 240 | 250 | 214 | 229 | 242 | 247 | 249 | verdict |
|---|---|---|---|---|---|---|---|---|
| target (official) | >0 | >0 | 0 | 0 | 0 | 0 | 0 | |
| stitch, no cancel | 2 | 5 | 1 | 1 | 3 | 5 | 4 | over-flags 5 valid parts |
| + skip closed/seam stitches | unchanged | — | — | — | — | — | — | **not** the cause |
| + vertex-set flap cancel | **0** | 1 | 0 | 0 | 0 | 0 | 0 | clears 240 (false neg) |
| + winding-aware flap cancel | **0** | 2 | 0 | 0 | 0 | 0 | **4** | clears 240, re-flags 249 |

## Root cause

The divergence is the official mesh **assembly**, not the deflection (relative
factor 1e-3 matches) and not closed/seam *pairing* (skipping them changes
nothing). The 214 spurious edge is internal to one face with **no BREP edge on
>2 faces** — i.e. the stitch *fabricated* a non-manifold edge from a degenerate
fold, which the official cancels.

`cadgenbench.common.mesh.tessellate_shape` builds the mesh as an integrated
pipeline:
1. topology stitch (per-edge node-index union-find),
2. **periodic-seam two-polygon merge** (a seam is one face storing two polygons),
3. **degenerate-edge BREP-vertex merge** (apices/poles carry no polygon),
4. **opposite-winding flap cancellation** (`_cancel_flaps`).

Detection correctness depends on **all four** being right together. Our partial
reimplementations each match a different subset (no-cancel over-flags;
post-hoc cancellation mis-handles 240/249) because the flap cancellation
interacts with stages 2-3 we don't replicate. A faithful fix must port the whole
`tessellate_shape` assembly, not approximate it.

## Recommendation

Either port `tessellate_shape` faithfully (sizable, must be regression-clean on
all 81 outputs + the existing curved/NIST cases), or leave the current
coordinate-weld `_mesh_defects` with the limitation documented. Impact is small
(~3/81; `validate()` is a local pre-screen, not the authoritative gate), so the
faithful port is low priority relative to the generation-prompt accuracy work.
