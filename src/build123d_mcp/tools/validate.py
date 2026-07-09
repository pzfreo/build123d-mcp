"""Pre-export validity gate.

CADGenBench (and most CAD scorers) apply a hard validity gate before any
geometric scoring: a submission that is not a well-formed, watertight,
manifold solid scores ZERO regardless of how close the geometry is. The most
common ways a build123d session produces an invalid artifact are silent —
an un-fused compound, a leftover 2D sketch as the current shape, an open
shell, or a degenerate boolean result — so this gate lets the agent confirm
the artifact is solid before exporting and submitting.

The checks mirror the gate: BRepCheck well-formedness, a closed manifold, a
real solid body, and non-degenerate volume.
"""

import json
import time
from dataclasses import dataclass

_EPS = 1e-9

# Triangle-count budgets for the accurate (slow) topology-stitch mesh check.
# Above the budget the gate falls back to the fast coordinate-weld check (inline)
# or reports "skipped" (isolated — see _EXACT_ISOLATED_MAX_TRIS below).
#
# _EXACT_INLINE_MAX_TRIS bounds the check when it runs IN-WORKER under a soft time
# deadline (_GATE_MESH_BUDGET_S) that cannot interrupt the underlying native
# BRepMesh call — so it must stay small enough that even the worst case can't
# approach the worker op-timeout. Used for interactive validate() on a small shape,
# and for STL-only export (no STEP artifact to hand an isolated subprocess).
_EXACT_INLINE_MAX_TRIS = 10000
# _EXACT_EXPORT_MAX_TRIS: legacy name for the same in-worker ceiling, historically
# also applied to the isolated path before #381 gave it its own (much higher)
# budget below. Kept only for the in-process STL-only export call site.
_EXACT_EXPORT_MAX_TRIS = 80000
# _EXACT_ISOLATED_MAX_TRIS bounds the SAME check when it runs in the hard-bounded
# subprocess (_gate_subprocess.py, deadline=inf) that export() and a large-shape
# validate() use (#360/#381): there the real worker-safety backstop is the parent's
# subprocess.run(timeout=...), not this triangle count, so it can be far more
# generous. Measured directly (not the old ~0.3ms/triangle guess, which was ~2x
# pessimistic): a genuinely large synthetic part needing the full open-edge ladder
# (the worst case) took 68s at 529k triangles. 300k keeps worst-case comfortably
# under the default ~100s subprocess budget (op_budget=120s minus margins), with
# room for real-world topology to cost more per-triangle than this synthetic case.
_EXACT_ISOLATED_MAX_TRIS = 300_000

# The open-edge deflection ladder refines a SUSPECT part (base mesh shows open
# edges) up to base/32 to distinguish a valid periodic/curved seam — which only
# reads as closed at a finer tessellation — from a genuine gap. The finest rung
# is inherently denser than the base mesh, so the ladder is bounded by its own
# (larger) triangle ceiling rather than the non-manifold-check budget; below the
# ceiling a rung's verdict is trusted, above it the rung is skipped. The ladder
# only runs when the base pass already found open edges, so a clean part never
# pays for it.
_OPEN_LADDER_MAX_TRIS = 400000

# A clean base-deflection mesh can still hide tolerance-sensitive faces that fail
# to tessellate when a downstream consumer asks for a finer mesh. Run one cheap
# refined pass whose only job is "does every face still have a triangulation?".
# This does not build or stitch a Python mesh, but it still asks OCC to refine the
# shape, so keep a deterministic triangle ceiling for the in-worker path.
_REFINED_UNTRIANGULATED_FACTOR = 4
_REFINED_UNTRIANGULATED_MAX_TRIS = 400000

# Wall-clock budget for the WHOLE mesh analysis — the exact (stitch + ladder)
# check AND the fast fallback share one deadline, so their cost is not additive.
# The OCC tessellation/stitch is pure-Python O(triangles); without this a very
# large part could run the gate past the worker op-timeout, which KILLS the
# worker and loses the session (far worse than a missed defect). Kept comfortably
# under the minimum export op budget (60s, minus the STEP re-import) so the gate
# can never approach the timeout regardless of --exec-timeout. When the budget
# (or a triangle ceiling) is hit, the mesh check returns "undetermined" and the
# gate relies on the (cheap) B-rep checks. This degrades to a possibly-missed
# mesh defect on a huge part, NEVER to a false FAIL of a valid part.
_GATE_MESH_BUDGET_S = 35.0

# Headroom under the op budget when validate() runs the mesh gate out-of-process for a
# large shape: covers the in-worker B-rep checks that already ran + subprocess teardown,
# so the child is killed before the parent op-watchdog SIGKILLs the worker. Matches
# export()'s _EXPORT_MESH_MARGIN_S / _EXPORT_MESH_MIN_S — the same gate, same bound.
_MESH_GATE_MARGIN_S = 15.0
_MESH_GATE_MIN_S = 10.0

# If the BASE mesh already has more triangles than this AND shows open edges, the
# part is too complex to refine through the finer ladder rungs within the gate's
# budget — the finer rungs' (un-interruptible) BRepMesh calls alone would blow it.
# Such a part is deferred to the fast check (UNDETERMINED), never failed. A genuine
# small/moderate open part still ladders and is caught; only large parts degrade.
_LADDER_BASE_MAX_TRIS = 40000


@dataclass(frozen=True)
class MeshGateResult:
    """Structured result from the exact mesh validity gate."""

    nonmanifold_edges: int = 0
    open_edges: int = 0
    untriangulated_faces: int = 0
    refined_untriangulated_faces: int = 0
    nonmanifold_vertices: int = 0
    vertex_deflection_defects: int = 0
    ok: bool = False
    refined_verified: bool = False

    @classmethod
    def unchecked(cls) -> "MeshGateResult":
        return cls(ok=False, refined_verified=False)

    @classmethod
    def from_json(cls, data: dict) -> "MeshGateResult":
        return cls(
            nonmanifold_edges=int(data["nm"]),
            open_edges=int(data["open"]),
            untriangulated_faces=int(data["untri"]),
            refined_untriangulated_faces=int(data.get("refined_untri", 0)),
            nonmanifold_vertices=int(data.get("nmv", 0)),
            vertex_deflection_defects=int(data.get("vdefl", 0)),
            ok=bool(data["ok"]),
            refined_verified=bool(data.get("refined_verified", True)),
        )

    def to_json(self) -> dict:
        return {
            "nm": self.nonmanifold_edges,
            "open": self.open_edges,
            "untri": self.untriangulated_faces,
            "refined_untri": self.refined_untriangulated_faces,
            "nmv": self.nonmanifold_vertices,
            "vdefl": self.vertex_deflection_defects,
            "ok": self.ok,
            "refined_verified": self.refined_verified,
        }


def _edge_incidence_counts(mf, n_nodes: int):
    """Per-undirected-edge triangle-incidence counts for a merged triangle array.

    ``mf`` is an (M, 3) int array of triangles over ``n_nodes`` merged nodes.
    Returns the incidence count of each distinct undirected edge: a clean closed
    orientable 2-manifold has every edge incident to exactly 2 triangles, so
    ``(counts == 1)`` are open (boundary) edges and ``(counts > 2)`` are
    non-manifold edges. Shared so the open-edge and non-manifold counts use one
    tested code path.
    """
    import numpy as np

    e = np.sort(mf[:, [0, 1, 1, 2, 0, 2]].reshape(-1, 2), axis=1)
    keys = e[:, 0].astype(np.int64) * (n_nodes + 1) + e[:, 1]
    _, counts = np.unique(keys, return_counts=True)
    return counts


