"""Out-of-process localized surface comparison for ``shape_compare``.

Run as ``python -m build123d_mcp._shape_compare_subprocess <a.step> <b.step>
<out.json> <eps>``. The two STEP files are imported, tessellated, and compared
with a symmetric nearest-neighbour surface distance. This is isolated because
OCC tessellation is an un-interruptible native call; the parent bounds this
process with ``subprocess.run(timeout=...)`` so the worker session survives a
large or pathological part.
"""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from typing import Any


def _round_pt(p: list[float] | tuple[float, float, float], digits: int = 4) -> list[float]:
    return [round(float(p[0]), digits), round(float(p[1]), digits), round(float(p[2]), digits)]


def _bbox_diag(mins: list[float], maxs: list[float]) -> float:
    return math.dist(mins, maxs)


def _shape_diag(shape: Any) -> float:
    bb = shape.bounding_box()
    return math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))


def _tessellate_points(shape: Any) -> tuple[list[tuple[float, float, float]], list[list[int]]]:
    diag = _shape_diag(shape)
    if diag <= 0:
        return [], []
    verts, tris = shape.tessellate(max(diag * 1e-3, 1e-4))
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


def compare_shapes(shape_a: Any, shape_b: Any, eps: float = 0.01) -> dict:
    """Return a symmetric vertex-sampled Hausdorff diff with a localized region."""
    from scipy.spatial import cKDTree

    pts_a, tris_a = _tessellate_points(shape_a)
    pts_b, tris_b = _tessellate_points(shape_b)
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

    max_deviation = max(float(dist_a_to_b.max(initial=0.0)), float(dist_b_to_a.max(initial=0.0)))
    moved_a = {i for i, d in enumerate(dist_a_to_b) if float(d) > eps}
    moved_b = {i for i, d in enumerate(dist_b_to_a) if float(d) > eps}
    moved_points = [pts_a[i] for i in moved_a] + [pts_b[i] for i in moved_b]
    total_points = len(pts_a) + len(pts_b)

    if not moved_points:
        return {
            "max_deviation": round(max_deviation, 4),
            "changed": {
                "centroid": None,
                "bbox": None,
                "local_max_deviation": 0.0,
                "moved_fraction": 0.0,
            },
            "unchanged_elsewhere": True,
            "eps": eps,
            "sample_points": total_points,
            "warnings": warnings,
        }

    mins = [min(p[i] for p in moved_points) for i in range(3)]
    maxs = [max(p[i] for p in moved_points) for i in range(3)]
    centroid = [sum(p[i] for p in moved_points) / len(moved_points) for i in range(3)]

    comps_a = _moved_components(pts_a, tris_a, moved_a)
    comps_b = _moved_components(pts_b, tris_b, moved_b)
    component_count = len(comps_a) + len(comps_b)
    moved_count = len(moved_points)
    shape_diag = max(_shape_diag(shape_a), _shape_diag(shape_b))
    changed_diag = _bbox_diag(mins, maxs)

    # This is a locality check, not a score. A single edit can appear as two
    # moved patches (old and new feature positions), so use both clustering and
    # spatial extent to flag obvious "also changed elsewhere" cases.
    changed_is_spatially_local = shape_diag <= 0 or changed_diag <= max(eps * 4, shape_diag * 0.55)
    unchanged_elsewhere = bool(changed_is_spatially_local)

    return {
        "max_deviation": round(max_deviation, 4),
        "changed": {
            "centroid": _round_pt(centroid),
            "bbox": [_round_pt(mins), _round_pt(maxs)],
            "local_max_deviation": round(max_deviation, 4),
            "moved_fraction": round(moved_count / total_points, 4),
            "component_count": component_count,
        },
        "unchanged_elsewhere": unchanged_elsewhere,
        "eps": eps,
        "sample_points": total_points,
        "warnings": warnings,
    }


def main(a_step: str, b_step: str, out_json: str, eps: str) -> None:
    from build123d import import_step

    try:
        result = compare_shapes(import_step(a_step), import_step(b_step), float(eps))
    except Exception as exc:  # noqa: BLE001 - convert worker failures to structured JSON
        result = {"error": f"{type(exc).__name__}: {exc}"}
    with open(out_json, "w") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
