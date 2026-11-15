"""STEP revision diff with an index-independent analytic face supplement."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
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


def _feature_hosts(analysis, report, side):
    if analysis is None:
        return {}
    changed = {f.get(side) for f in report.get("features", [])}
    hosts = defaultdict(set)
    for feature in analysis.document["features"]:
        if feature["id"] not in changed:
            continue
        for index in feature["faces"]:
            for neighbor in analysis.adjacency.get(index, ()):
                hosts[neighbor].add(analysis.bind_id(feature["id"]))
    return hosts


def _translated_boundary(before, old_id, after, new_id, delta):
    """An area centroid shift is motion only if the whole boundary follows it."""
    if before is None or after is None:
        return False
    old_face, new_face = before.mapped_faces.get(old_id), after.mapped_faces.get(new_id)
    if old_face is None or new_face is None:
        return False

    def signature(face, shift):
        def point(value):
            return tuple(round(float(v) + d, 5) for v, d in zip(value, shift))

        rows = []
        for edge in face.edges():
            kind = str(edge.geom_type).rsplit(".", 1)[-1]
            if kind == "CIRCLE" and edge.is_closed:
                geometry = (point(edge.arc_center), round(edge.radius, 5))
            else:
                geometry = (
                    *sorted((point(edge.position_at(0)), point(edge.position_at(1)))),
                    point(edge.position_at(0.5)),
                )
            rows.append((kind, round(edge.length, 5), geometry))
        return sorted(rows)

    return signature(old_face, delta) == signature(new_face, (0, 0, 0))


def face_diff(
    old: dict, new: dict, report: dict, before_geometry=None, after_geometry=None
) -> dict:
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

    old_hosts = _feature_hosts(before_geometry, report, "old")
    new_hosts = _feature_hosts(after_geometry, report, "new")
    paired_old, paired_new, planar = set(), set(), []
    boundary_changes, modified = [], []
    for i, face in enumerate(before):
        j = nearest(face, after)
        if j is None or nearest(after[j], before) != i:
            continue
        other = after[j]
        delta = [b - a for a, b in zip(face["centroid"], other["centroid"])]
        offset = sum(d * n for d, n in zip(delta, face["direction"]))
        area_delta = other["area"] - face["area"]
        explaining = sorted(old_hosts.get(face["id"], set()) | new_hosts.get(other["id"], set()))
        translated = (
            abs(area_delta) <= 1e-5
            and math.dist(delta, (0, 0, 0)) > 1e-5
            and _translated_boundary(
                before_geometry, face["id"], after_geometry, other["id"], delta
            )
        )
        if abs(offset) <= 1e-5 and explaining and not translated:
            boundary_changes.append(
                {
                    "face_id": face["id"],
                    "new_face_id": other["id"],
                    "area_delta": round(area_delta, 6),
                    "explaining_feature_ids": explaining,
                }
            )
        elif abs(offset) <= 1e-5 and (abs(area_delta) > 1e-5 or not translated):
            modified.append(
                {
                    "old": compact(face),
                    "new": compact(other),
                    "reason": "boundary change without verified feature adjacency",
                }
            )
        else:
            planar.append(
                {
                    "old": compact(face),
                    "new": compact(other),
                    "normal_offset": round(offset, 6),
                    "tangential_displacement": compact(
                        [d - offset * n for d, n in zip(delta, face["direction"])]
                    ),
                    "area_delta": round(area_delta, 6),
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
        "feature_boundary_changes": boundary_changes[:_MAX_ROWS],
        "feature_boundary_changes_count": len(boundary_changes),
        "unclaimed_face_changes": {
            "modified": modified[:_MAX_ROWS],
            "modified_count": len(modified),
            "removed": removed[:_MAX_ROWS],
            "added": added[:_MAX_ROWS],
            "removed_count": len(removed),
            "added_count": len(added),
        },
        "truncated": any(
            len(rows) > _MAX_ROWS for rows in (planar, removed, added, boundary_changes, modified)
        ),
        "basis": "analytic descriptors (kind, area, centroid and surface parameters); trim contours are not compared",
    }


def _node_count(value):
    if isinstance(value, dict):
        return 1 + sum(_node_count(v) for k, v in value.items() if k != "verdict")
    if isinstance(value, list):
        return 1 + sum(_node_count(v) for v in value)
    return 1


def _bounded(value, budget=1800):
    """A fresh budget per top-level key; every omitted subtree is counted.

    Strings carry explicit character counts when shortened. List/dict omissions
    count immediate entries; a whole omitted subtree counts all of its nodes.
    Measures and small independent sections can never be consumed by features.
    """
    remaining = [budget]

    def visit(item):
        if remaining[0] <= 0:
            return {"omitted": _node_count(item), "unit": "nodes"}
        remaining[0] -= 1
        if isinstance(item, dict):
            out = {}
            entries = [(k, v) for k, v in item.items() if k != "verdict"]
            for i, (key, child) in enumerate(entries):
                if remaining[0] <= 0:
                    out["omitted"] = len(entries) - i
                    out["omitted_unit"] = "fields"
                    break
                out[key] = visit(child)
            return out
        if isinstance(item, list):
            out = []
            for i, child in enumerate(item):
                if i >= _MAX_ROWS or remaining[0] <= 0:
                    out.append({"omitted": len(item) - i, "unit": "items"})
                    break
                out.append(visit(child))
            return out
        if isinstance(item, str) and len(item) > 1000:
            return {"text": item[:1000], "omitted": len(item) - 1000, "unit": "characters"}
        return compact(item)

    return visit(value)


def _caller_changes(changes, before, after):
    out = {}
    for key, item in changes.items():
        if key.startswith("sizes.") or key in {"bottom", "cbore", "spotface", "csink"}:
            out[key] = item
        elif key in {
            "position",
            "location",
            "center",
            "origin",
            "at",
            "axis",
            "direction",
            "normal",
        }:
            if isinstance(item, dict) and all(
                isinstance(item.get(side), list) and len(item[side]) == 3 for side in ("old", "new")
            ):
                out[key] = {
                    side: transform_vector(
                        item[side],
                        source.placement,
                        point=key not in {"axis", "direction", "normal"},
                    )
                    for side, source in (("old", before), ("new", after))
                }
    return out


def _bind_references(value, before, after, side="new"):
    if isinstance(value, dict):
        return {
            key: _bind_references(item, before, after, key if key in {"old", "new"} else side)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_bind_references(item, before, after, side) for item in value]
    if isinstance(value, str):
        source = before if side == "old" else after
        if value in {f["id"] for f in source.document["features"]}:
            return source.bind_id(value)
    return value


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
    specify-core's model.png overlay, via its CLI in 0.1.15. A registered name
    wins over an identical path string. Feature ids include their STEP digest.
    """
    try:
        from specify_core import api

        deadline = time.monotonic() + session.exec_timeout
        before = resolve_analysis(session, old)
        after = resolve_analysis(session, new)
        report = api.diff(before.loaded, after.loaded)["report"]
        report["measures"] = {"old": before.measures, "new": after.measures}
        for feature in report.get("features", []):
            if "changes" in feature:
                feature["changes"] = _caller_changes(feature["changes"], before, after)
            source = after if feature.get("class") == "new" else before
            if isinstance(feature.get("at"), list):
                feature["at"] = transform_vector(feature["at"], source.placement, point=True)
            old_feature = next(
                (f for f in before.document["features"] if f["id"] == feature.get("old")), None
            )
            new_feature = next(
                (f for f in after.document["features"] if f["id"] == feature.get("new")), None
            )
            if old_feature and new_feature:
                for field in ("location", "axis"):
                    a, b = old_feature["record"].get(field), new_feature["record"].get(field)
                    if isinstance(a, list) and isinstance(b, list) and math.dist(a, b) > 1e-5:
                        feature.setdefault("changes", {})["caller." + field] = {"old": a, "new": b}
        # place is upstream's product-frame prose; omit it when placement is
        # non-identity rather than returning misleading caller-frame labels.
        identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
        if before.placement != identity or after.placement != identity:
            for feature in report.get("features", []):
                feature.pop("place", None)
        # Exclude PMI workflow prose/answers: they are not CAD edit acceptance
        # criteria. Keep structural feature changes, measures and PMI facts.
        result = {
            key: _bounded(_bind_references(report[key], before, after))
            for key in ("features", "measures", "pmi", "problems")
            if key in report
        }
        result["feature_changes_count"] = len(report.get("features", []))
        shown = sum("class" in f for f in result.get("features", []))
        if shown != result["feature_changes_count"]:
            result["features_truncated"] = {
                "shown": shown,
                "total": result["feature_changes_count"],
            }
        result.update(face_diff(before.document, after.document, report, before, after))
        text = json.dumps(result, separators=(",", ":"))
        if render:
            return {
                "report": text,
                "png": _render_overlay(
                    before.step, after.step, max(0.1, deadline - time.monotonic() - 1)
                ),
            }
        return text
    except Exception as exc:
        error = json.dumps({"error": str(exc)[:1500]})
        return {"report": error} if render else error
