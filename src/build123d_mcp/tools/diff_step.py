"""STEP revision diff with an index-independent analytic face supplement."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

from build123d_mcp.tools._recognition import resolve_analysis, transform_vector
from build123d_mcp.tools.inspect_features import compact

_MAX_ROWS = 50


def _signature(face):
    # Surface origins are parameterisation choices. For rotational surfaces,
    # compare the axis line's perpendicular origin rather than its axial gauge.
    row = {k: v for k, v in face.items() if k != "id"}
    if "origin" in row and "direction" in row:
        axis, origin = row["direction"], row["origin"]
        dot = sum(a * b for a, b in zip(axis, origin))
        row["origin"] = [p - dot * a for p, a in zip(origin, axis)]

    def rounded(value):
        if isinstance(value, float):
            return round(value, 5) or 0.0
        if isinstance(value, list):
            return [rounded(v) for v in value]
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        return value

    return json.dumps(rounded(row), sort_keys=True)


def face_diff(old: dict, new: dict, report: dict) -> dict:
    """Match multisets of analytic signatures before pairing changed planes.

    This is analytic descriptor evidence, not an assertion of B-rep equality:
    surfaces with identical descriptors can still have different trim contours.
    """
    buckets = defaultdict(list)
    for face in new["faces"]:
        buckets[_signature(face)].append(face)
    unmatched_old = []
    unchanged = 0
    for face in old["faces"]:
        matches = buckets[_signature(face)]
        if matches:
            matches.pop()
            unchanged += 1
        else:
            unmatched_old.append(face)
    unmatched_new = [f for group in buckets.values() for f in group]
    claimed = []
    for side, document in (("old", old), ("new", new)):
        ids = {f.get(side) for f in report.get("features", [])}
        claimed.append({i for f in document["features"] if f["id"] in ids for i in f["faces"]})
    before = [f for f in unmatched_old if f["id"] not in claimed[0]]
    after = [f for f in unmatched_new if f["id"] not in claimed[1]]

    def distance(a, b):
        if a["kind"] != "plane" or b["kind"] != "plane":
            return math.inf
        if sum(x * y for x, y in zip(a["direction"], b["direction"])) < 1 - 1e-8:
            return math.inf
        return math.dist(a["centroid"], b["centroid"]) + abs(
            math.sqrt(a["area"]) - math.sqrt(b["area"])
        )

    def nearest(face, candidates):
        ranked = sorted((distance(face, f), i) for i, f in enumerate(candidates))
        if not ranked or not math.isfinite(ranked[0][0]):
            return None
        if len(ranked) > 1 and abs(ranked[1][0] - ranked[0][0]) < 1e-5:
            return None  # ambiguous pairing is reported as removed/added instead
        return ranked[0][1]

    paired_old, paired_new, planar = set(), set(), []
    for i, face in enumerate(before):
        j = nearest(face, after)
        if j is None or nearest(after[j], before) != i:
            continue
        other = after[j]
        delta = [b - a for a, b in zip(face["centroid"], other["centroid"])]
        offset = sum(d * n for d, n in zip(delta, face["direction"]))
        planar.append(
            {
                "old": compact(face),
                "new": compact(other),
                "normal_offset": round(offset, 6),
                "tangential_displacement": compact(
                    [d - offset * n for d, n in zip(delta, face["direction"])]
                ),
                "area_delta": round(other["area"] - face["area"], 6),
            }
        )
        paired_old.add(i)
        paired_new.add(j)
    removed = [compact(f) for i, f in enumerate(before) if i not in paired_old]
    added = [compact(f) for i, f in enumerate(after) if i not in paired_new]
    return {
        "unchanged_faces": unchanged,
        "planar_deltas": planar[:_MAX_ROWS],
        "planar_deltas_count": len(planar),
        "unclaimed_face_changes": {
            "removed": removed[:_MAX_ROWS],
            "added": added[:_MAX_ROWS],
            "removed_count": len(removed),
            "added_count": len(added),
        },
        "truncated": any(len(rows) > _MAX_ROWS for rows in (planar, removed, added)),
        "basis": "analytic descriptors (kind, area, centroid and surface parameters); trim contours are not compared",
    }


def _bounded(value, budget=None):
    """Strip every verdict and bound all upstream collections/strings recursively."""
    if budget is None:
        budget = [1800]
    budget[0] -= 1
    if budget[0] < 0:
        return {"omitted": True}
    if isinstance(value, dict):
        out = {k: _bounded(v, budget) for k, v in value.items() if k != "verdict"}
        return out
    if isinstance(value, list):
        out = [_bounded(v, budget) for v in value[:_MAX_ROWS]]
        if len(value) > _MAX_ROWS:
            out.append({"omitted_count": len(value) - _MAX_ROWS})
        return out
    if isinstance(value, str):
        return value[:1000]
    return compact(value)


def _render_overlay(old: bytes, new: bytes, timeout: float) -> bytes:
    # 0.1.15 has no Python diff renderer. Use its own shipped CLI, which owns
    # the orange/green/red overlay. Do not substitute our render_view renderer.
    with tempfile.TemporaryDirectory(prefix="build123d_diff_") as directory:
        for name, data in (("old", old), ("new", new)):
            with open(os.path.join(directory, name + ".step"), "wb") as stream:
                stream.write(data)
        output = os.path.join(directory, "overlay")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "specify_core",
                "diff",
                os.path.join(directory, "old.step"),
                os.path.join(directory, "new.step"),
                "-o",
                output,
            ],
            capture_output=True,
            timeout=timeout,
        )
        image = os.path.join(output, "model.png")
        if not os.path.isfile(image):
            raise ValueError(
                "specify-core diff produced no model.png; rendering blocked on "
                "specify-core#180 item 4: " + result.stderr.decode(errors="replace")[-500:]
            )
        with open(image, "rb") as stream:
            return stream.read()


def diff_step(session, old: str, new: str, render: bool = False) -> str | dict:
    """Compare STEP paths or session objects with specify-core and analytic faces.

    Reports adapted/orphaned/new features plus bare plane offsets and tangential
    moves. Counts are exact; expanded lists are bounded. render=True includes
    specify-core's model.png overlay, via its CLI in 0.1.15.
    """
    try:
        from specify_core import api

        before = resolve_analysis(session, old)
        after = resolve_analysis(session, new)
        report = api.diff(before.loaded, after.loaded)["report"]
        before._measures = before.caller_measures(report["measures"]["old"])
        after._measures = after.caller_measures(report["measures"]["new"])
        report["measures"] = {"old": before._measures, "new": after._measures}
        for feature in report.get("features", []):
            source = after if feature.get("class") == "new" else before
            if isinstance(feature.get("at"), list):
                feature["at"] = transform_vector(feature["at"], source.placement, point=True)
        # place is upstream's product-frame prose; omit it when placement is
        # non-identity rather than returning misleading caller-frame labels.
        identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
        if before.placement != identity or after.placement != identity:
            for feature in report.get("features", []):
                feature.pop("place", None)
        # Exclude PMI workflow prose/answers: they are not CAD edit acceptance
        # criteria. Keep structural feature changes, measures and PMI facts.
        result = _bounded(
            {k: report[k] for k in ("features", "measures", "pmi", "problems") if k in report}
        )
        result["feature_changes_count"] = len(report.get("features", []))
        result.update(face_diff(before.document, after.document, report))
        text = json.dumps(result, separators=(",", ":"))
        if render:
            return {
                "report": text,
                "png": _render_overlay(before.step, after.step, session.exec_timeout),
            }
        return text
    except Exception as exc:
        error = json.dumps({"error": str(exc)[:1500]})
        return {"report": error} if render else error
