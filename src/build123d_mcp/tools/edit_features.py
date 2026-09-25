"""Conservative feature selection and transactional edits of imported solids."""

from __future__ import annotations

import json
import math

from build123d_mcp.tools.measure import _resolve_shape
from build123d_mcp.tools.recognise_features import recognise_features


def _report(session, object_name: str, family: str) -> dict:
    report = json.loads(
        recognise_features(session, object_name=object_name, families=family, max_features=100)
    )
    if "error" in report:
        raise ValueError(report["error"])
    if report["truncated"]:
        raise ValueError("feature inventory exceeds 100 records; enumeration would be incomplete")
    return report


def _families(kind: str) -> tuple[str, ...]:
    names = {
        "hole": ("holes",),
        "bore": ("holes",),
        "boss": ("bosses", "polygonal_bosses"),
        "polygonal_boss": ("polygonal_bosses",),
        "slot": ("slots",),
        "chamfer": ("chamfers",),
        "fillet": ("fillets",),
    }
    try:
        return names[kind.strip().lower().replace(" ", "_")]
    except KeyError as exc:
        raise ValueError(
            f"unsupported feature kind {kind!r}; choose one of {sorted(names)}"
        ) from exc


def _measured_value(family: str, record: dict) -> tuple[str | None, float | None]:
    choices = {
        "holes": ("diameter",),
        "bosses": ("diameter", "height"),
        "polygonal_bosses": ("across_flats", "diameter", "height"),
        "slots": ("width", "length"),
        "chamfers": ("size", "width"),
        "fillets": ("radius",),
    }
    for key in choices[family]:
        value = record.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return key, float(value)
    return None, None


def _axis(record: dict) -> list[float] | None:
    axis = record.get("axis")
    if isinstance(axis, str) and axis.lower() in {"x", "y", "z"}:
        return [float(axis.lower() == name) for name in "xyz"]
    if isinstance(axis, (list, tuple)) and len(axis) == 3:
        return [float(v) for v in axis]
    return None


def _axis_matches(axis: list[float] | None, requested: str, swapped: bool) -> bool:
    if not requested:
        return True
    if axis is None:
        return False
    requested = requested.upper()
    if requested not in {"X", "Y", "Z"}:
        raise ValueError("qualifiers.axis must be X, Y, or Z")
    index = {"X": 0, "Y": 2 if swapped else 1, "Z": 1 if swapped else 2}[requested]
    return abs(axis[index]) >= 0.98


def _side_matches(record: dict, requested: str, swapped: bool, midpoint: tuple) -> bool:
    if not requested:
        return True
    point = record.get("location", record.get("center"))
    if not isinstance(point, (list, tuple)) or len(point) != 3:
        return False
    axis = requested[1]
    index = {"X": 0, "Y": 2 if swapped else 1, "Z": 1 if swapped else 2}[axis]
    distance = point[index] - midpoint[index]
    return distance > 0.01 if requested[0] == "+" else distance < -0.01