def _nonmanifold_vertex_count(mf) -> int:
    """Count non-manifold VERTICES in a merged triangle array.

    A non-manifold vertex is one where two or more surface sheets meet at a single
    point (e.g. two bodies touching corner-to-corner): the boundary is still
    edge-manifold and watertight, but it is not a 2-manifold surface, which a CAD
    scorer rejects (#298). Edge-incidence counts cannot see it — it is purely a
    vertex-link property.

    ``mf`` is an (M, 3) int array of triangles over coordinate-WELDED nodes (so
    seams/poles are already merged; the index-stitch under-merges seams and would
    false-positive here). For each vertex, the 'opposite edge' (a, b) of every
    incident triangle links its two spokes; a manifold vertex's incident triangles
    form a single connected fan (one component), a pinch forms two or more.
    """
    from collections import defaultdict

    spokes: dict = defaultdict(list)
    for a, b, c in mf.tolist():
        spokes[a].append((b, c))
        spokes[b].append((a, c))
        spokes[c].append((a, b))
    nmv = 0
    for edges in spokes.values():
        par: dict = {}

        def root(x: int, par: dict = par) -> int:
            par.setdefault(x, x)
            r = x
            while par[r] != r:
                r = par[r]
            while par[x] != r:
                par[x], x = r, par[x]
            return r

        for a, b in edges:
            ra, rb = root(a), root(b)
            if ra != rb:
                par[ra] = rb
        if len({root(x) for x in par}) > 1:
            nmv += 1
    return nmv


def _edge_face_adjacency(occ, fmap, emap):
    """Map each edge index (in ``emap``) to the list of incident face indices.

    Equivalent to ``MapShapesAndAncestors_s(EDGE, FACE)`` + iterating each edge's
    ancestor list, but built by walking faces' edges with ``TopExp_Explorer``
    instead. The ancestor-list path returns a ``TopTools_ListOfShape`` whose
    Python-side iteration is pathologically slow (tens of seconds for a few
    thousand edges on a large B-rep — the dominant cost of the stitch); explorer
    traversal builds the identical adjacency at C speed. Face order within a
    list is irrelevant: the merge that consumes it is symmetric union, so the
    connected components — and thus the verdict — are unchanged.
    """
    from collections import defaultdict

    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    adj: dict = defaultdict(list)
    for fi in range(1, fmap.Size() + 1):
        f = TopoDS.Face_s(fmap.FindKey(fi))
        exp = TopExp_Explorer(f, TopAbs_EDGE)
        while exp.More():
            adj[emap.FindIndex(exp.Current())].append(fi)
            exp.Next()
    return adj


def _run_mesh_gate_subprocess(step_path: str, timeout: float) -> MeshGateResult | None:
    """Run the exact mesh check on a written STEP in a separate process, hard-
    bounded by ``timeout`` seconds (the only way to bound the un-interruptible OCC
    tessellation without risking the worker). Returns ``MeshGateResult`` or
    ``None`` if the subprocess timed out (was killed), errored, or its result could
    not be parsed — ``None`` means UNDETERMINED, so the caller keeps its safe
    in-process verdict rather than inventing one.
    """
    import json
    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "build123d_mcp._gate_subprocess", step_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("GATE_RESULT:"):
            try:
                d = json.loads(line[len("GATE_RESULT:") :])
            except ValueError:
                return None
            if "error" in d:
                return None
            return MeshGateResult.from_json(d)
    return None


def _gate_report(shape, exact: bool = False, mesh_override: MeshGateResult | None = None) -> dict:
    """Return the validity-gate verdict for a shape as a plain dict.

    Reused by the export tool so a 3D export can warn when the written solid
    would fail the gate.

    ``exact`` selects the mesh non-manifold check: the default fast coordinate-weld
    (_mesh_defects, sub-second, the in-worker validate() fallback) or the accurate
    topology-stitch (_mesh_defects_exact, slower, used at export and by validate()'s
    out-of-process mesh gate where shipping an invalid solid actually costs).

    The fast fallback is **structurally blind to open edges** — it only counts
    non-manifold edges and pins ``mesh_open_edges`` to 0 (#381). So a ``mesh_check ==
    "fast"`` verdict can never be trusted for the open-edge / non-closure class; the
    report says so in ``warnings``, and the out-of-process exact check (``export()`` and
    a large shape's ``validate()``) is the authority. ``mesh_override`` feeds that exact
    result in; an ``ok=False`` override (or a huge in-worker shape) reports ``"skipped"``.
    """
    from OCP.BRepCheck import BRepCheck_Analyzer

    try:
        n_solids = len(shape.solids())
    except Exception:
        n_solids = 0
    try:
        volume = round(float(shape.volume), 4)
    except Exception:
        volume = 0.0
    # build123d's `is_manifold` false-negates on closed solids imported from STEP
    # (verified on NIST CAD models — a single closed shell with zero open edges
    # still reports is_manifold=False), so it is NOT a reliable gate. The
    # authoritative test is the edge-face map: a watertight, manifold solid has
    # every non-degenerate edge shared by exactly two faces. An open shell leaves
    # boundary edges with one face; a non-manifold junction leaves an edge with
    # three or more. Reported separately as `open_edges` / `nonmanifold_edges`.
    open_edges, nonmanifold_edges, bad_edges_ok = _edge_defects(shape)
    watertight_manifold = bad_edges_ok and open_edges == 0 and nonmanifold_edges == 0
    try:
        brep_valid = bool(BRepCheck_Analyzer(shape.wrapped).IsValid())
    except Exception:
        brep_valid = False
    # Mesh-level non-manifold check, mirroring how a CAD scorer validates
    # (tessellate → check the mesh is manifold). Catches self-touching /
    # coincident-face defects that are valid B-reps the edge-face map above does
    # not see — the dominant invalid-but-watertight failure mode observed on
    # CADGenBench (a single solid whose mesh has an edge shared by 4 triangles).
    # Prefer the accurate topology-stitch, bounded by triangle count (generous at
    # export, small inline); above the budget — or if the exact build fails — fall
    # back to the fast coordinate-weld check. mesh_check records which ran.
    # The exact stitch+ladder and the fast fallback SHARE one wall-clock deadline,
    # so the whole mesh analysis is bounded (not additive) and can never approach
    # the worker op-timeout. If both are over budget, the mesh check is skipped and
    # the gate relies on the B-rep checks above (never a false FAIL).
    if mesh_override is not None:
        # Mesh results computed out-of-process (export's subprocess retry for a
        # part too large to mesh within the in-process budget). Bounded by a hard
        # subprocess timeout there, so it can run the full check without skipping.
        mesh = mesh_override
        # ok=False means the out-of-process check timed out / couldn't determine —
        # mark it "skipped" so the "mesh validity not verified" warning fires and the
        # caller doesn't report false confidence on an unchecked part.
        mesh_check = "exact-subprocess" if mesh.ok else "skipped"
    else:
        _cap = _EXACT_EXPORT_MAX_TRIS if exact else _EXACT_INLINE_MAX_TRIS
        _mesh_deadline = time.monotonic() + _GATE_MESH_BUDGET_S
        mesh = _mesh_defects_exact(shape, max_triangles=_cap, deadline=_mesh_deadline)
        mesh_check = "exact"
        if not mesh.ok:
            # Exact check over budget / unbuildable — fall back to the fast
            # non-manifold-only check (no open-edge / face-tessellation analysis),
            # under the SAME deadline. This in-worker fallback is structurally blind to
            # open edges (mesh_open_edges stays 0), so the report warns (#381); a large
            # shape avoids it entirely by running the exact check out-of-process.
            mesh_nm_edges, mesh_fast_ok = _mesh_defects(shape, deadline=_mesh_deadline)
            mesh = MeshGateResult(
                nonmanifold_edges=mesh_nm_edges if mesh_fast_ok else 0,
                ok=mesh_fast_ok,
                refined_verified=False,
            )
            mesh_check = "fast" if mesh.ok else "skipped"
    mesh_nm_edges = mesh.nonmanifold_edges
    mesh_open_edges = mesh.open_edges
    mesh_untri_faces = mesh.untriangulated_faces
    mesh_refined_untri_faces = mesh.refined_untriangulated_faces
    mesh_nmv = mesh.nonmanifold_vertices
    mesh_vdefl = mesh.vertex_deflection_defects
    mesh_ok = mesh.ok
    mesh_refined_verified = mesh.refined_verified
    mesh_nonmanifold = mesh_ok and mesh_nm_edges > 0
    mesh_open = mesh_ok and mesh_open_edges > 0
    mesh_incomplete = mesh_ok and mesh_untri_faces > 0
    mesh_refined_incomplete = mesh_ok and mesh_refined_untri_faces > 0
    mesh_nmv_flag = mesh_ok and mesh_nmv > 0
    mesh_vdefl_flag = mesh_ok and mesh_vdefl > 0

    reasons: list[str] = []
    if not brep_valid:
        reasons.append("B-rep is not well-formed (BRepCheck failed)")
    if volume <= _EPS:
        reasons.append("zero/degenerate volume")
    if open_edges:
        reasons.append(f"{open_edges} open edge(s) — not watertight (open shell or unsewn faces)")
    if nonmanifold_edges:
        reasons.append(f"{nonmanifold_edges} non-manifold edge(s) — edges shared by 3+ faces")
    if mesh_nm_edges:
        reasons.append(
            f"{mesh_nm_edges} mesh non-manifold edge(s) — faces meet >2-ways "
            "(self-touch / coincident faces); a CAD scorer rejects this even though "
            "it looks watertight"
        )
    if mesh_untri_faces:
        reasons.append(
            f"{mesh_untri_faces} face(s) failed to tessellate — the exported boundary "
            "is incomplete (un-meshable or degenerate face)"
        )
    if mesh_refined_untri_faces:
        reasons.append(
            f"{mesh_refined_untri_faces} face(s) failed to tessellate at a finer mesh "
            "deflection — the B-rep is tolerance-sensitive and may be rejected by "
            "downstream CAD consumers"
        )
    if mesh_open_edges:
        reasons.append(
            f"{mesh_open_edges} mesh open edge(s) — the tessellated boundary is not "
            "closed (non-conformal face junction or unsewn faces) even though the "
            "B-rep edges look matched"
        )
    if mesh_nmv:
        reasons.append(
            f"{mesh_nmv} mesh non-manifold vertex/vertices — ≥2 surface sheets meet at a "
            "single point (e.g. bodies touching corner-to-corner); edge-manifold and "
            "watertight but not a 2-manifold surface, which a CAD scorer rejects"
        )
    if mesh_vdefl:
        reasons.append(
            f"{mesh_vdefl} vertex(es) where a tessellated edge endpoint misses its BREP "
            "vertex by more than the mesh deflection — the boundary looks closed by "
            "coordinate proximity but isn't conformal there (a patched/healed face whose "
            "polygon endpoint is genuinely off-vertex); a CAD scorer's own mesh sanity "
            "check rejects this even though it BRepCheck-validates"
        )
    if n_solids == 0 and not open_edges and not nonmanifold_edges:
        reasons.append("closed surface but no solid body — wrap the faces in Solid() before export")
    elif n_solids == 0:
        reasons.append("no solid body — the current shape is 2D/open geometry, not a solid")

    # Non-fatal advisories: things that pass the watertight-manifold gate but
    # still hurt the geometric score. Disjoint solids are each watertight, so a
    # mesh scorer accepts them — but a single-part task expects ONE body, and the
    # extra components tank the topology (component-count) score. Almost always an
    # un-fused result. (Intentional assembly exports via '*' will see this too.)
    warnings: list[str] = []
    if n_solids > 1:
        warnings.append(
            f"{n_solids} disjoint solid bodies — a single-part task expects one fused "
            "solid; fuse them (Part() + ... or a.fuse(b)) or the topology score suffers"
        )
    if mesh_check == "skipped":
        warnings.append(
            "mesh-level validity not verified — this shape is too large to tessellate "
            "and stitch within the gate's time budget, so only the B-rep checks ran; a "
            "mesh non-manifold / non-closure / refined-tessellation defect (if any) "
            "would not be caught here"
        )
    elif mesh_check == "fast":
        # The fast coordinate-weld fallback checked non-manifold edges but is
        # structurally blind to mesh open edges / non-closure (mesh_open_edges is
        # pinned to 0, not measured) — so this PASS is NOT authoritative for the
        # open-edge class. Say so, or the agent trusts a verdict the fast check
        # never made (#381). export() runs the exact check out-of-process.
        warnings.append(
            "mesh open-edge / non-closure / refined face-tessellation NOT verified — "
            "this shape was too large for "
            "the exact stitch in-loop, so the fast fallback ran; it catches mesh "
            "non-manifold edges but CANNOT see open edges (unclosed boundary) or faces "
            "that only fail at a finer mesh. export() runs the authoritative check — "
            "test-export before trusting this PASS"
        )
    elif mesh_ok and not mesh_refined_verified:
        warnings.append(
            "refined face-tessellation not verified — the exact mesh gate found other "
            "defects, then the finer face-tessellation probe ran out of budget before "
            "proving whether additional tolerance-sensitive faces exist"
        )

    passes = (
        brep_valid
        and watertight_manifold
        and not mesh_nonmanifold
        and not mesh_open
        and not mesh_incomplete
        and not mesh_refined_incomplete
        and not mesh_nmv_flag
        and not mesh_vdefl_flag
        and volume > _EPS
        and n_solids >= 1
    )
    return {
        "passes_gate": passes,
        "n_solids": n_solids,
        "volume": volume,
        "watertight_manifold": watertight_manifold,
        "open_edges": open_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "mesh_nonmanifold_edges": mesh_nm_edges,
        "mesh_nonmanifold_vertices": mesh_nmv,
        "mesh_open_edges": mesh_open_edges,
        "mesh_vertex_deflection_defects": mesh_vdefl,
        "untriangulated_faces": mesh_untri_faces,
        "refined_untriangulated_faces": mesh_refined_untri_faces,
        "refined_untriangulated_faces_verified": mesh_refined_verified,
        "mesh_check": mesh_check,
        "brep_valid": brep_valid,
        "warnings": warnings,
        "reasons": reasons,
    }


