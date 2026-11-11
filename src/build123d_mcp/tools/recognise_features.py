"""Shared feature inventory with exact, run-local edit evidence.

The recogniser package deliberately owns geometry interpretation but not CAD
mutation.  This adapter keeps that boundary: it returns structured occurrences
and retains opaque evidence inside the worker, while ``recognition_faces()`` in
the execute namespace resolves a returned handle to exact caller-part faces.
"""

from __future__ import annotations

import json
from collections import Counter
from enum import Enum
from typing import Any

from build123d_mcp.tools.measure import _resolve_shape
from build123d_mcp.tools.resolve import _describe

_MAX_FEATURES = 100
# Result fields that are not published as feature families: pattern aggregates
# and SectionRecess refusals (published under ``section_recesses``).
_NON_TARGET_RESULT_FIELDS = frozenset(
    {
        "hole_patterns",
        "slot_patterns",
        "oriented_slot_patterns",
        "section_recess_patterns",
        "section_recess_refusals",
        "gusset_rib_patterns",
    }
)
# quiddity adds pattern aggregates over time (e.g. ``circular_face_patterns``), so
# any ``*_patterns`` / ``*_refusals`` field is treated as an aggregate by name too.
_AGGREGATE_SUFFIXES = ("_patterns", "_refusals")


def _is_non_target(name: str) -> bool:
    return name in _NON_TARGET_RESULT_FIELDS or name.endswith(_AGGREGATE_SUFFIXES)


# Aggregate fields agents still ask for by name. They carry no per-feature
# evidence (so no edit handles), but their records are useful context, so they
# are returned read-only instead of rejected as unknown families.
def _is_read_only(name: str) -> bool:
    return _is_non_target(name) and not name.endswith("_refusals")


_INVALID_SOLID_GUIDANCE = (
    "Recognition needs a valid solid, and this shape fails the B-rep validity check. "
    "Repair it first (read build123d://skill/repair; validate() must pass), then call "
    "recognise_features() again on the repaired shape."
)


def _read_only_records(value: Any) -> list[Any]:
    """Flatten an aggregate result field (some are tuples of per-axis lists)."""
    items = list(value)
    if items and all(isinstance(item, (list, tuple)) for item in items):
        return [record for group in items for record in group]
    return items


def _compact_record(record: Any) -> Any:
    """JSON-safe summary of one aggregate record, bounded in size.

    Raw B-rep objects (e.g. a cylinder's ``face``) are dropped, and nested lists
    of member records (e.g. a pattern's ``holes``) collapse to a count, so one
    aggregate record cannot drag every member feature into the response.
    """
    data = record.to_dict() if hasattr(record, "to_dict") else _json_value(record)
    if not isinstance(data, dict):
        return _json_value(data)
    out: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, (list, tuple)) and any(isinstance(v, dict) for v in val):
            out[f"{key}_count"] = len(val)
            continue
        try:
            json.dumps(val)
        except TypeError:
            continue
        out[key] = _json_value(val)
    return out


def _json_value(value: Any) -> Any:
    """Round record values while preserving their JSON-compatible shape."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _inventory(result: dict[str, list]) -> dict[str, int]:
    return {name: len(records) for name, records in sorted(result.items())}


def _normalise_families(requested: str, known: set[str]) -> tuple[list[str], list[str]]:
    selected: list[str] = []
    unknown: list[str] = []
    for raw in requested.split(","):
        name = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if not name:
            continue
        candidates = (
            name,
            f"{name}s",
            f"{name}es",
            f"{name[:-1]}ies" if name.endswith("y") else name,
        )
        family = next((candidate for candidate in candidates if candidate in known), None)
        if family is None:
            unknown.append(name)
        elif family not in selected:
            selected.append(family)
    return selected, unknown


def _frame(evidence: Any) -> dict[str, Any] | None:
    frame = evidence.frame
    if frame is None:
        return None
    return {key: _json_value(frame[key]) for key in ("origin", "x", "y", "z", "gauge")}


def _face_rows(source: Any, evidence: Any, references: Any) -> list[dict[str, Any]]:
    source_faces = tuple(source.faces())
    rows: list[tuple[int, dict[str, Any]]] = []
    for reference in references:
        face = evidence.face(reference)
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
    from build123d_mcp.tools._recognition import build_evidence

    evidence = build_evidence(source, framed=coordinate_frame == "part")

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
        # BRepCheck on a large part takes seconds; check once per run, not per call.
        "source_valid": source.is_valid,
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
            if not source.is_valid:
                # The recogniser only makes claims about a proven valid solid, so an
                # invalid import fails deep inside one family with an opaque message.
                return json.dumps(
                    {"error": _INVALID_SOLID_GUIDANCE, "recogniser_detail": str(exc)},
                    indent=2,
                )
            return json.dumps({"error": f"Recognition failed: {exc}"}, indent=2)
        session._recognition_runs[cache_key] = run

    evidence = run["evidence"]
    invalid_source = not run["source_valid"]
    if invalid_source and not evidence.features:
        # Newer quiddity degrades locally on invalid input instead of raising, so an
        # empty inventory here means "no proof", not "no features": say why.
        return json.dumps(
            {
                "error": _INVALID_SOLID_GUIDANCE,
                "recogniser_detail": "no feature could be proven on the invalid solid",
            },
            indent=2,
        )
    result = evidence.result
    inventory = _inventory(result)
    # Targetable families are the ones evidence publishes per occurrence. A family
    # recognition counts but evidence folds into another (pockets -> section_recesses)
    # is not; one with no records this run stays askable, and matches nothing.
    known = {evidence.family(f) for f in evidence.features if not evidence.is_aggregate(f)} | {
        name for name, records in result.items() if not records and not _is_non_target(name)
    }
    read_only_known = {name for name in inventory if _is_read_only(name)}
    requested, unknown = _normalise_families(families, known | read_only_known)
    if unknown:
        return json.dumps(
            {
                "error": f"Unknown targetable families: {', '.join(unknown)}",
                "targetable_families": sorted(known),
                "read_only_families": sorted(read_only_known),
            },
            indent=2,
        )
    selected = [name for name in requested if name in known]
    read_only = {}
    for name in (n for n in requested if n in read_only_known):
        records = _read_only_records(result[name])
        read_only[name] = {
            "count": len(records),
            "records": [_compact_record(r) for r in records[:max_features]],
            "truncated": len(records) > max_features,
        }

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
            "record_type": record.kind,
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
        # quiddity also publishes pattern aggregates (hole_patterns,
        # circular_face_patterns) as evidence features; they are offered read-only
        # here, so count only the families the caller can actually target.
        "targetable": dict(
            sorted(Counter(t["family"] for t in run["targets"] if t["family"] in known).items())
        ),
        "association": evidence.association(),
        "requested_families": selected,
        "matched": len(matching),
        "returned": len(features),
        "truncated": len(matching) > len(features),
        "features": features,
    }
    if read_only:
        response["read_only"] = read_only
        response["read_only_note"] = (
            "Aggregate records for context only: they have no @feature handles. "
            "Target edits through the member families (e.g. holes) instead."
        )
    if invalid_source:
        response["warning"] = (
            "The shape fails the B-rep validity check; these features were proven only on "
            "its sound regions. " + _INVALID_SOLID_GUIDANCE
        )
    if not evidence.complete:
        failed = sorted({entry.get("family", "?") for entry in evidence.failures})
        response["incomplete"] = (
            f"Recognition of {', '.join(failed)} failed inside quiddity; records of those "
            "families are missing, not absent."
        )
    frame = _frame(evidence)
    if frame is not None:
        response["frame"] = frame
    return json.dumps(response, indent=2)