def find_candidates(
    session,
    kind: str,
    qualifiers: str = "{}",
    stated_value: float | None = None,
    object_name: str = "",
) -> str:
    """Enumerate recognised instances under literal and Y/Z-swapped axis readings.

    ``qualifiers`` is JSON with optional ``axis``, ``side`` and ``value_field``. No guess
    is made when recognition or a stated-value check fails.
    """
    try:
        families = _families(kind)
        filters = json.loads(qualifiers)
        if not isinstance(filters, dict) or set(filters) - {"axis", "side", "value_field"}:
            raise ValueError("qualifiers must be a JSON object with axis, side and/or value_field")
        requested_axis = filters.get("axis", "")
        if not isinstance(requested_axis, str):
            raise ValueError("qualifiers.axis must be a string")
        if requested_axis and requested_axis.upper() not in {"X", "Y", "Z"}:
            raise ValueError("qualifiers.axis must be X, Y, or Z")
        requested_side = filters.get("side", "")
        if not isinstance(requested_side, str) or (
            requested_side and requested_side.upper() not in {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}
        ):
            raise ValueError("qualifiers.side must be +X, -X, +Y, -Y, +Z, or -Z")
        requested_side = requested_side.upper()
        value_field = filters.get("value_field")
        if value_field is not None and not isinstance(value_field, str):
            raise ValueError("qualifiers.value_field must be a string")
        if stated_value is not None and (
            isinstance(stated_value, bool)
            or not isinstance(stated_value, (int, float))
            or not math.isfinite(stated_value)
            or stated_value <= 0
        ):
            raise ValueError("stated_value must be a positive finite number")
        reports = [_report(session, object_name, family) for family in families]
        box = _resolve_shape(session, object_name).bounding_box()
        midpoint = tuple((a + b) / 2 for a, b in zip(box.min, box.max))
        candidates = []
        for feature in (item for report in reports for item in report["features"]):
            family = feature["family"]
            record = feature["record"]
            field, value = _measured_value(family, record)
            if value_field is not None:
                field = value_field
                raw = record.get(field)
                value = float(raw) if isinstance(raw, (int, float)) else None
            axis = _axis(record)
            tolerance = max(0.1, 0.01 * stated_value) if stated_value is not None else None
            value_matches = stated_value is None or (
                value is not None and abs(value - stated_value) <= tolerance
            )
            candidates.append(
                {
                    "ref": feature["ref"],
                    "family": family,
                    "record": record,
                    "measured_field": field,
                    "measured_value": value,
                    "value_matches": value_matches,
                    "axis": axis,
                    "literal_axis_matches": _axis_matches(axis, requested_axis, False),
                    "yz_swapped_axis_matches": _axis_matches(axis, requested_axis, True),
                    "literal_side_matches": _side_matches(record, requested_side, False, midpoint),
                    "yz_swapped_side_matches": _side_matches(
                        record, requested_side, True, midpoint
                    ),
                }
            )
        return json.dumps(
            {
                "kind": kind,
                "count": len(candidates),
                "candidates": candidates,
                "literal_matches": [
                    c["ref"]
                    for c in candidates
                    if c["value_matches"]
                    and c["literal_axis_matches"]
                    and c["literal_side_matches"]
                ],
                "yz_swapped_matches": [
                    c["ref"]
                    for c in candidates
                    if c["value_matches"]
                    and c["yz_swapped_axis_matches"]
                    and c["yz_swapped_side_matches"]
                ],
                "stated_value_unmatched": stated_value is not None
                and not any(c["value_matches"] for c in candidates),
                "recognition_only": True,
            },
            indent=2,
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": str(exc)}, indent=2)


def interface_features(session, object_name: str = "") -> str:
    """Suggest planar mounting faces from recognised hole openings.

    These are candidates rather than claims about mating intent. An edit gate
    must compare their geometry, not assume every large flat face is an interface.
    """
    try:
        shape = _resolve_shape(session, object_name)
        report = _report(session, object_name, "holes")
        rows = []
        for index, face in enumerate(shape.faces()):
            if str(face.geom_type).split(".")[-1] != "PLANE":
                continue
            normal = tuple(face.normal_at())
            center = tuple(face.center())
            opening_edges = [
                (tuple(edge.arc_center), edge.radius)
                for edge in face.edges()
                if str(edge.geom_type).split(".")[-1] == "CIRCLE"
            ]
            holes = []
            for feature in report["features"]:
                record = feature["record"]
                opening = record["location"]
                offset = sum((opening[i] - center[i]) * normal[i] for i in range(3))
                axis = record["axis"]
                parallel = abs(sum(axis[i] * normal[i] for i in range(3)))
                opening_radii = {record["diameter"] / 2}
                for stack in (record["cbore"], record["spotface"], record["csink"]):
                    if isinstance(stack, dict):
                        opening_radii.update(
                            value / 2
                            for key, value in stack.items()
                            if key in {"diameter", "major_diameter"}
                            and isinstance(value, (int, float))
                        )
                on_boundary = any(
                    any(abs(radius - wanted) <= 0.01 for wanted in opening_radii)
                    and all(abs(edge_center[i] - opening[i]) <= 0.01 for i in range(3))
                    for edge_center, radius in opening_edges
                )
                if abs(offset) <= 0.01 and parallel >= 0.98 and on_boundary:
                    holes.append(feature["ref"])
            if holes:
                rows.append(
                    {
                        "face_index": index,
                        "area": round(face.area, 6),
                        "normal": list(normal),
                        "hole_refs": holes,
                    }
                )
        return json.dumps(
            {
                "object": object_name or "current_shape",
                "mounting_face_candidates": rows,
                "protected_hole_refs": sorted({ref for row in rows for ref in row["hole_refs"]}),
                "note": "Geometric suggestions only; the caller must choose which interfaces to protect.",
            },
            indent=2,
        )
    except (ValueError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)}, indent=2)


def _solid_from_occt(shape):
    """Reject mixed defeaturing results instead of silently taking one solid."""
    from build123d import Solid
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID
    from OCP.TopoDS import TopoDS, TopoDS_Iterator

    if shape.ShapeType() == TopAbs_SOLID:
        return Solid(TopoDS.Solid_s(shape))
    if shape.ShapeType() != TopAbs_COMPOUND:
        raise ValueError("defeaturing did not produce a solid")
    solids = []
    stack = [shape]
    while stack:
        iterator = TopoDS_Iterator(stack.pop())
        while iterator.More():
            child = iterator.Value()
            if child.ShapeType() == TopAbs_SOLID:
                solids.append(Solid(TopoDS.Solid_s(child)))
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
    handle: str,
    diameter: float,
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
            raise ValueError("protected_refs must be a JSON list of feature handles")
        target = session._recognition_targets.get(handle)
        if target is None:
            raise ValueError("unknown or expired feature handle; call recognise_features() again")
        source_name = target["source_name"]
        source = target["source"]
        active = (
            session.current_shape if source_name == "@current" else session.objects.get(source_name)
        )
        if active is not source:
            raise ValueError("stale feature handle; source geometry has changed")
        if target["family"] != "holes":
            raise ValueError("edit_feature currently supports only plain through holes")
        if not source.is_valid or len(source.solids()) != 1:
            raise ValueError("source must be one valid solid")
        for ref in protected:
            entry = session._recognition_targets.get(ref)
            if entry is None or entry["source"] is not source:
                raise ValueError(f"protected handle {ref!r} is unknown or belongs to another shape")
            if entry["family"] != "holes":
                raise ValueError("protected_refs currently accepts only hole handles")
        if handle in protected:
            raise ValueError("target hole is protected; remove it from protected_refs to edit it")
        evidence = target["evidence"]
        feature = target["feature"]
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
        if abs(diameter - record["diameter"]) <= 1e-6:
            raise ValueError("new diameter equals the current diameter")

        from build123d import Align, Cylinder, Plane
        from quiddity.evidence import build_recognition_evidence

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
            resolver = getattr(evidence, "caller_face", evidence.face)
            remover.AddFaceToRemove(resolver(next(iter(faces))).wrapped)
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
        after = build_recognition_evidence(candidate)
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
                "handle": handle,
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
