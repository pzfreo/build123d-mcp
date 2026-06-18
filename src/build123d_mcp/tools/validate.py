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

    reasons: list[str] = []
    if not brep_valid:
        reasons.append("B-rep is not well-formed (BRepCheck failed)")
    if volume <= _EPS:
        reasons.append("zero/degenerate volume")
    if open_edges:
        reasons.append(f"{open_edges} open edge(s) — not watertight (open shell or unsewn faces)")
    if nonmanifold_edges:
        reasons.append(f"{nonmanifold_edges} non-manifold edge(s) — edges shared by 3+ faces")
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

    passes = brep_valid and watertight_manifold and volume > _EPS and n_solids >= 1
    return {
        "passes_gate": passes,
        "n_solids": n_solids,
        "volume": volume,
        "watertight_manifold": watertight_manifold,
        "open_edges": open_edges,
        "nonmanifold_edges": nonmanifold_edges,
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
