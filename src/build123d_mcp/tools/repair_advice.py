"""Structured repair/edit recipes for agents to implement explicitly.

This module deliberately returns advice, not geometry.  The goal is to help an
agent write better build123d/OCP code in execute(), while keeping every
geometry-changing operation visible, auditable, and tied to acceptance checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    applies_when: list[str]
    approach: list[str]
    code_patterns: list[str]
    acceptance_checks: list[str]
    stop_conditions: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "applies_when": self.applies_when,
            "approach": self.approach,
            "code_patterns": self.code_patterns,
            "acceptance_checks": self.acceptance_checks,
            "stop_conditions": self.stop_conditions,
        }


_BASELINE_GATE = Recipe(
    id="gate_first_baseline",
    title="Establish a valid baseline before editing",
    applies_when=[
        "Any imported STEP edit, or any model that has not been export-gated yet.",
    ],
    approach=[
        "Import/register the original shape once and save a rollback snapshot.",
        "Run validate() and export() on the unchanged shape before any edit.",
        "If the source fails, repair the smallest inherited defect first and keep the repaired baseline named separately.",
        "After every candidate repair, compare volume and bounding box against the previous accepted baseline.",
    ],
    code_patterns=[
        "save_snapshot('before_repair')",
        "validate(part) -> locate_gate_defects(part) -> execute(candidate repair)",
        "export(part_candidate, 'step') as the acceptance gate, not validate() alone",
        "shape_compare(original_or_repaired_baseline, candidate) to bound unintended change",
    ],
    acceptance_checks=[
        "Single solid with non-zero volume.",
        "BRepCheck passes in memory and after STEP round trip.",
        "Volume/bbox movement is explained by the defect repair or requested edit.",
        "A rollback snapshot exists for the last known-good state.",
    ],
    stop_conditions=[
        "Do not stack the requested edit on top of an unaccepted repair candidate.",
        "If a broad heal moves unrelated faces, discard it and use a targeted recipe.",
    ],
)


_MALFORMED_FACE = Recipe(
    id="malformed_face_local_repair",
    title="Repair a localized BRepCheck-invalid face",
    applies_when=[
        "Gate says B-rep is not well-formed, BRepCheck failed, or locate_gate_defects reports brep_invalid_face.",
        "The defect is a discrete face or thin strip rather than a global self-intersection.",
    ],
    approach=[
        "Use one BRepCheck_Analyzer over the whole solid and match suspect face centers to locate_gate_defects().",
        "Try ShapeFix_Shape only as a candidate, then verify volume/bbox and export-gate result.",
        "If ShapeFix does not clear the defect, remove only the bad face and sew the surrounding shell across a small tolerance sweep.",
        "If drop-and-sew becomes BRep-valid but mesh-fragile, switch to the mesh-fragile-face recipe for the remaining defect.",
    ],
    code_patterns=[
        "BRepCheck_Analyzer(part.wrapped) once; never rebuild the analyzer per face on a large shape.",
        "BRepBuilderAPI_Sewing(tol) over all faces except the selected bad face; scan tight tolerances.",
        "Wrap raw OCCT output with a strict as_solid() helper that rejects mixed topology and multiple solids.",
    ],
    acceptance_checks=[
        "The candidate has one solid and expected face-count delta, usually -1 for a removed sliver strip.",
        "Volume change is small and local to the located defect.",
        "export() of the candidate succeeds; a validate() pass alone is not enough.",
    ],
    stop_conditions=[
        "If the repair creates mixed topology, multiple solids, or large volume drift, discard it.",
        "If the candidate is BRep-valid but still mesh-invalid, continue with a mesh recipe rather than repeating ShapeFix.",
    ],
)


_MESH_FRAGILE_FACE = Recipe(
    id="mesh_fragile_face_repatch",
    title="Repatch a mesh-only fragile or untriangulated face",
    applies_when=[
        "BRepCheck passes but the gate reports mesh open edges, refined untriangulated faces, or vertex deflection.",
        "locate_gate_defects identifies a tiny BSpline/sliver face or local mesh cluster.",
    ],
    approach=[
        "Locate faces nearest the reported mesh defect coordinates and rank them by area and distance.",
        "Prefer rebuilding the smallest face from its existing boundary with BRepFill_Filling, then re-sew tightly.",
        "If the boundary contains a near-zero edge, replace the face with simpler planar triangles only if sewing stays BRep-valid.",
        "For self-touch coordinates, a tiny explicit relief cut is acceptable only when it is far from requested design features and fully documented.",
    ],
    code_patterns=[
        "for i, f in enumerate(shape.faces()): collect area, center, bbox, geom_type near defect_point",
        "BRepFill_Filling() over the face boundary, followed by BRepBuilderAPI_Sewing(0.001..0.05)",
        "For tiny self-touch: subtract a sub-millimetre Box/Sphere centered on the located defect, then export-gate.",
    ],
    acceptance_checks=[
        "BRep remains valid after the patch.",
        "Mesh gate no longer reports the same face/edge coordinate.",
        "The repair volume is sub-millimetre or otherwise justified by the local defect size.",
    ],
    stop_conditions=[
        "Do not delete a mesh-fragile face if that makes BRepCheck fail.",
        "Do not keep a relief cut if it touches the requested feature or materially changes design intent.",
    ],
)


_EXPORT_SLIVERS = Recipe(
    id="export_roundtrip_sliver_cleanup",
    title="Clean slivers introduced by export-roundtrip topology",
    applies_when=[
        "The live candidate validates, but export() or re-imported output.step fails BRepCheck.",
        "Invalid faces are near-zero planar slivers on an old overlap plane or seam after a union/cut.",
    ],
    approach=[
        "Import the written STEP under a separate name and locate invalid faces on the round-tripped file.",
        "Map those coordinates back to the live candidate; inspect face area, center, and plane/axis.",
        "Remove only the near-zero faces at the obsolete seam and re-sew the shell with tight tolerances.",
        "Export the cleaned candidate over output.step and re-run the export gate.",
    ],
    code_patterns=[
        "failed_export = import_step('output.step'); invalid_face_report(failed_export)",
        "bad = [i for i, f in enumerate(candidate.faces()) if abs(f.area) < eps and on_old_plane(f.center())]",
        "BRepBuilderAPI_Sewing(tol) over candidate.faces() excluding bad sliver faces",
    ],
    acceptance_checks=[
        "The cleaned candidate changes volume only by the removed sliver amount.",
        "The requested feature measurements are unchanged from the live-valid candidate.",
        "The final output.step passes the export gate's BRep checks.",
    ],
    stop_conditions=[
        "If removed faces are not near-zero or not on the obsolete seam, stop and rebuild the boolean without hidden overlap.",
        "If export creates new invalid coordinates, repeat diagnosis from the written STEP, not the live shape.",
    ],
)


_OPEN_SHELL = Recipe(
    id="open_shell_or_disjoint_edit_rebuild",
    title="Rebuild an edit that produced open edges or disjoint solids",
    applies_when=[
        "The edited candidate reports open edges, not watertight, open shell, mixed topology, or more than one solid.",
        "A face extrusion, local fuse, or bore recut created an invalid feature.",
    ],
    approach=[
        "Treat this as a failed construction, not a repair target. Restore the previous snapshot and rebuild the edit.",
        "Avoid extruding one face when the real feature front is split across multiple faces.",
        "Construct the added/removed feature as a full solid with deliberate overlap, fuse/cut once, then re-cut through features through the entire new length.",
        "Reject any candidate that creates multiple solids or fills/duplicates the intended through feature.",
    ],
    code_patterns=[
        "restore_snapshot('before_edit') before trying an alternate topology.",
        "Build an explicit sleeve/block/patch solid from measured axes and planes; do not rely on an isolated face extrusion.",
        "After fuse, cut the bore/hole as one continuous cutter through original plus new material.",
    ],
    acceptance_checks=[
        "validate() reports exactly one watertight solid.",
        "Feature recognizers show the edited feature changed and adjacent holes/bores remain continuous.",
        "shape_compare() localizes the change to the requested region.",
    ],
    stop_conditions=[
        "If the no-cutter variant fills a bore, discard it.",
        "If a tighter cutter creates a separate bore segment or second solid, discard it.",
        "If ShapeFix crashes or returns mixed topology, do not replace a written output with that candidate.",
    ],
)


_SELF_TOUCH = Recipe(
    id="self_touch_boolean_redesign",
    title="Replace coincident/self-touching booleans with explicit clearance",
    applies_when=[
        "The gate reports non-manifold edges, faces meet more than two ways, coincident faces, or self-touch.",
    ],
    approach=[
        "Redo the boolean so surfaces overlap or separate by a real tolerance; do not leave tangent/coincident faces.",
        "If the self-touch is inherited and far from the edit, a tiny documented relief can be acceptable.",
        "For generated models, move the construction planes/limits rather than healing the damaged result.",
    ],
    code_patterns=[
        "overlap = 0.05 or a named tolerance; extend cutters past target faces by overlap.",
        "relief_tool = Pos(*defect_point) * Box(eps, eps, eps) only for inherited self-touch defects.",
    ],
    acceptance_checks=[
        "locate_gate_defects no longer reports the same self-touch coordinate.",
        "The edit remains dimensionally correct after adding overlap/clearance.",
    ],
    stop_conditions=[
        "Do not use tolerance-only sewing to hide a real coincident-face design.",
        "Do not keep a relief cut that is larger than the localized defect warrants.",
    ],
)


_SPLIT_BORED_BOSS = Recipe(
    id="split_bored_boss_extension",
    title="Extend a split rounded-square boss while preserving its central bore",
    applies_when=[
        "The requested edit increases boss length/depth/height and the boss has a through bore.",
        "The target front face is split into several planar faces around the bore or rounded corners.",
    ],
    approach=[
        "Measure the boss axis from cylindrical bore faces, not from the rendered view alone.",
        "Identify the old front plane, requested delta, bore diameter/radius, and outer boss profile/bounds.",
        "Do not extrude a single planar face if the boss front is split; build an explicit extension solid/sleeve that overlaps the old boss by a small epsilon.",
        "Fuse the extension, then re-cut the central bore as one continuous cutter through original plus added length.",
        "If export-roundtrip reports old-plane slivers, use the export-roundtrip sliver cleanup recipe.",
    ],
    code_patterns=[
        "axis_out = normalized cylinder axis; old_front = measured front plane; new_front = old_front + delta",
        "extension = make_prism_or_sleeve_from_profile(center, axis_out, old_front - eps, new_front)",
        "edited = (baseline + extension) - continuous_bore_cutter(axis, radius, total_length + 2*eps)",
        "Reject face-only extrude variants unless the profile has the full outer wire and inner bore wire.",
    ],
    acceptance_checks=[
        "The boss length/depth changes by the requested delta along the measured axis.",
        "The central bore diameter and continuity are preserved through the new length.",
        "The final shape is one valid solid; no open edges and no disjoint extension solid.",
        "shape_compare() shows changes localized to the boss and any documented inherited repair.",
    ],
    stop_conditions=[
        "If the candidate fills the bore, discard it.",
        "If the candidate creates a separate bore segment, discard it.",
        "If the candidate leaves open edges, rebuild the extension from a complete profile rather than sewing the damaged result blindly.",
    ],
)


_RECIPES = {
    r.id: r
    for r in (
        _BASELINE_GATE,
        _MALFORMED_FACE,
        _MESH_FRAGILE_FACE,
        _EXPORT_SLIVERS,
        _OPEN_SHELL,
        _SELF_TOUCH,
        _SPLIT_BORED_BOSS,
    )
}

_MATCHERS: list[tuple[str, list[str]]] = [
    (
        "malformed_face_local_repair",
        [r"BRepCheck", r"B-rep is not well-formed", r"brep_invalid_face"],
    ),
    (
        "mesh_fragile_face_repatch",
        [
            r"mesh_open_edge",
            r"mesh open edge",
            r"refined",
            r"untriangulated",
            r"finer mesh",
            r"vertex deflection",
        ],
    ),
    (
        "export_roundtrip_sliver_cleanup",
        [r"round.?trip", r"output\.step", r"export", r"sliver", r"old.*plane"],
    ),
    (
        "open_shell_or_disjoint_edit_rebuild",
        [r"open edge", r"not watertight", r"open shell", r"disjoint", r"two solids"],
    ),
    (
        "self_touch_boolean_redesign",
        [r"non-manifold", r"self-touch", r"coincident", r"faces meet >2"],
    ),
]

_GOAL_MATCHERS: list[tuple[str, list[str]]] = [
    (
        "split_bored_boss_extension",
        [
            r"\bboss\b",
            r"\bbore\b|through.?hole|central hole",
            r"extend|increase|length|height|depth|longer",
        ],
    ),
]


def _matches_all(text: str, patterns: list[str]) -> bool:
    return all(re.search(p, text, re.IGNORECASE) for p in patterns)


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _ordered_unique(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for recipe_id in ids:
        if recipe_id not in seen:
            seen.add(recipe_id)
            out.append(recipe_id)
    return out


def _markdown(recipe_ids: list[str]) -> str:
    lines = [
        "Repair advice",
        "",
        "Use these as implementation recipes in execute(); do not treat them as an opaque geometry mutator.",
        "",
    ]
    for idx, recipe_id in enumerate(recipe_ids, 1):
        recipe = _RECIPES[recipe_id]
        lines.append(f"{idx}. {recipe.title} ({recipe.id})")
        lines.append("   Approach:")
        for step in recipe.approach:
            lines.append(f"   - {step}")
        lines.append("   Acceptance:")
        for check in recipe.acceptance_checks:
            lines.append(f"   - {check}")
        lines.append("")
    return "\n".join(lines).rstrip()


def repair_advice(error_text: str = "", goal: str = "", context: str = "") -> str:
    """Return generic repair/edit recipes matched to a gate failure and goal."""

    combined = "\n".join(part for part in (error_text, context) if part)
    goal_text = goal or ""

    recipe_ids = ["gate_first_baseline"]
    for recipe_id, patterns in _GOAL_MATCHERS:
        if _matches_all(goal_text, patterns) or _matches_all(combined, patterns):
            recipe_ids.append(recipe_id)

    for recipe_id, patterns in _MATCHERS:
        if _matches_any(combined, patterns):
            recipe_ids.append(recipe_id)

    if len(recipe_ids) == 1:
        recipe_ids.extend(
            [
                "malformed_face_local_repair",
                "open_shell_or_disjoint_edit_rebuild",
            ]
        )

    recipe_ids = _ordered_unique(recipe_ids)
    data = {
        "kind": "repair_advice",
        "note": (
            "This is planning guidance only. Implement the chosen recipe as explicit "
            "build123d/OCP code in execute(), with snapshots, measurements, and export-gate checks."
        ),
        "matched_recipe_ids": recipe_ids,
        "recipes": [_RECIPES[recipe_id].as_dict() for recipe_id in recipe_ids],
        "next_tool_calls": [
            "validate(object_name)",
            "locate_gate_defects(object_name) if the gate fails",
            "execute(code) to implement exactly one candidate recipe",
            "export(path, 'step', object_name=...) to verify the written STEP",
            "shape_compare(baseline, candidate) when preserving surrounding geometry matters",
        ],
        "markdown": _markdown(recipe_ids),
    }
    return json.dumps(data, indent=2)