def _edge_defects(shape) -> tuple[int, int, bool]:
    """Count open and non-manifold edges via the edge→face map.

    A watertight, manifold B-rep has every non-degenerate (non-seam) edge shared
    by exactly two faces. Returns (open_edges, nonmanifold_edges, ok) where
    ``ok`` is False if the map could not be built (then the caller treats the
    shape as failing rather than silently passing).

    Edges with NO incident face are free wires — annotation/PMI curves (leader
    and dimension lines carried by an imported STEP) or stray construction
    geometry — not shell boundaries, so they are skipped. Counting them as open
    edges false-FAILed clean solids imported from PMI-annotated STEP (verified on
    the NIST CAD models, where the solid is watertight but the file carries dozens
    of free annotation edges). Only a one-face edge is a genuine open boundary.
    """
    try:
        from OCP.BRep import BRep_Tool
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
        from OCP.TopExp import TopExp
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

        m = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, m)
        open_edges = nonmanifold_edges = 0
        for i in range(1, m.Extent() + 1):
            edge = TopoDS.Edge_s(m.FindKey(i))
            if BRep_Tool.Degenerated_s(edge):  # seam/pole edges are not boundaries
                continue
            faces = m.FindFromIndex(i).Extent()
            if faces == 0:
                continue  # free wire / PMI annotation curve, not a shell boundary
            if faces == 1:
                open_edges += 1
            elif faces > 2:
                nonmanifold_edges += 1
        return open_edges, nonmanifold_edges, True
    except Exception:
        return 0, 0, False


def _mesh_defects(shape, deadline: float | None = None) -> tuple[int, bool]:
    """Tessellate and count mesh-level non-manifold edges (shared by >2 triangles).

    Mirrors how a CAD scorer validates (B-rep → mesh → manifold check), catching
    self-touching / coincident-face defects that pass BRepCheck and the edge-face
    map (a single watertight solid whose mesh has an edge shared by 4 triangles).
    OCP meshes each face independently, so coincident vertices are welded by
    rounded coordinate before counting; verified to give zero false positives on
    curved and real CAD geometry (incl. a 199k-edge NIST model). Returns
    (nonmanifold_edges, ok).

    Deliberately edge-only: a tessellation-based non-manifold *vertex* (pinch
    point) test is too easily tripped into false positives by sliver/degenerate
    triangles and per-face sampling on curved surfaces, and a gate that rejects
    valid geometry is worse than one that misses a rare case. The >2-incidence
    edge count is robust because per-face sampling noise produces 1-incidence
    edges, never >2.
    """
    try:
        import math
        from collections import Counter

        # Check the shared deadline BEFORE tessellating: shape.tessellate() is an
        # un-interruptible OCC call (tens of seconds on a large part), so if the
        # exact attempt already spent the budget, bail here rather than blow it.
        if deadline is not None and time.monotonic() > deadline:
            return 0, False

        bb = shape.bounding_box()
        diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        if diag <= 0:
            return 0, False
        verts, tris = shape.tessellate(max(diag * 1e-3, 1e-4))
        if not verts or not tris:
            return 0, False

        # Pure-Python welds/counts below are O(verts+tris); on a very large part
        # they can run long, so honour the shared gate deadline and bail (ok=False)
        # rather than let the fast fallback push the gate past the worker timeout.
        if deadline is not None and time.monotonic() > deadline:
            return 0, False

        q = diag * 1e-5  # weld tolerance: merge per-face samples on shared edges
        remap: list[int] = []
        keys: dict = {}
        for v in verts:
            k = (round(v.X / q), round(v.Y / q), round(v.Z / q))
            remap.append(keys.setdefault(k, len(keys)))

        if deadline is not None and time.monotonic() > deadline:
            return 0, False

        edge_count: Counter = Counter()
        for t in tris:
            a, b, c = remap[t[0]], remap[t[1]], remap[t[2]]
            if len({a, b, c}) < 3:
                continue  # degenerate triangle after welding
            for e in ((a, b), (b, c), (a, c)):
                edge_count[tuple(sorted(e))] += 1

        return sum(1 for n in edge_count.values() if n > 2), True
    except Exception:
        return 0, False


def _mesh_defects_exact(
    shape, max_triangles: int | None = None, deadline: float | None = None
) -> MeshGateResult:
    """Accurate mesh non-manifold count via a topology-stitched tessellation.

    Builds one conformal boundary mesh from the per-face OCC triangulations by
    TOPOLOGY rather than by coordinate proximity: each shared edge's
    PolygonOnTriangulation gives equal-length node-index lists in the two
    adjacent faces, merged by index (union-find); nodes resolving to the same
    BREP vertex are merged too (closing fillet/cone/pole apices). Winding is made
    globally consistent first (REVERSED faces flipped), then opposite-winding
    flap pairs from degenerate folds are cancelled. Finally counts undirected
    edges shared by >2 triangles.

    Being tolerance-free, it has neither the false positives nor the false
    negatives that ``_mesh_defects``'s coordinate weld produces at the rounding
    boundary (see #281) — it matches the mesh gate a CAD scorer applies. It is
    slower (per-edge OCC introspection: ~0.5-2s typical, more on large imported
    B-reps), so callers use it where correctness matters most (export) rather
    than on every interactive validate(). ``max_triangles`` bounds that cost: if
    the tessellation exceeds it, return ok=False *before* the slow stitch so the
    caller can fall back to the fast check. Returns ``MeshGateResult``; ``ok=False``
    if the mesh could not be built or was over budget (caller then falls back to the
    fast check). ``refined_verified=False`` means the base mesh findings may be
    usable but the finer face-tessellation probe did not complete. ``open_edges>0``
    means the tessellated boundary is not closed (edges incident to a single
    triangle); ``untriangulated_faces>0`` means a face failed to mesh at the base
    deflection, leaving the boundary incomplete. ``refined_untriangulated_faces>0``
    means the base mesh succeeded but a lightweight finer pass (base/4) exposed
    faces that downstream consumers using tighter tolerances may reject.

    ``vertex_deflection_defects`` guards the BREP-vertex merge below: a node
    claiming to sit at a given BREP vertex (an edge polygon's own first/last
    entry) is only unioned into that vertex if it is actually within
    ``deflection`` of the vertex's analytic position. Unioning unconditionally
    (as this function did before) silently welds a genuinely-off-vertex node
    into place instead of reporting it — a patched/healed face whose polygon
    endpoint misses its vertex by a fraction of a millimetre reads as perfectly
    closed here even though the same shape fails CADGenBench's own mesh sanity
    check, which performs exactly this guard and raises on it. Mirrors that
    check (``cadgenbench.common.mesh``'s vertex-merge guard) so a shape this
    gate passes actually passes there too. The SAME guard also runs inside the
    open-edge ladder's own, independent vertex-merge (``_open_pass``, below) —
    a defect too small to trip the single check here at the base deflection
    can still be exposed once the ladder escalates to a finer rung while
    chasing an unrelated open-edge gap, and without the guard there too that
    escalation would silently weld it shut instead. The union itself still
    always proceeds either way (this guard's job is to report, not repair),
    so ``mesh_open_edges`` on its own can legitimately read 0 for a shape that
    still fails via ``vertex_deflection_defects`` — that is not a contradiction,
    it means the boundary stitches closed but isn't conformal at that vertex.

    The open-edge (closedness) verdict is computed by a separate seam-aware
    conformal stitch run over a DEFLECTION LADDER: a part is closed iff ANY rung
    (base, base/4, base/16, base/32) yields zero open edges. Coarser-then-finer
    tessellation is needed because a single deflection can leave a valid
    periodic/curved face with a non-conformal seam that only closes at a finer
    sampling; conversely a genuine gap stays open at every rung. The non-manifold
    and base untriangulated counts come from the base-deflection index-stitch
    below. The refined untriangulated-face count is a separate, unconditional
    base/4 probe that runs after the base pass so OCC's sticky triangulation cache
    cannot inflate the base triangle-count/budget decisions.
    """
    try:
        import math
        from collections import defaultdict

        import numpy as np
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRepTools import BRepTools
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_VERTEX
        from OCP.TopExp import TopExp, TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import TopTools_IndexedMapOfShape

        bb = shape.bounding_box()
        diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        if diag <= 0:
            return MeshGateResult.unchecked()
        # Deflection relative to part scale, clamped — matches the scorer.
        deflection = min(0.5, max(0.005, diag * 1e-3))
        _open_deadline = (
            deadline if deadline is not None else time.monotonic() + _GATE_MESH_BUDGET_S
        )
        occ = shape.wrapped

        # --- open-edge (closedness) via seam-aware conformal stitch ladder ---
        # One pass at a given deflection -> (open_edges, n_triangles). Builds a
        # conformal boundary mesh from the per-face OCC triangulations by topology
        # (inter-face shared edges, periodic seam edges, BREP-vertex endpoints)
        # with a coordinate-weld backstop, then counts undirected edges incident
        # to a single triangle. Tolerance-free per stitch; coordinate weld is only
        # a tiny-fraction-of-diagonal backstop so distinct surfaces never merge.
        # With no time deadline (the export subprocess path, bounded instead by a
        # hard subprocess kill) the finest rung is affordable now that the stitch
        # is fast, so lift the triangle ceiling and let the /32 rung actually run —
        # this is what lets a large part's defect be detected. The in-process path
        # keeps the ceiling (its un-interruptible BRepMesh calls must stay bounded).
        _ladder_ceil = float("inf") if _open_deadline == float("inf") else _OPEN_LADDER_MAX_TRIS

        def _open_pass(defl: float) -> tuple[int, int, set]:
            BRepMesh_IncrementalMesh(occ, defl, False, 0.5, True)
            fmap = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(occ, TopAbs_FACE, fmap)
            V: list = []
            T: list = []
            fbase: dict = {}
            ftri: dict = {}
            for fi in range(1, fmap.Size() + 1):
                f = TopoDS.Face_s(fmap.FindKey(fi))
                loc = TopLoc_Location()
                tri = BRep_Tool.Triangulation_s(f, loc)
                if tri is None:
                    continue
                trsf = loc.Transformation()
                base = len(V)
                fbase[fi] = base
                ftri[fi] = (tri, loc, f)
                for i in range(1, tri.NbNodes() + 1):
                    p = tri.Node(i).Transformed(trsf)
                    V.append((p.X(), p.Y(), p.Z()))
                rev = f.Orientation() == TopAbs_REVERSED
                for i in range(1, tri.NbTriangles() + 1):
                    a, b, c = tri.Triangle(i).Get()
                    a, b, c = base + a - 1, base + b - 1, base + c - 1
                    if rev:
                        a, b = b, a
                    T.append((a, b, c))
                # Bail mid-build as soon as the soup blows the ceiling or the
                # budget — before paying the rest of the O(triangles) append +
                # the stitch — so a single huge rung can't run the gate long.
                if len(T) > _ladder_ceil or time.monotonic() > _open_deadline:
                    return -1, len(T), set()
            if not T:
                return 0, 0, set()
            Va = np.asarray(V, dtype=np.float64)
            Ta = np.asarray(T, dtype=np.int64)
            if Ta.shape[0] > _ladder_ceil or time.monotonic() > _open_deadline:
                # Too large / out of time to stitch within the gate's budget.
                # Signal UNDETERMINED (-1) BEFORE paying the O(triangles) stitch,
                # so the ladder defers to the fast check rather than risk the
                # worker op-timeout (session loss) or a wrong verdict.
                return -1, int(Ta.shape[0]), set()
            par = list(range(len(V)))

            def fnd(x: int) -> int:
                r = x
                while par[r] != r:
                    r = par[r]
                while par[x] != r:
                    par[x], x = r, par[x]
                return r

            def uni(a: int, b: int) -> None:
                ra, rb = fnd(a), fnd(b)
                if ra != rb:
                    par[max(ra, rb)] = min(ra, rb)

            def stitch(nls: list) -> None:
                if len(nls) < 2:
                    return
                ref = nls[0]
                rp = Va[ref]
                for o in nls[1:]:
                    if o.shape[0] != ref.shape[0]:
                        continue
                    op = Va[o]
                    seq = o if abs(rp - op).max() <= abs(rp - op[::-1]).max() else o[::-1]
                    for u, v in zip(ref.tolist(), seq.tolist()):
                        uni(u, v)

            # (a) inter-face shared edges — and (c) BREP-vertex endpoints, both
            # driven by the same per-(edge,face) PolygonOnTriangulation. That OCC
            # call dominates the pass, so extract each polygon ONCE here and reuse
            # its endpoints for the vertex merge below rather than re-walking every
            # edge a second time.
            emap = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(occ, TopAbs_EDGE, emap)
            eadj = _edge_face_adjacency(occ, fmap, emap)
            vm = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(occ, TopAbs_VERTEX, vm)
            vnodes: dict = defaultdict(list)
            for ei in range(1, emap.Size() + 1):
                if time.monotonic() > _open_deadline:
                    return -1, len(T), set()  # stitch over budget — undetermined
                edge = TopoDS.Edge_s(emap.FindKey(ei))
                nls = []
                for fi in eadj.get(ei, ()):
                    if fi not in ftri:
                        continue
                    tri, loc, _ = ftri[fi]
                    poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
                    if poly is None:
                        continue
                    nls.append(
                        np.fromiter(poly.Nodes(), dtype=np.int64, count=poly.NbNodes())
                        + (fbase[fi] - 1)
                    )
                stitch(nls)
                if nls:
                    vf = vm.FindIndex(TopExp.FirstVertex_s(edge))
                    vl = vm.FindIndex(TopExp.LastVertex_s(edge))
                    for arr in nls:
                        if vf:
                            vnodes[vf].append(int(arr[0]))
                        if vl:
                            vnodes[vl].append(int(arr[-1]))
            # (b) periodic SEAM edges (edge appears twice on the same face)
            for fi, (tri, loc, f) in ftri.items():
                smap = TopTools_IndexedMapOfShape()
                polys: dict = defaultdict(list)
                exp = TopExp_Explorer(f, TopAbs_EDGE)
                while exp.More():
                    e = TopoDS.Edge_s(exp.Current())
                    if BRepTools.IsReallyClosed_s(e, f):
                        idx = smap.Add(e)
                        poly = BRep_Tool.PolygonOnTriangulation_s(e, tri, loc)
                        if poly is not None:
                            polys[idx].append(
                                np.fromiter(poly.Nodes(), dtype=np.int64, count=poly.NbNodes())
                                + (fbase[fi] - 1)
                            )
                    exp.Next()
                for idx, lists in polys.items():
                    seen_seam: set = set()
                    u: list = []
                    for a in lists:
                        kk = a.tobytes()
                        if kk not in seen_seam:
                            seen_seam.add(kk)
                            u.append(a)
                    if len(u) >= 2:
                        stitch(u)
            # (c) BREP-vertex merge — close fillet/cone/pole apices where the
            # endpoints of the edges meeting at a B-rep vertex map to distinct
            # tessellation nodes. Endpoints were collected in the loop above.
            # Same guard as the base pass below (#397): a candidate node is only
            # trustworthy within `defl` of the vertex's analytic position — union
            # anyway (this pass's job is closedness, not repair) but record its
            # world-space position (not just a boolean) so the caller can union
            # it BY IDENTITY against the base pass's own findings — a boolean
            # here would silently cap the total defect count at +1 regardless
            # of how many distinct vertices only this ladder rung catches. Time-
            # budgeted like the edge-adjacency loop above it (this walk is over
            # distinct BREP vertices, typically far fewer than edges, but a
            # pathological vertex count on a huge part shouldn't run unbounded
            # within a rung the caller has already committed wall-clock to).
            pass_vdefl: set = set()
            for vi, ns in vnodes.items():
                if time.monotonic() > _open_deadline:
                    return -1, len(T), pass_vdefl  # stitch over budget — undetermined
                vp = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vm.FindKey(vi)))
                vertex_xyz = np.array([vp.X(), vp.Y(), vp.Z()], dtype=np.float64)
                if float(np.abs(Va[ns] - vertex_xyz).max()) > defl:
                    pass_vdefl.add((round(vp.X(), 6), round(vp.Y(), 6), round(vp.Z(), 6)))
                for n in ns[1:]:
                    uni(ns[0], n)
            # (d) coordinate-weld backstop
            wdiag = float(np.linalg.norm(Va.max(0) - Va.min(0)))
            wtol = max(1e-7, 1e-7 * wdiag)
            q = np.round(Va / wtol).astype(np.int64)
            _, cinv = np.unique(q, axis=0, return_inverse=True)
            cinv = np.asarray(cinv).ravel()
            seen_c: dict = {}
            for i, r in enumerate(cinv.tolist()):
                if r in seen_c:
                    uni(seen_c[r], i)
                else:
                    seen_c[r] = i
            roots = np.array([fnd(i) for i in range(len(V))], dtype=np.int64)
            _, inv = np.unique(roots, return_inverse=True)
            inv = np.asarray(inv).ravel()
            mfo = inv[Ta]
            mfo = mfo[
                (mfo[:, 0] != mfo[:, 1]) & (mfo[:, 1] != mfo[:, 2]) & (mfo[:, 0] != mfo[:, 2])
            ]
            nn = int(inv.max()) + 1
            co = _edge_incidence_counts(mfo, nn)
            return int((co == 1).sum()), int(Ta.shape[0]), pass_vdefl

        def _open_ladder() -> tuple[int, set]:
            # A part is closed iff ANY ladder rung yields zero open edges. A valid
            # periodic/curved face can leave a non-conformal seam open at one
            # deflection that closes at a finer one; a genuine gap stays open at
            # every rung. The base pass shares the base mesh the caller already
            # built (and budget-checked); the FINER rungs are bounded by the
            # ladder's own (larger) ceiling — OCC's deflection→triangle scaling is
            # markedly sub-quadratic for curved B-reps, so a (base/defl)^2
            # prediction over-skips valid rungs; build the rung and trust a closed
            # verdict only if it fits the ceiling.
            # Returns (open-edge count or -1 = UNDETERMINED, vertex_defl_coords).
            # -1 must NOT be treated as a FAIL — the caller falls back to the fast
            # check — so a valid part whose closing rung we could not afford is
            # never wrongly rejected. vertex_defl_coords is UNIONED (by world-space
            # position, not just OR'd as a boolean) across every rung actually run
            # (#397): each rung's own vertex-merge closes apices the SAME way the
            # base non-manifold pass below does, and a shape can read as "closed"
            # here while still carrying an off-vertex node the base pass's own
            # single-deflection check might not have caught — union by identity so
            # the caller's final count isn't silently capped at +1 regardless of
            # how many distinct vertices only this ladder catches.
            open0, ntris0, vdefl0 = _open_pass(deflection)
            if open0 < 0:
                return -1, vdefl0
            if open0 == 0:
                return 0, vdefl0
            if ntris0 > _LADDER_BASE_MAX_TRIS and _open_deadline != float("inf"):
                # Open at base, but too large to refine within the in-process time
                # budget — defer rather than run the expensive finer rungs. Skipped
                # when there is no time deadline (the export subprocess path, bounded
                # by a hard kill instead), so large parts ARE laddered out-of-process.
                return -1, vdefl0
            vdefl_any = vdefl0
            for d in (4, 16, 32):
                if time.monotonic() > _open_deadline:
                    return -1, vdefl_any  # out of budget — do not start another (finer) rung
                openK, _, vdeflK = _open_pass(deflection / d)
                vdefl_any = vdefl_any | vdeflK
                if openK < 0:
                    return -1, vdefl_any  # finer rung too large / out of time — undetermined
                if openK == 0:
                    return 0, vdefl_any
            return open0, vdefl_any  # every rung ran in budget and stayed open → genuine gap

        def _refined_untriangulated_faces() -> tuple[int, bool]:
            """Count faces that only fail to mesh at one finer deflection.

            This intentionally does not stitch or allocate the full triangle soup; it
            only asks OCC to refine and then verifies every face owns a triangulation.
            Return ``(count, verified)``. If the refined pass cannot be checked within
            the budget, ``verified`` is False so the caller can report any definite
            missing faces found so far without implying the count is complete.
            """
            if time.monotonic() > _open_deadline:
                return 0, False
            refined_deflection = deflection / _REFINED_UNTRIANGULATED_FACTOR
            BRepMesh_IncrementalMesh(occ, refined_deflection, False, 0.5, True)
            if time.monotonic() > _open_deadline:
                return 0, False
            fmap = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(occ, TopAbs_FACE, fmap)
            missing = 0
            n_tris = 0
            for fi in range(1, fmap.Size() + 1):
                if time.monotonic() > _open_deadline:
                    return missing, False
                face = TopoDS.Face_s(fmap.FindKey(fi))
                loc = TopLoc_Location()
                tri = BRep_Tool.Triangulation_s(face, loc)
                if tri is None:
                    missing += 1
                    continue
                n_tris += tri.NbTriangles()
            if n_tris > _REFINED_UNTRIANGULATED_MAX_TRIS and _open_deadline != float("inf"):
                return missing, False
            return missing, True

        def _finish(
            nm_edges: int,
            open_edges: int,
            base_untriangulated: int,
            nmv: int,
            vdefl: int,
        ) -> MeshGateResult:
            refined_untriangulated, refined_ok = _refined_untriangulated_faces()
            if not refined_ok and not (
                nm_edges
                or open_edges
                or base_untriangulated
                or refined_untriangulated
                or nmv
                or vdefl
            ):
                return MeshGateResult.unchecked()
            return MeshGateResult(
                nonmanifold_edges=nm_edges,
                open_edges=open_edges,
                untriangulated_faces=base_untriangulated,
                refined_untriangulated_faces=refined_untriangulated,
                nonmanifold_vertices=nmv,
                vertex_deflection_defects=vdefl,
                ok=True,
                refined_verified=refined_ok,
            )

        # Build the base-deflection mesh for the non-manifold / untriangulated
        # pass below FIRST. The open-edge ladder (which refines the cached
        # triangulation, and OCC never coarsens it back) runs LAST so it cannot
        # inflate this base mesh past the triangle budget.
        BRepMesh_IncrementalMesh(occ, deflection, False, 0.5, True)

        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(occ, TopAbs_FACE, faces)
        if faces.Size() == 0:
            return MeshGateResult.unchecked()

        # 1. Lay every face's triangulation into one global node/triangle list,
        #    flipping winding for REVERSED faces so orientation is consistent.
        vertices: list = []
        triangles: list = []
        face_base: dict = {}
        face_tri: dict = {}
        untriangulated = 0
        for fi in range(1, faces.Size() + 1):
            face = TopoDS.Face_s(faces.FindKey(fi))
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is None:
                # A face OCC could not tessellate — the exported boundary is
                # incomplete. A genuine defect, not a reason to bail to the fast
                # check (which would silently pass it). Count it and continue.
                untriangulated += 1
                continue
            trsf = loc.Transformation()
            base = len(vertices)
            face_base[fi] = base
            face_tri[fi] = (tri, loc)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                vertices.append((p.X(), p.Y(), p.Z()))
            reversed_face = face.Orientation() == TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                n1, n2, n3 = tri.Triangle(i).Get()
                a, b, c = base + n1 - 1, base + n2 - 1, base + n3 - 1
                if reversed_face:
                    a, b = b, a
                triangles.append((a, b, c))
        if not triangles:
            # Nothing meshed: a defect if some faces failed, else un-analysable.
            return (
                MeshGateResult(
                    untriangulated_faces=untriangulated,
                    ok=True,
                    refined_verified=False,
                )
                if untriangulated
                else MeshGateResult.unchecked()
            )
        if max_triangles is not None and len(triangles) > max_triangles:
            # Over the perf budget; bail before the slow stitch so the caller
            # falls back to the fast check rather than hanging.
            return MeshGateResult.unchecked()

        parent = list(range(len(vertices)))

        def find(x: int) -> int:
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(x: int, y: int) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[max(rx, ry)] = min(rx, ry)

        verts = np.asarray(vertices, dtype=np.float64)
        # --- non-manifold VERTICES (#298) ---
        # Detected on a coordinate-WELDED copy of the base soup (seam-safe; the
        # index-stitch below under-merges seams and would false-positive). A vertex
        # where >=2 surface sheets meet at a single point is edge-manifold and
        # watertight yet not a 2-manifold surface — a CAD scorer rejects it, and the
        # edge-incidence counts cannot see it. Computed once here; reported by all
        # post-stitch returns.
        try:
            _wd = float(np.linalg.norm(verts.max(0) - verts.min(0)))
            _wtol = max(1e-7, 1e-7 * _wd)
            _, _wi = np.unique(
                np.round(verts / _wtol).astype(np.int64), axis=0, return_inverse=True
            )
            _wi = np.asarray(_wi).ravel()
            _wmf = _wi[np.asarray(triangles, dtype=np.int64)]
            _wmf = _wmf[
                (_wmf[:, 0] != _wmf[:, 1]) & (_wmf[:, 1] != _wmf[:, 2]) & (_wmf[:, 0] != _wmf[:, 2])
            ]
            nmv = _nonmanifold_vertex_count(_wmf)
        except Exception:
            nmv = 0
        vmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(occ, TopAbs_VERTEX, vmap)
        emap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(occ, TopAbs_EDGE, emap)
        edge_adj = _edge_face_adjacency(occ, faces, emap)

        # 2. Merge shared-edge nodes by index, and collect per-BREP-vertex
        #    endpoints for the degenerate-edge merge in step 3.
        vertex_nodes: dict = defaultdict(list)
        for ei in range(1, emap.Size() + 1):
            if time.monotonic() > _open_deadline:
                # The per-edge stitch is over the gate budget (a very large/complex
                # part — few edges but expensive OCC calls each); defer to the fast
                # check rather than approach the worker op-timeout. Bounds total
                # gate wall-clock to ~the budget.
                return MeshGateResult.unchecked()
            edge = TopoDS.Edge_s(emap.FindKey(ei))
            node_lists = []
            for fi in edge_adj.get(ei, ()):
                if fi not in face_tri:
                    continue
                tri, loc = face_tri[fi]
                poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
                if poly is None:
                    continue
                arr = np.fromiter(poly.Nodes(), dtype=np.int64, count=poly.NbNodes()) + (
                    face_base[fi] - 1
                )
                node_lists.append(arr)
            if not node_lists:
                continue
            ref = node_lists[0]
            ref_pts = verts[ref]
            for other in node_lists[1:]:
                if other.shape[0] != ref.shape[0]:
                    continue  # cannot index-stitch a length mismatch
                op = verts[other]
                fwd = float(np.abs(ref_pts - op).max())
                rev = float(np.abs(ref_pts - op[::-1]).max())
                seq = other if fwd <= rev else other[::-1]
                for u, v in zip(ref.tolist(), seq.tolist()):
                    union(u, v)
            v_first = vmap.FindIndex(TopExp.FirstVertex_s(edge))
            v_last = vmap.FindIndex(TopExp.LastVertex_s(edge))
            for arr in node_lists:
                if v_first:
                    vertex_nodes[v_first].append(int(arr[0]))
                if v_last:
                    vertex_nodes[v_last].append(int(arr[-1]))
        # A node claiming to sit at BREP vertex `vi` (an edge polygon's own
        # first/last entry) is only trustworthy if it is actually within
        # `deflection` of that vertex's analytic position — union first,
        # verify never lets a genuinely-off-vertex node (a patched face whose
        # polygon endpoint misses its vertex) merge in silently as "the same
        # point". Record each offending vertex's own world-space position
        # (rounded — a set, not a count, so it can be unioned by IDENTITY
        # against the open-edge ladder's own separate finding below rather
        # than by a boolean OR that would silently cap the total at +1
        # regardless of how many DISTINCT vertices the ladder alone catches),
        # then union anyway so the rest of the stitch (and the open-edge
        # ladder) still closes normally around it — this check's job is to
        # REPORT the defect, not repair it.
        vertex_defl_coords: set = set()
        for vi, nodes in vertex_nodes.items():
            vp = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vmap.FindKey(vi)))
            vertex_xyz = np.array([vp.X(), vp.Y(), vp.Z()], dtype=np.float64)
            if float(np.abs(verts[nodes] - vertex_xyz).max()) > deflection:
                vertex_defl_coords.add((round(vp.X(), 6), round(vp.Y(), 6), round(vp.Z(), 6)))
            base_node = nodes[0]
            for n in nodes[1:]:
                union(base_node, n)

        # 3. Relabel to representatives and drop triangles that collapsed.
        roots = np.array([find(i) for i in range(len(vertices))], dtype=np.int64)
        uniq, inv = np.unique(roots, return_inverse=True)
        mf = inv[np.asarray(triangles, dtype=np.int64)]
        keep = (mf[:, 0] != mf[:, 1]) & (mf[:, 1] != mf[:, 2]) & (mf[:, 0] != mf[:, 2])
        mf = mf[keep]
        if mf.shape[0] == 0:
            _ov, _ov_vdefl = _open_ladder()
            _vd = len(vertex_defl_coords | _ov_vdefl)
            if _ov >= 0:
                return _finish(0, _ov, untriangulated, nmv, _vd)
            # open undetermined — a face that failed to tessellate (or a pinch
            # vertex) is still a definite defect
            return (
                _finish(0, 0, untriangulated, nmv, _vd)
                if (untriangulated or nmv or _vd)
                else MeshGateResult.unchecked()
            )

        # 4. Cancel opposite-winding flap pairs (a degenerate fold meshes to a
        #    triangle and its mirror; same 3 nodes, opposite parity). Same-winding
        #    duplicates are a real coincident-face overlap and are kept.
        srt = np.sort(mf, axis=1)

        def _even(t: tuple, s: tuple) -> bool:
            a, b, c = t
            return (a, b, c) in ((s[0], s[1], s[2]), (s[1], s[2], s[0]), (s[2], s[0], s[1]))

        groups: dict = defaultdict(lambda: [0, 0])
        parity: list = []
        for i in range(mf.shape[0]):
            s = (int(srt[i, 0]), int(srt[i, 1]), int(srt[i, 2]))
            t = (int(mf[i, 0]), int(mf[i, 1]), int(mf[i, 2]))
            is_even = _even(t, s)
            parity.append(is_even)
            groups[s][0 if is_even else 1] += 1
        cancel = {s: min(ev, od) for s, (ev, od) in groups.items()}
        seen_even: dict = defaultdict(int)
        seen_odd: dict = defaultdict(int)
        keep2 = np.ones(mf.shape[0], dtype=bool)
        for i in range(mf.shape[0]):
            s = (int(srt[i, 0]), int(srt[i, 1]), int(srt[i, 2]))
            if parity[i]:
                if seen_even[s] < cancel[s]:
                    seen_even[s] += 1
                    keep2[i] = False
            elif seen_odd[s] < cancel[s]:
                seen_odd[s] += 1
                keep2[i] = False
        mf = mf[keep2]
        if mf.shape[0] == 0:
            _ov, _ov_vdefl = _open_ladder()
            _vd = len(vertex_defl_coords | _ov_vdefl)
            if _ov >= 0:
                return _finish(0, _ov, untriangulated, nmv, _vd)
            # open undetermined — a face that failed to tessellate (or a pinch
            # vertex) is still a definite defect
            return (
                _finish(0, 0, untriangulated, nmv, _vd)
                if (untriangulated or nmv or _vd)
                else MeshGateResult.unchecked()
            )

        # 5. Non-manifold count from the index-stitched mesh: undirected edges
        #    shared by >2 triangles. (Closedness/open edges come from the
        #    deflection-ladder stitch — the precision-tuned index-stitch here
        #    leaves valid seams/poles spuriously open, so it must not drive the
        #    open-edge count.)
        n = int(uniq.shape[0])
        counts = _edge_incidence_counts(mf, n)
        nm_edges = int((counts > 2).sum())
        _ov, _ov_vdefl = _open_ladder()
        _vd = len(vertex_defl_coords | _ov_vdefl)
        if _ov >= 0:
            return _finish(nm_edges, _ov, untriangulated, nmv, _vd)
        # open undetermined — a non-manifold, untriangulated, non-manifold-vertex,
        # or vertex-deflection defect is still definite, independent of the
        # open-edge ladder
        if nm_edges or untriangulated or nmv or _vd:
            return _finish(nm_edges, 0, untriangulated, nmv, _vd)
        return MeshGateResult.unchecked()
    except Exception:
        return MeshGateResult.unchecked()


