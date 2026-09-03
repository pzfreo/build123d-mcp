"""Shared feature inventory with exact, run-local edit evidence.

The recogniser package deliberately owns geometry interpretation but not CAD
mutation.  This adapter keeps that boundary: it returns structured occurrences
and retains opaque evidence inside the worker, while ``recognition_faces()`` in
the execute namespace resolves a returned handle to exact caller-part faces.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from build123d_mcp.tools.measure import _resolve_shape
from build123d_mcp.tools.resolve import _describe

_MAX_FEATURES = 100
_NON_TARGET_RESULT_FIELDS = frozenset(
    {
        "cylinders",
        "rotational",
        "hole_patterns",
        "slot_patterns",
        "oriented_slot_patterns",
        "pocket_patterns",
        "passages",
    }
)


def _json_value(value: Any) -> Any:
    """Round record values while preserving their JSON-compatible shape."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _inventory(result: Any) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for field in fields(result):
        value = getattr(result, field.name)
        counts[field.name] = value if isinstance(value, bool) else len(value)
    return counts


def _normalise_families(requested: str, known: set[str]) -> tuple[list[str], list[str]]:
    selected: list[str] = []
    unknown: list[str] = []
    for raw in requested.split(","):
        name = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if not name:
            continue
        candidates = (name, f"{name}s", f"{name[:-1]}ies" if name.endswith("y") else name)
        family = next((candidate for candidate in candidates if candidate in known), None)
        if family is None:
            unknown.append(name)
        elif family not in selected:
            selected.append(family)
    return selected, unknown


def _association(evidence: Any) -> dict[str, Any]:
    association = evidence.association
    return {
        "faces": {
            "total": association.face_count.total,
            "associated": association.face_count.associated,
            "unassociated": association.face_count.unassociated,
            "ratio": _json_value(association.face_count.ratio),
        },
        "surface_area": {
            "total": _json_value(association.surface_area.total),
            "associated": _json_value(association.surface_area.associated),
            "unassociated": _json_value(association.surface_area.unassociated),
            "ratio": _json_value(association.surface_area.ratio),
        },
    }


def _frame(evidence: Any) -> dict[str, Any] | None:
    frame = getattr(evidence, "frame", None)
    if frame is None:
        return None
    return {
        "origin": _json_value(frame.origin),
        "x": _json_value(frame.x),
        "y": _json_value(frame.y),
        "z": _json_value(frame.z),
        "gauge": frame.gauge.value,
    }


def _source_face(evidence: Any, reference: Any) -> Any:
    caller_face = getattr(evidence, "caller_face", None)
    return caller_face(reference) if caller_face is not None else evidence.face(reference)


def _face_rows(source: Any, evidence: Any, references: Any) -> list[dict[str, Any]]:
    source_faces = tuple(source.faces())
    rows: list[tuple[int, dict[str, Any]]] = []
    for reference in references:
        face = _source_face(evidence, reference)
        matches = [
            index
            for index, candidate in enumerate(source_faces)
            if candidate.wrapped.IsSame(face.wrapped)
        ]
        if len(matches) != 1:
            raise ValueError("recognition evidence did not resolve to one exact source face")
        index = matches[0]
        row = {"index": index, "selector": f".faces()[{index}]"}
        row.update(_describe(face))
        rows.append((index, row))
    return [row for _, row in sorted(rows, key=lambda item: item[0])]


def _discard_run(session: Any, run: dict[str, Any]) -> None:
    for reference in run["references"]:
        session._recognition_targets.pop(reference, None)


def _build_run(session: Any, source: Any, source_name: str, coordinate_frame: str):
    from b123d_recognisers import RefusedFramedEvidence, build_framed_recognition_evidence
    from b123d_recognisers.evidence import build_recognition_evidence

    evidence: Any
    if coordinate_frame == "caller":
        evidence = build_recognition_evidence(source)
    else:
        evidence = build_framed_recognition_evidence(source)
        if isinstance(evidence, RefusedFramedEvidence):
            raise ValueError(f"part-relative evidence refused: {evidence.reason.value}")

    run_number = session._recognition_next_run
    session._recognition_next_run += 1
    per_family: Counter[str] = Counter()
    targets = []
    references = []
    for feature in evidence.features:
        family = evidence.family(feature)
        index = per_family[family]
        per_family[family] += 1
        reference = f"@feature[r{run_number}/{family}/{index}]"
        target = {
            "reference": reference,
            "family": family,
            "index": index,
            "feature": feature,
            "evidence": evidence,
            "source": source,
            "source_name": source_name,
        }
        targets.append(target)
        references.append(reference)
        session._recognition_targets[reference] = target
    return {
        "run": run_number,
        "source": source,
        "source_name": source_name,
        "coordinate_frame": coordinate_frame,
        "evidence": evidence,
        "targets": targets,
        "references": references,
    }


def recognise_features(
    session,
    object_name: str = "",
    families: str = "",
    coordinate_frame: str = "caller",
    include_faces: bool = False,
    max_features: int = 50,
) -> str:
    """Return a shared feature inventory and exact run-local edit targets.

    With no ``families``, the response is a compact count summary. Pass one or
    more comma-separated families to receive records and handles. Inside
    ``execute()``, ``recognition_faces(handle)`` returns exact constituent faces;
    pass ``role='defining'`` for only the faces that establish the occurrence.
    """
    try:
        source = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    if coordinate_frame not in {"caller", "part"}:
        return json.dumps({"error": "coordinate_frame must be 'caller' or 'part'"}, indent=2)
    if not isinstance(max_features, int) or isinstance(max_features, bool):
        return json.dumps({"error": "max_features must be an integer"}, indent=2)
    if not 1 <= max_features <= _MAX_FEATURES:
        return json.dumps(
            {"error": f"max_features must be between 1 and {_MAX_FEATURES}"}, indent=2
        )

    source_name = object_name or "@current"
    cache_key = (source_name, coordinate_frame)
    run = session._recognition_runs.get(cache_key)
    cached = run is not None and run["source"] is source
    if not cached:
        if run is not None:
            _discard_run(session, run)
        try:
            run = _build_run(session, source, source_name, coordinate_frame)
        except (RuntimeError, ValueError) as exc:
            return json.dumps({"error": f"Recognition failed: {exc}"}, indent=2)
        session._recognition_runs[cache_key] = run

    evidence = run["evidence"]
    result = evidence.result
    inventory = _inventory(result)
    known = set(inventory) - _NON_TARGET_RESULT_FIELDS
    selected, unknown = _normalise_families(families, known)
    if unknown:
        return json.dumps(
            {
                "error": f"Unknown targetable families: {', '.join(unknown)}",
                "targetable_families": sorted(known),
            },
            indent=2,
        )

    matching = [target for target in run["targets"] if target["family"] in selected]
    features = []
    for target in matching[:max_features]:
        feature = target["feature"]
        defining = evidence.defining_faces(feature)
        constituent = evidence.constituent_faces(feature)
        record = evidence.record(feature)
        item = {
            "ref": target["reference"],
            "family": target["family"],
            "index": target["index"],
            "record_type": type(record).__name__,
            "record": _json_value(record.to_dict()),
            "defining_face_count": len(defining),
            "constituent_face_count": len(constituent),
            "python": f"recognition_faces({target['reference']!r})",
        }
        if include_faces:
            item["defining_faces"] = _face_rows(source, evidence, defining)
            item["constituent_faces"] = _face_rows(source, evidence, constituent)
        features.append(item)

    response = {
        "object": object_name or "current_shape",
        "coordinate_frame": coordinate_frame,
        "run": run["run"],
        "cached": cached,
        "inventory": inventory,
        "targetable": dict(sorted(Counter(t["family"] for t in run["targets"]).items())),
        "association": _association(evidence),
        "requested_families": selected,
        "matched": len(matching),
        "returned": len(features),
        "truncated": len(matching) > len(features),
        "features": features,
    }
    frame = _frame(evidence)
    if frame is not None:
        response["frame"] = frame
    return json.dumps(response, indent=2)
