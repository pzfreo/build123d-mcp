"""Conservative, stateless transactional edits of imported solids."""

from __future__ import annotations

import json
import math

from build123d_mcp.tools._recognition import build_evidence
from build123d_mcp.tools.measure import _resolve_shape


def _solid_from_occt(shape):
    """Reject mixed defeaturing results instead of silently taking one solid."""
    from build123d import Solid
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Iterator

    from build123d_mcp._ocp_compat import to_solid

    if shape.ShapeType() == TopAbs_SOLID:
        return Solid(to_solid(shape))
    if shape.ShapeType() != TopAbs_COMPOUND:
        raise ValueError("defeaturing did not produce a solid")
    solids = []
    stack = [shape]
    while stack:
        iterator = TopoDS_Iterator(stack.pop())
        while iterator.More():
            child = iterator.Value()
            if child.ShapeType() == TopAbs_SOLID:
                solids.append(Solid(to_solid(child)))
            elif child.ShapeType() == TopAbs_COMPOUND:
                stack.append(child)
            else:
                raise ValueError("defeaturing produced mixed topology")
            iterator.Next()
    if len(solids) != 1:
        raise ValueError(f"defeaturing produced {len(solids)} solids, expected one")
    return solids[0]


def _hole_records(evidence) -> list[dict]:
    return [
        evidence.record(feature).to_dict()
        for feature in evidence.features
        if evidence.family(feature) == "holes"
    ]


def _same_holes(expected: list[dict], actual: list[dict]) -> bool:
    """Match occurrences, including duplicates, with small recognition tolerance."""
    if len(expected) != len(actual):
        return False
    unused = actual.copy()
    for old in expected:
        match = next(
            (
                new
                for new in unused
                if old["bottom"] == new["bottom"]
                and abs(old["diameter"] - new["diameter"]) <= 0.01
                and abs(old["depth"] - new["depth"]) <= 0.01
                and all(abs(a - b) <= 0.01 for a, b in zip(old["location"], new["location"]))
                and all(abs(a - b) <= 0.001 for a, b in zip(old["axis"], new["axis"]))
                and old["cbore"] == new["cbore"]
                and old["spotface"] == new["spotface"]
                and old["csink"] == new["csink"]
            ),
            None,
        )
        if match is None:
            return False
        unused.remove(match)
    return True


def _bounds(shape) -> tuple[float, ...]:
    box = shape.bounding_box()
    return (*box.min, *box.max)


def _annulus_bounds(opening: list[float], axis: list[float], depth: float, radius: float):
    exit_point = [opening[i] + axis[i] * depth for i in range(3)]
    radial = [radius * math.sqrt(max(0.0, 1 - axis[i] ** 2)) for i in range(3)]
    return tuple(min(opening[i], exit_point[i]) - radial[i] for i in range(3)) + tuple(
        max(opening[i], exit_point[i]) + radial[i] for i in range(3)
    )