def _resolve_shape(session, object_name: str):
    if object_name:
        if object_name not in session.objects:
            return None, json.dumps(
                {
                    "error": f"Unknown object '{object_name}'. Registered: {list(session.objects.keys())}"
                }
            )
        return session.objects[object_name], None
    if session.current_shape is None:
        return None, json.dumps(
            {"error": "No shape in session. Execute code to create geometry first."}
        )
    return session.current_shape, None


def validate(session, object_name: str = "") -> str:
    """Report whether a shape would pass a watertight-manifold-solid validity gate.

    Returns a one-line PASS/FAIL verdict followed by a JSON report. A FAIL means
    a STEP/STL export of this shape would be rejected by a CAD scorer (and score
    zero), so fix it before exporting.
    """
    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err
    report = _validate_gate(session, shape)
    verdict = "PASS" if report["passes_gate"] else "FAIL"
    summary = f"Validity gate: {verdict}"
    if report["reasons"]:
        summary += " — " + "; ".join(report["reasons"])
    if report["warnings"]:
        summary += " (warning: " + "; ".join(report["warnings"]) + ")"
    if not report["passes_gate"]:
        summary += " — the build123d://skill/repair resource has the defect-class repair ladder"
    return summary + "\n" + json.dumps(report, indent=2)


def _validate_gate(session, shape) -> dict:
    """Gate the shape, isolating the expensive mesh check exactly as ``export()`` does.

    The B-rep checks (BRepCheck, edge→face map) are cheap and run in-worker. The mesh
    stitch is the un-interruptible native cost that a huge B-rep can drag past the op
    timeout (#360), so for a large shape it runs in the same hard-bounded subprocess
    ``export()`` uses (``_run_mesh_gate_subprocess``) — the exact check there catches the
    mesh open edges the old in-loop fast fallback was structurally blind to (#381). If
    that subprocess can't finish (timeout / no-subprocess host), the mesh result is
    reported ``"skipped"`` with a warning while the in-worker B-rep verdict stands — a
    subprocess kill can never lose the verdict, because the verdict was never in the
    subprocess. A small shape keeps the fast in-worker exact check (a STEP round-trip
    would dominate); if that overruns its inline budget it degrades to the fast fallback,
    which is likewise flagged mesh-open-edge-unverified.
    """
    from build123d_mcp.tools._bounded import _is_large

    if not _is_large([shape]):
        return _gate_report(shape)
    mesh = _mesh_gate_out_of_process(session, shape)
    return _gate_report(
        shape,
        exact=True,
        mesh_override=mesh if mesh is not None else MeshGateResult.unchecked(),
    )


def _mesh_gate_out_of_process(session, shape) -> MeshGateResult | None:
    """Serialise the shape to a temp STEP and run the exact mesh gate in the same
    hard-bounded subprocess ``export()`` uses. Returns ``MeshGateResult`` or
    ``None`` (→ ``"skipped"``) if it timed out, the host blocks child processes, or
    the shape couldn't be serialised — in every case the caller keeps the in-worker
    B-rep verdict rather than inventing a mesh result."""
    import os
    import tempfile

    from build123d_mcp.tools._budget import op_budget
    from build123d_mcp.tools.export import _write_step

    t0 = time.monotonic()
    work = tempfile.mkdtemp(prefix="b123d_validate_")
    step = os.path.join(work, "s.step")
    try:
        try:
            _write_step(shape, step)
        except Exception:  # noqa: BLE001 - unserialisable → skip mesh; B-rep still runs
            return None
        # Bound the child by the op budget LEFT (minus a margin for the in-worker B-rep
        # checks + subprocess teardown), so it is always killed before the parent op
        # watchdog SIGKILLs the worker — the export() convention.
        remaining = op_budget(session) - (time.monotonic() - t0) - _MESH_GATE_MARGIN_S
        if remaining < _MESH_GATE_MIN_S:
            return None
        return _run_mesh_gate_subprocess(step, timeout=remaining)
    finally:
        try:
            os.unlink(step)
        except OSError:
            pass
        try:
            os.rmdir(work)
        except OSError:
            pass
