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


def _gate_report(shape) -> dict:
    """Return the validity-gate verdict for a shape as a plain dict.

    Reused by the export tool so a 3D export can warn when the written solid
    would fail the gate.
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
    mesh_nm_edges, mesh_ok = _mesh_defects(shape)
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
            if faces < 2:
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
