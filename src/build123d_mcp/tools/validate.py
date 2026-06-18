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
    try:
        is_manifold = bool(shape.is_manifold)
    except Exception:
        is_manifold = False
    try:
        brep_valid = bool(BRepCheck_Analyzer(shape.wrapped).IsValid())
    except Exception:
        brep_valid = False

    reasons: list[str] = []
    if not brep_valid:
        reasons.append("B-rep is not well-formed (BRepCheck failed)")
    if volume <= _EPS:
        reasons.append("zero/degenerate volume")
    if not is_manifold:
        reasons.append("not a closed manifold — open shell, 2D sketch, or non-manifold edges")
    if n_solids == 0:
        if is_manifold and volume > _EPS:
            reasons.append(
                "closed surface but no solid body — wrap the faces in Solid() before export"
            )
        else:
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

    passes = brep_valid and is_manifold and volume > _EPS and n_solids >= 1
    return {
        "passes_gate": passes,
        "n_solids": n_solids,
        "volume": volume,
        "is_manifold": is_manifold,
        "brep_valid": brep_valid,
        "warnings": warnings,
        "reasons": reasons,
    }


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