def edit_feature(
    session,
    feature_id: str,
    diameter: float,
    object_name: str = "",
    result_name: str = "",
    protected_refs: str = "[]",
) -> str:
    """Resize one plain through hole, checking predicted change before commit.

    Only a single cylindrical wall without a counterbore, spotface or
    countersink is accepted. Other feature edits require a different geometric
    contract; silently guessing their dependent geometry would be unsafe.
    """
    try:
        if (
            isinstance(diameter, bool)
            or not isinstance(diameter, (int, float))
            or not math.isfinite(diameter)
            or diameter <= 0
        ):
            raise ValueError("diameter must be a positive finite number")
        protected = json.loads(protected_refs)
        if not isinstance(protected, list) or not all(isinstance(ref, str) for ref in protected):
            raise ValueError("protected_refs must be a JSON list of feature ids")
        source = _resolve_shape(session, object_name)
        source_name = object_name or "@current"
        evidence = build_evidence(source, session)
        if feature_id not in evidence.features:
            raise ValueError("unknown or stale feature id; inspect_features() on the current shape")
        feature = feature_id
        if evidence.family(feature) != "holes":
            raise ValueError("edit_feature currently supports only plain through holes")
        if not source.is_valid or len(source.solids()) != 1:
            raise ValueError("source must be one valid solid")
        for ref in protected:
            if ref not in evidence.features or evidence.family(ref) != "holes":
                raise ValueError(f"protected feature id {ref!r} is unknown or not a hole")
        if feature_id in protected:
            raise ValueError("target hole is protected; remove it from protected_refs to edit it")
        record = evidence.record(feature).to_dict()
        faces = evidence.constituent_faces(feature)
        if (
            record["bottom"] != "through"
            or record["cbore"] is not None
            or record["spotface"] is not None
            or record["csink"] is not None
            or len(faces) != 1
        ):
            raise ValueError("hole has dependent or non-cylindrical geometry; edit refused")
        # Verify the STEP index against live analytic geometry even for enlargements.
        for face_id in faces:
            evidence.face(face_id)
        if abs(diameter - record["diameter"]) <= 1e-6:
            raise ValueError("new diameter equals the current diameter")

        from build123d import Align, Cylinder, Plane

        axis = record["axis"]
        opening = record["location"]
        depth = record["depth"]
        old_radius = record["diameter"] / 2
        new_radius = diameter / 2
        expected_delta = math.pi * (old_radius**2 - new_radius**2) * depth
        if diameter > record["diameter"]:
            base = source
        else:
            from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing

            remover = BRepAlgoAPI_Defeaturing()
            remover.SetShape(source.wrapped)
            remover.AddFaceToRemove(evidence.face(next(iter(faces))).wrapped)
            remover.Build()
            if not remover.IsDone():
                raise ValueError("defeaturing the old bore failed")
            base = _solid_from_occt(remover.Shape())
            filled = base.volume - source.volume
            expected_fill = math.pi * old_radius**2 * depth
            if abs(filled - expected_fill) > max(0.01, expected_fill * 0.001):
                raise ValueError("defeaturing changed material beyond the old bore")
        # Start outside the opening, but stop at the recorded exit exactly. This
        # avoids cutting into a far wall after a through hole opens into a cavity.
        lead = min(0.01, depth * 0.001)
        start = tuple(opening[i] - axis[i] * lead for i in range(3))
        cutter = Plane(origin=start, z_dir=axis) * Cylinder(
            new_radius, depth + lead, align=(Align.CENTER, Align.CENTER, Align.MIN)
        )
        candidate = base - cutter
        if not candidate.is_valid or len(candidate.solids()) != 1:
            raise ValueError("edited shape is invalid or not a single solid")
        tolerance = max(0.01, abs(expected_delta) * 0.001)
        actual_delta = candidate.volume - source.volume
        if abs(actual_delta - expected_delta) > tolerance:
            raise ValueError(
                f"volume mismatch: predicted {expected_delta:.6f}, measured {actual_delta:.6f} mm³"
            )
        expected_added = max(expected_delta, 0.0)
        expected_removed = max(-expected_delta, 0.0)
        added_shape = candidate - source
        removed_shape = source - candidate
        added = added_shape.volume
        removed = removed_shape.volume
        if abs(added - expected_added) > tolerance or abs(removed - expected_removed) > tolerance:
            raise ValueError("changed material differs from the predicted hole annulus")
        predicted_bounds = _annulus_bounds(opening, axis, depth, max(old_radius, new_radius))
        measured_bounds = _bounds(added_shape if expected_added else removed_shape)
        if any(abs(a - b) > 0.02 for a, b in zip(predicted_bounds, measured_bounds)):
            raise ValueError("changed region differs from the predicted hole annulus")
        if any(abs(a - b) > 0.01 for a, b in zip(_bounds(source), _bounds(candidate))):
            raise ValueError("outer envelope moved during hole edit")
        after = build_evidence(candidate, session)
        before_holes = _hole_records(evidence)
        before_holes.remove(record)
        after_holes = _hole_records(after)
        changed = [hole for hole in after_holes if abs(hole["diameter"] - diameter) <= 0.01]
        if not changed:
            raise ValueError("resized hole was not recognised in the result")
        replacement = next(
            (
                hole
                for hole in changed
                if all(abs(a - b) <= 0.01 for a, b in zip(hole["location"], opening))
                and abs(hole["depth"] - depth) <= 0.01
                and hole["bottom"] == "through"
                and all(abs(a - b) <= 0.001 for a, b in zip(hole["axis"], axis))
            ),
            None,
        )
        if replacement is None:
            raise ValueError("resized hole moved or changed depth")
        after_holes.remove(replacement)
        if not _same_holes(before_holes, after_holes):
            raise ValueError("another hole or mounting interface changed")

        name = result_name or (source_name if source_name != "@current" else "edited")
        if name != source_name and name in session.objects:
            raise ValueError(f"result_name {name!r} already exists")
        session.namespace["show"](candidate, name)
        return json.dumps(
            {
                "result_name": name,
                "feature_id": feature_id,
                "predicted": {
                    "added_volume": round(expected_added, 6),
                    "removed_volume": round(expected_removed, 6),
                },
                "measured": {"added_volume": round(added, 6), "removed_volume": round(removed, 6)},
                "predicted_region_bbox": [round(v, 6) for v in predicted_bounds],
                "measured_region_bbox": [round(v, 6) for v in measured_bounds],
                "other_holes_unchanged": True,
                "outer_envelope_unchanged": True,
                "protected_refs_checked": protected,
                "committed": True,
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001 - OCP failures must leave session state untouched
        return json.dumps({"error": str(exc), "committed": False}, indent=2)
