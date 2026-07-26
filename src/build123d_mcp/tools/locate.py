"""``locate_gate_defects()`` — report WHERE a solid fails the validity gate.

The validate/export gate says *what* is wrong (``1 non-manifold edge``,
``BRepCheck failed``) but never *where*, so the agent repairs blind — chamfer
here, sew there — burning 50-70 execute() calls per fixture. This returns each
defect with **3D coordinates** (and B-rep face/edge identity), turning a blind
hunt into a targeted fix.

The mesh non-manifold check tessellates (un-interruptible OCC ``BRepMesh``), so —
like the export gate and the render fix — the work runs in a real ``subprocess``
the parent hard-bounds, never blocking/SIGKILLing the worker. On a host that
blocks child-process creation (#143 / InProcessSession) it falls back to running
in-process (those hosts run no worker op-timeout, so there is nothing to kill).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter

from build123d_mcp.tools._budget import op_budget
from build123d_mcp.tools.validate import _resolve_shape

# The locate subprocess is bounded by the time LEFT in the op budget (op_budget(),
# shared with the parent op-timeout) minus a margin, so the un-interruptible mesh
# check is always killed before the parent SIGKILLs the worker.
_LOCATE_MARGIN_S = 15
_LOCATE_MIN_S = 10

_DEFECT_DIAGNOSTICS = {
    "brep_invalid_face": {
        "diagnostic_class": "brep_quality",
        "repair_family": "rebuild_or_replace_bad_face",
        "next_step": (
            "Use face_index/where to inspect the local patch, then rebuild or replace that "
            "face explicitly in execute(); avoid broad opaque healing."
        ),
    },
    "open_edge": {
        "diagnostic_class": "brep_topology",
        "repair_family": "sew_shell_or_add_missing_face",
        "next_step": (
            "Use the edge midpoint to find the gap, then add the missing face or sew the "
            "local shell in execute()."
        ),
    },
    "nonmanifold_edge": {
        "diagnostic_class": "brep_topology",
        "repair_family": "separate_self_touch_or_redo_boolean",
        "next_step": (
            "Inspect the edge shared by 3+ faces; redo the boolean or add explicit relief "
            "so the result is a true manifold."
        ),
    },
    "mesh_open_edge": {
        "diagnostic_class": "mesh_topology",
        "repair_family": "repair_nonconformal_face_junction",
        "next_step": (
            "Treat the coordinate as a non-conformal face junction; re-patch or re-sew "
            "the local faces and verify with export(), not validate() alone."
        ),
    },
    "mesh_nonmanifold_edge": {
        "diagnostic_class": "mesh_topology",
        "repair_family": "separate_self_touch_or_redo_boolean",
        "next_step": (
            "Inspect for coincident or tangent sheets meeting >2 ways; redo the boolean "
            "or add explicit clearance/relief in build123d code."
        ),
    },
    "mesh_nonmanifold_vertex": {
        "diagnostic_class": "mesh_topology",
        "repair_family": "avoid_corner_touch",
        "next_step": (
            "Separate corner-touching bodies or add enough material/overlap that they "
            "fuse into one 2-manifold solid."
        ),
    },
    "mesh_refined_untriangulated_face": {
        "diagnostic_class": "mesh_quality",
        "repair_family": "repatch_fragile_face",
        "next_step": (
            "Use face_index/where to find the fragile face, simplify or re-patch it, "
            "then run the export gate."
        ),
    },
    "mesh_untriangulated_face": {
        "diagnostic_class": "mesh_quality",
        "repair_family": "rebuild_unmeshable_face",
        "next_step": (
            "Use face_index/where to find the face that cannot be triangulated at the "
            "base tolerance, then simplify or rebuild it before export."
        ),
    },
    "mesh_vertex_deflection_defect": {
        "diagnostic_class": "mesh_quality",
        "repair_family": "repatch_boundary_to_vertices",
        "next_step": (
            "Rebuild the local patch so triangulated edge endpoints match the BREP "
            "vertices; tolerance-only fixes can still fail downstream."
        ),
    },
    "locator_error": {
        "diagnostic_class": "diagnostic_incomplete",
        "repair_family": "rerun_or_cross_check",
        "next_step": (
            "The locator skipped one probe; cross-check validate()/export() output and "
            "rerun locate_gate_defects() after simplifying or isolating the part."
        ),
    },
}

_DEFECT_PRIORITY = (
    "brep_invalid_face",
    "open_edge",
    "nonmanifold_edge",
    "mesh_open_edge",
    "mesh_nonmanifold_edge",
    "mesh_nonmanifold_vertex",
    "mesh_untriangulated_face",
    "mesh_refined_untriangulated_face",
    "mesh_vertex_deflection_defect",
    "locator_error",
)


def _enrich_defect(defect: dict) -> dict:
    out = dict(defect)
    meta = _DEFECT_DIAGNOSTICS.get(str(out.get("kind", "?")), {})
    for key in ("diagnostic_class", "repair_family", "next_step"):
        if key in meta:
            out.setdefault(key, meta[key])
    if meta and "verify_after_repair" not in out:
        out["verify_after_repair"] = (
            "Run export(..., 'step') so the written-and-reimported STEP passes the gate; "
            "validate() alone can miss round-trip failures."
        )
    return out


def _diagnosis(defects: list[dict]) -> dict:
    if not defects:
        return {
            "status": "no_located_defects",
            "primary_kind": None,
            "counts_by_kind": {},
            "diagnostic_classes": [],
            "repair_families": [],
            "recommended_next_steps": [
                "If validate()/export() still reports FAIL, inspect that exact gate output; "
                "this locator only reports structural gate defects it can localise."
            ],
        }

    counts = Counter(str(d.get("kind", "?")) for d in defects)
    primary = next(
        (kind for kind in _DEFECT_PRIORITY if counts.get(kind)), counts.most_common(1)[0][0]
    )
    classes = sorted(
        {
            str(d.get("diagnostic_class"))
            for d in defects
            if d.get("diagnostic_class") and d.get("diagnostic_class") != "diagnostic_incomplete"
        }
    )
    families = sorted(
        {
            str(d.get("repair_family"))
            for d in defects
            if d.get("repair_family") and d.get("repair_family") != "rerun_or_cross_check"
        }
    )
    primary_meta = _DEFECT_DIAGNOSTICS.get(primary, {})
    steps = [
        "Open build123d://skill/repair and use the matching repair-family section.",
        primary_meta.get(
            "next_step",
            "Use the located coordinates/indices to write an explicit local repair in execute().",
        ),
        "After each attempt, restore failed snapshots and verify the written STEP with export().",
    ]
    if counts.get("locator_error"):
        steps.insert(
            1,
            "One locator probe was incomplete; use the reported defects plus validate()/export() "
            "rather than treating this as exhaustive.",
        )
    return {
        "status": "defects_located",
        "primary_kind": primary,
        "counts_by_kind": dict(sorted(counts.items())),
        "diagnostic_classes": classes,
        "repair_families": families,
        "recommended_next_steps": steps,
    }


def _format(defects: list) -> str:
    enriched = [_enrich_defect(d) for d in defects]
    payload = {
        "count": len(enriched),
        "defects": enriched,
        "diagnosis": _diagnosis(enriched),
    }
    if not defects:
        return (
            "No validity defects located — the part passes the structural checks.\n"
            + json.dumps(payload)
        )
    kinds = ", ".join(sorted({d.get("kind", "?") for d in enriched}))
    return f"{len(defects)} defect(s) located ({kinds}):\n" + json.dumps(payload, indent=2)


def locate_gate_defects(session, object_name: str = "") -> str:
    """Locate the validity-gate defects on a shape, each with 3D coordinates.

    Returns a defect list: ``brep_invalid_face`` (face index + center + status),
    ``open_edge`` / ``nonmanifold_edge`` (B-rep edge midpoint), ``mesh_open_edge``
    (an unclosed tessellated boundary — approximate, from a coordinate weld rather
    than the gate's own exact topology-stitched check; re-check with the export
    gate after a fix), the mesh self-touches a CAD scorer rejects —
    ``mesh_nonmanifold_edge`` and ``mesh_nonmanifold_vertex`` (corner-to-corner
    touch), ``mesh_untriangulated_face`` (a face that fails to tessellate at the
    base tolerance), ``mesh_refined_untriangulated_face`` (a face that only fails
    at a finer tolerance) — and ``mesh_vertex_deflection_defect`` (a
    tessellated edge endpoint
    that misses its BREP vertex by more than the mesh deflection — a
    patched/healed face whose boundary is topologically closed but
    geometrically off-vertex; BRepCheck and a coordinate weld both miss this,
    but a CAD scorer's own mesh sanity check does not). Empty list means the
    part passes the structural checks. The JSON also includes ``diagnosis``:
    counts by defect kind, the primary defect class, repair-family labels, and
    next steps that keep the repair as explicit build123d/OCP code in
    ``execute()``. object_name: named object from show() (default: current shape).
    """
    t0 = time.monotonic()
    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err

    from build123d_mcp.tools.export import _write_step

    work = tempfile.mkdtemp(prefix="b123d_locate_")
    in_step = os.path.join(work, "in.step")
    out_json = os.path.join(work, "defects.json")
    try:
        try:
            _write_step(shape, in_step)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"could not serialise the shape to locate defects: {exc}"})

        remaining = op_budget(session) - (time.monotonic() - t0) - _LOCATE_MARGIN_S
        if remaining < _LOCATE_MIN_S:
            return json.dumps(
                {
                    "error": "not enough of the op budget left to locate defects safely; retry on a fresh op."
                }
            )

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build123d_mcp._locate_subprocess",
                    in_step,
                    out_json,
                    str(remaining),
                ],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return json.dumps(
                {
                    "error": "defect location exceeded the time budget — the part is too large/complex "
                    "to mesh-check in budget; inspect with measure()/render()."
                }
            )
        except OSError:
            # Host blocks child-process creation (#143 / InProcessSession). OCC
            # tessellation is uninterruptible, and this path has no child process
            # to kill, so keep the safe B-rep diagnostics and report mesh location
            # as unavailable instead of risking an unbounded in-process call.
            from build123d_mcp._locate_subprocess import collect_defects

            return _format(collect_defects(shape, include_mesh_checks=False))

        if proc.returncode != 0 or not os.path.exists(out_json):
            return json.dumps({"error": "defect locator failed: " + (proc.stderr or "")[-300:]})
        try:
            with open(out_json) as f:
                defects = json.load(f)["defects"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            return json.dumps({"error": f"defect locator produced an unreadable result: {exc}"})
        return _format(defects)
    finally:
        for p in (in_step, out_json):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass
