"""Out-of-process localized surface comparison for ``shape_compare``.

Run as ``python -m build123d_mcp._shape_compare_subprocess <a.step> <b.step>
<out.json> <eps>``. The two STEP files are imported, tessellated, and compared
with a symmetric nearest-neighbour surface distance. This is isolated because
OCC tessellation is an un-interruptible native call; the parent bounds this
process with ``subprocess.run(timeout=...)`` so the worker session survives a
large or pathological part.

Noise model (why eps is not a fixed mm). The two shapes are tessellated
INDEPENDENTLY, so even an unchanged surface has a nonzero nearest-neighbour
distance: most points sit at ~0, but a thin tail of outliers (sharp edges,
corners where the two triangulations diverge) reaches roughly the mesh
DEFLECTION. So eps is scaled to the deflection, and on top of that the moved
points are clustered and only SIGNIFICANT components are kept — the real edit is
one contiguous patch of many vertices, while residual noise is scattered tiny
components. Together these stop a same-geometry pair from fabricating a change.
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import deque
from typing import Any

# Headroom (s) the exact boolean needs after tessellation; if less than this remains
# of the op budget, skip it and report the (flagged) mesh estimate instead of risking
# a hard subprocess kill that would lose the whole result.
_BOOL_RESERVE_S = 25.0

# eps (the per-vertex "this point moved" threshold) = factor x mesh deflection,
# placed above the independent-tessellation noise floor. Deflection is diag*1e-3
# (see compare_shapes), so this is ~3x the typical worst-case noise.
_EPS_DEFLECTION_FACTOR = 3.0
_EPS_FLOOR_MM = 0.05
# A genuine changed region is a contiguous moved patch that spans real physical
# size; residual tessellation noise is a speck within the mesh resolution. Filter
# by SPATIAL SPAN (density-independent — works for sparse synthetic boxes and dense
# real parts alike): keep a component only if its bbox diagonal is at least this
# many mesh-deflections across. A speck of noise spans ~1 deflection; a real feature
# spans many.
_MIN_REGION_SPAN_FACTOR = 2.5
_MIN_REGION_SPAN_FLOOR = 0.1
# unchanged_elsewhere: the change is localized if the changed regions cluster in one
# area — i.e. the region centroids span only a small fraction of the part diagonal.
# Spread (not moved-fraction, not region-count) is the right signal: it is scale-
# independent (a feature is a huge fraction of a tiny toy part but a tiny fraction of
# a real one), and a single ring feature (a fillet) fragments into many arc
# components clustered in one place — still localized.
_LOCAL_SPREAD_FRAC = 0.4


def _round_pt(p: list[float] | tuple[float, float, float], digits: int = 4) -> list[float]:
    return [round(float(p[0]), digits), round(float(p[1]), digits), round(float(p[2]), digits)]


def _bbox_diag(mins: list[float], maxs: list[float]) -> float:
    return math.dist(mins, maxs)


def _shape_diag(shape: Any) -> float:
    bb = shape.bounding_box()
    return math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))


def _tessellate_points(
    shape: Any, deflection: float
) -> tuple[list[tuple[float, float, float]], list[list[int]]]:
    """Tessellate at a SHARED deflection so both meshes have comparable sampling."""
    verts, tris = shape.tessellate(deflection)
    pts = [(float(v.X), float(v.Y), float(v.Z)) for v in verts]
    return pts, [list(t) for t in tris]


def _moved_components(
    pts: list[tuple[float, float, float]], tris: list[list[int]], moved: set[int]
) -> list[list[int]]:
    if not moved:
        return []

    adj: dict[int, set[int]] = {i: set() for i in moved}
    for tri in tris:
        if len(tri) < 3:
            continue
        a, b, c = tri[0], tri[1], tri[2]
        for u, v in ((a, b), (b, c), (a, c)):
            if u in moved and v in moved:
                adj[u].add(v)
                adj[v].add(u)

    seen: set[int] = set()
    components: list[list[int]] = []
    for start in moved:
        if start in seen:
            continue
        q = deque([start])
        seen.add(start)
        comp: list[int] = []
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        components.append(comp)
    return components


def _regions(
    pts: list[tuple[float, float, float]],
    tris: list[list[int]],
    moved: set[int],
    dist: Any,
    min_span: float,
) -> list[dict]:
    """Significant changed regions on one shape: contiguous moved-vertex patches that
    span at least ``min_span`` mm, each with its own centroid/bbox/local max."""
    out: list[dict] = []
    for comp in _moved_components(pts, tris, moved):
        cpts = [pts[i] for i in comp]
        mins = [min(p[k] for p in cpts) for k in range(3)]
        maxs = [max(p[k] for p in cpts) for k in range(3)]
        if _bbox_diag(mins, maxs) < min_span:
            continue  # a speck within mesh resolution — tessellation noise, not a real change
        cen = [sum(p[k] for p in cpts) / len(cpts) for k in range(3)]
        out.append(
            {
                "centroid": _round_pt(cen),
                "bbox": [_round_pt(mins), _round_pt(maxs)],
                "max_deviation": round(max(float(dist[i]) for i in comp), 4),
                "point_count": len(comp),
            }
        )
    return out


_EXACT_VOL_TOL = 1.0  # mm^3 — ignore boolean slivers below this


def _chunk_displacement(chunk: Any, ref_solid: Any) -> float:
    """Max distance from a difference chunk's vertices to the reference solid — i.e.
    how far the surface actually moved (exact, no vertex-NN flat-face inflation)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    e = TopExp_Explorer(chunk, TopAbs_VERTEX)
    seen: set = set()
    mx = 0.0
    while e.More():
        v = TopoDS.Vertex_s(e.Current())
        p = BRep_Tool.Pnt_s(v)
        key = (round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3))
        if key not in seen:
            seen.add(key)
            d = BRepExtrema_DistShapeShape(v, ref_solid)
            if d.IsDone():
                mx = max(mx, d.Value())
        e.Next()
    return mx


def _exact_region_magnitude(
    shape_a: Any, shape_b: Any, regions: list, margin: float = 3.0
) -> dict | None:
    """Exact boolean diff in the located region(s): true added/removed VOLUME and
    surface DISPLACEMENT, with no vertex-NN inflation. Clipping to the (tight) region
    keeps the boolean inside budget even on huge parts. Returns None if the boolean
    fails (caller keeps the mesh estimate)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.GProp import GProp_GProps

    pts = [c for r in regions for c in r["bbox"]]
    mn = [min(p[k] for p in pts) - margin for k in range(3)]
    mx = [max(p[k] for p in pts) + margin for k in range(3)]
    try:
        box = BRepPrimAPI_MakeBox(gp_Pnt(*mn), gp_Pnt(*mx)).Shape()
        cA = BRepAlgoAPI_Common(shape_a.wrapped, box)
        cA.Build()
        cB = BRepAlgoAPI_Common(shape_b.wrapped, box)
        cB.Build()
        if not cA.IsDone() or not cB.IsDone():
            return None
        rem = BRepAlgoAPI_Cut(cA.Shape(), cB.Shape())  # material in A not B = removed
        rem.Build()
        add = BRepAlgoAPI_Cut(cB.Shape(), cA.Shape())  # material in B not A = added
        add.Build()
        if not rem.IsDone() or not add.IsDone():
            return None
    except Exception:  # noqa: BLE001 - boolean failure → fall back to mesh estimate
        return None

    def _vol(s: Any) -> float:
        g = GProp_GProps()
        BRepGProp.VolumeProperties_s(s, g)
        return g.Mass()

    def _ctr(s: Any) -> list[float]:
        g = GProp_GProps()
        BRepGProp.VolumeProperties_s(s, g)
        c = g.CentreOfMass()
        return [round(c.X(), 3), round(c.Y(), 3), round(c.Z(), 3)]

    rv, av = _vol(rem.Shape()), _vol(add.Shape())
    disp = 0.0
    if av > _EXACT_VOL_TOL:
        disp = max(disp, _chunk_displacement(add.Shape(), shape_a.wrapped))
    if rv > _EXACT_VOL_TOL:
        disp = max(disp, _chunk_displacement(rem.Shape(), shape_b.wrapped))
    return {
        "added_volume": round(av, 2),
        "removed_volume": round(rv, 2),
        "added_centroid": _ctr(add.Shape()) if av > _EXACT_VOL_TOL else None,
        "removed_centroid": _ctr(rem.Shape()) if rv > _EXACT_VOL_TOL else None,
        "displacement": round(disp, 4),
    }


def compare_shapes(
    shape_a: Any, shape_b: Any, eps: float = 0.0, deadline: float | None = None
) -> dict:
    """Symmetric vertex-sampled surface diff, localized to significant changed regions,
    with an EXACT boolean magnitude in each region.

    eps<=0 (the default) auto-scales the move threshold to the mesh deflection. This
    is model-vs-input EDIT VERIFICATION, not a score: a true no-op reads
    max_deviation~0, and a tangential feature move (sliding a hole) is invisible to a
    surface-distance metric — see the warning emitted when no region is found.
    deadline (monotonic time): if set and too little remains after tessellation, the
    exact boolean is skipped for the (flagged) mesh estimate.
    """
    from scipy.spatial import cKDTree

    diag = max(_shape_diag(shape_a), _shape_diag(shape_b))
    if diag <= 0:
        return {
            "error": "degenerate shape (zero bounding box) — nothing to compare",
            "warnings": [],
        }
    deflection = max(diag * 1e-3, 1e-4)
    if eps <= 0:
        eps = max(deflection * _EPS_DEFLECTION_FACTOR, _EPS_FLOOR_MM)

    pts_a, tris_a = _tessellate_points(shape_a, deflection)
    pts_b, tris_b = _tessellate_points(shape_b, deflection)
    warnings: list[str] = []
    if not pts_a or not pts_b:
        return {
            "error": "could not tessellate one or both shapes for surface comparison",
            "warnings": warnings,
        }

    tree_a = cKDTree(pts_a)
    tree_b = cKDTree(pts_b)
    dist_a_to_b, _ = tree_b.query(pts_a, workers=-1)
    dist_b_to_a, _ = tree_a.query(pts_b, workers=-1)

    # Raw symmetric Hausdorff max — dominated by the independent-tessellation noise
    # tail (sharp edges/corners), so NOT the headline; reported for transparency.
    raw_surface_max = max(float(dist_a_to_b.max(initial=0.0)), float(dist_b_to_a.max(initial=0.0)))
    moved_a = {i for i, d in enumerate(dist_a_to_b) if float(d) > eps}
    moved_b = {i for i, d in enumerate(dist_b_to_a) if float(d) > eps}
    total_points = len(pts_a) + len(pts_b)

    min_span = max(deflection * _MIN_REGION_SPAN_FACTOR, _MIN_REGION_SPAN_FLOOR)
    regions = _regions(pts_a, tris_a, moved_a, dist_a_to_b, min_span) + _regions(
        pts_b, tris_b, moved_b, dist_b_to_a, min_span
    )
    regions.sort(key=lambda r: r["point_count"], reverse=True)
    moved_count = sum(r["point_count"] for r in regions)
    moved_fraction = round(moved_count / total_points, 4) if total_points else 0.0
    # Headline deviation is the largest REAL change (over significant regions), 0 when
    # nothing moved above the noise floor — so a no-op reads ~0, not the noise tail.
    region_max = max((r["max_deviation"] for r in regions), default=0.0)

    base = {
        "max_deviation": round(region_max, 4),
        "raw_surface_max": round(raw_surface_max, 4),
        "eps": round(eps, 4),
        "sample_points": total_points,
    }

    if not regions:
        warnings.append(
            f"No surface change above eps={round(eps, 4)} mm (the tessellation-resolution floor). "
            "EITHER the shapes match within mesh resolution, OR the edit is a TANGENTIAL move "
            "(e.g. relocating a hole/feature), which a surface-distance metric cannot detect. For "
            "move/relocation edits, confirm with the volume/bbox/center deltas and feature positions "
            "(find_holes), not this field."
        )
        return {
            **base,
            "changed": {
                "centroid": None,
                "bbox": None,
                "local_max_deviation": 0.0,
                "moved_fraction": 0.0,
            },
            "regions": [],
            "region_count": 0,
            "unchanged_elsewhere": True,
            "warnings": warnings,
        }

    primary = regions[0]  # the DOMINANT changed region — not a merged envelope across all changes
    centroids = [r["centroid"] for r in regions]
    region_spread = _bbox_diag(
        [min(c[k] for c in centroids) for k in range(3)],
        [max(c[k] for c in centroids) for k in range(3)],
    )
    unchanged_elsewhere = bool(region_spread <= diag * _LOCAL_SPREAD_FRAC)
    if not unchanged_elsewhere:
        warnings.append(
            "the changed regions are spread across the part, not confined to one area — the edit may "
            "have touched more than the requested feature; verify nothing unintended moved."
        )

    # Exact magnitude: an EXACT boolean diff clipped to the located region(s) replaces
    # the vertex-NN deviation, which inflates 10-100x on flat faces. Clipping to the
    # tight region keeps it in budget even on huge parts; fall back to the mesh
    # estimate (clearly flagged) only if the boolean fails.
    skip_exact = deadline is not None and time.monotonic() > deadline - _BOOL_RESERVE_S
    exact = None if skip_exact else _exact_region_magnitude(shape_a, shape_b, regions)
    if exact is not None:
        magnitude = exact["displacement"]
        magnitude_method = "exact_boolean"
    else:
        magnitude = primary["max_deviation"]
        magnitude_method = "mesh_estimate"
        why = (
            "skipped (insufficient op budget after tessellation)"
            if skip_exact
            else "unavailable (boolean failed)"
        )
        warnings.append(
            f"exact boolean magnitude {why}; max_deviation is a vertex-mesh estimate that can "
            "overstate the true surface displacement on flat faces — cross-check the volume/bbox deltas."
        )

    return {
        **base,
        "max_deviation": round(magnitude, 4),
        "magnitude_method": magnitude_method,
        "changed": {
            "centroid": primary["centroid"],
            "bbox": primary["bbox"],
            "local_max_deviation": round(magnitude, 4),
            "moved_fraction": moved_fraction,
            "added_volume": exact["added_volume"] if exact else None,
            "removed_volume": exact["removed_volume"] if exact else None,
            "added_centroid": exact["added_centroid"] if exact else None,
            "removed_centroid": exact["removed_centroid"] if exact else None,
        },
        "regions": regions[:5],
        "region_count": len(regions),
        "unchanged_elsewhere": unchanged_elsewhere,
        "warnings": warnings,
    }


def main(a_step: str, b_step: str, out_json: str, eps: str, budget_s: str = "0") -> None:
    from build123d import import_step

    t0 = time.monotonic()
    deadline = t0 + float(budget_s) if float(budget_s) > 0 else None
    try:
        result = compare_shapes(
            import_step(a_step), import_step(b_step), float(eps), deadline=deadline
        )
    except Exception as exc:  # noqa: BLE001 - convert worker failures to structured JSON
        result = {"error": f"{type(exc).__name__}: {exc}"}
    with open(out_json, "w") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main(*sys.argv[1:6])
