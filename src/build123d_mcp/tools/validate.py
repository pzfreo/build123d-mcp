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

_EPS = 1e-9

# Triangle-count budgets for the accurate (slow) topology-stitch mesh check.
# Above the budget the gate falls back to the fast coordinate-weld check.
# Inline validate() is called often, so it only runs the exact check when cheap;
# export() is authoritative and runs once, so it gets a generous budget that
# still bounds the worst case (the stitch is ~0.3 ms/triangle).
_EXACT_INLINE_MAX_TRIS = 10000
_EXACT_EXPORT_MAX_TRIS = 80000


def _gate_report(shape, exact: bool = False) -> dict:
    """Return the validity-gate verdict for a shape as a plain dict.

    Reused by the export tool so a 3D export can warn when the written solid
    would fail the gate.

    ``exact`` selects the mesh non-manifold check: the default fast coordinate-weld
    (_mesh_defects, sub-second, used for interactive validate()) or the accurate
    topology-stitch (_mesh_defects_exact, slower, used at export where shipping an
    invalid solid actually costs). The exact check is tolerance-free, so it avoids
    the weld's occasional false positives and false negatives.
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
    _cap = _EXACT_EXPORT_MAX_TRIS if exact else _EXACT_INLINE_MAX_TRIS
    mesh_nm_edges, mesh_ok = _mesh_defects_exact(shape, max_triangles=_cap)
    mesh_check = "exact"
    if not mesh_ok:
        mesh_nm_edges, mesh_ok = _mesh_defects(shape)
        mesh_check = "fast"
    mesh_nonmanifold = mesh_ok and mesh_nm_edges > 0

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

    passes = (
        brep_valid
        and watertight_manifold
        and not mesh_nonmanifold
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


def _mesh_defects(shape) -> tuple[int, bool]:
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

        bb = shape.bounding_box()
        diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        if diag <= 0:
            return 0, False
        verts, tris = shape.tessellate(max(diag * 1e-3, 1e-4))
        if not verts or not tris:
            return 0, False

        q = diag * 1e-5  # weld tolerance: merge per-face samples on shared edges
        remap: list[int] = []
        keys: dict = {}
        for v in verts:
            k = (round(v.X / q), round(v.Y / q), round(v.Z / q))
            remap.append(keys.setdefault(k, len(keys)))

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


def _mesh_defects_exact(shape, max_triangles: int | None = None) -> tuple[int, bool]:
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
    caller can fall back to the fast check. Returns (nonmanifold_edges, ok);
    ok=False if the mesh could not be built or was over budget (the caller then
    does not gate on it / falls back).
    """
    try:
        import math
        from collections import defaultdict

        import numpy as np
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_VERTEX
        from OCP.TopExp import TopExp
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import (
            TopTools_IndexedDataMapOfShapeListOfShape,
            TopTools_IndexedMapOfShape,
        )

        bb = shape.bounding_box()
        diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        if diag <= 0:
            return 0, False
        # Deflection relative to part scale, clamped — matches the scorer.
        deflection = min(0.5, max(0.005, diag * 1e-3))
        occ = shape.wrapped
        BRepMesh_IncrementalMesh(occ, deflection, False, 0.5, True)

        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(occ, TopAbs_FACE, faces)
        if faces.Size() == 0:
            return 0, False

        # 1. Lay every face's triangulation into one global node/triangle list,
        #    flipping winding for REVERSED faces so orientation is consistent.
        vertices: list = []
        triangles: list = []
        face_base: dict = {}
        face_tri: dict = {}
        for fi in range(1, faces.Size() + 1):
            face = TopoDS.Face_s(faces.FindKey(fi))
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is None:
                return 0, False
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
            return 0, False
        if max_triangles is not None and len(triangles) > max_triangles:
            # Over the perf budget; bail before the slow stitch so the caller
            # falls back to the fast check rather than hanging.
            return 0, False

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
        vmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(occ, TopAbs_VERTEX, vmap)
        edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(occ, TopAbs_EDGE, TopAbs_FACE, edge_faces)

        # 2. Merge shared-edge nodes by index, and collect per-BREP-vertex
        #    endpoints for the degenerate-edge merge in step 3.
        vertex_nodes: dict = defaultdict(list)
        for ei in range(1, edge_faces.Extent() + 1):
            edge = TopoDS.Edge_s(edge_faces.FindKey(ei))
            node_lists = []
            for adj in edge_faces.FindFromIndex(ei):
                fi = faces.FindIndex(adj)
                if fi == 0 or fi not in face_tri:
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
        for nodes in vertex_nodes.values():
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
            return 0, True

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
            return 0, True

        # 5. Count undirected edges shared by >2 triangles.
        n = int(uniq.shape[0])
        e = mf[:, [0, 1, 1, 2, 0, 2]].reshape(-1, 2)
        e = np.sort(e, axis=1)
        keys = e[:, 0].astype(np.int64) * (n + 1) + e[:, 1]
        _, counts = np.unique(keys, return_counts=True)
        return int((counts > 2).sum()), True
    except Exception:
        return 0, False


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
    report = _gate_report(shape)
    verdict = "PASS" if report["passes_gate"] else "FAIL"
    summary = f"Validity gate: {verdict}"
    if report["reasons"]:
        summary += " — " + "; ".join(report["reasons"])
    if report["warnings"]:
        summary += " (warning: " + "; ".join(report["warnings"]) + ")"
    return summary + "\n" + json.dumps(report, indent=2)
