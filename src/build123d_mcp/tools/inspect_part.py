"""Compact generation-checkpoint feature inventory (#417 prototype)."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any

from build123d_mcp.tools.find_features import _pattern_record, _record
from build123d_mcp.tools.measure import _resolve_shape

_TOP_LEVEL_EXPECTATION_KEYS = {
    "bbox",
    "solid_count",
    "holes",
    "bosses",
    "patterns",
    "section_varying",
    "tolerance",
}
_GROUP_KEYS = {
    "holes": {"count", "axis", "diameter", "depth", "bottom", "cbore", "spotface"},
    "bosses": {"count", "axis", "diameter", "height"},
    "patterns": {
        "count",
        "type",
        "diameter",
        "pitch",
        "direction",
        "center",
        "member_count",
        "member_diameter",
    },
}


def _axis_key(axis: list[float] | tuple[float, ...]) -> tuple[float, float, float]:
    if len(axis) != 3:
        raise ValueError(f"axis must contain exactly 3 values, got {len(axis)}")
    values = (
        round(float(axis[0]), 3),
        round(float(axis[1]), 3),
        round(float(axis[2]), 3),
    )
    opposite = (-values[0], -values[1], -values[2])
    return min(values, opposite)


def _group_records(records: list[dict], fields: tuple[str, ...]) -> list[dict]:
    grouped: dict[tuple[Any, ...], list[dict]] = defaultdict(list)
    for record in records:
        key: list[Any] = []
        for field in fields:
            value = record.get(field)
            if field == "axis" and value is not None:
                value = _axis_key(value)
            elif isinstance(value, float):
                value = round(value, 3)
            elif isinstance(value, dict):
                value = json.dumps(value, sort_keys=True)
            key.append(value)
        grouped[tuple(key)].append(record)

    result = []
    for group_key, members in grouped.items():
        item: dict[str, Any] = dict(zip(fields, group_key, strict=True))
        for field, value in list(item.items()):
            if field == "axis" and value is not None:
                item[field] = list(value)
            elif field in {"cbore", "spotface"} and isinstance(value, str):
                item[field] = json.loads(value)
        item["count"] = len(members)
        result.append(item)
    result.sort(key=lambda item: tuple(str(item.get(field)) for field in fields))
    return result


def _group_patterns(records: list[dict]) -> list[dict]:
    grouped: dict[str, tuple[dict, int]] = {}
    for record in records:
        key = json.dumps(record, sort_keys=True)
        _record_value, count = grouped.get(key, (record, 0))
        grouped[key] = (record, count + 1)
    return [
        {**record, "count": count} for record, count in (grouped[key] for key in sorted(grouped))
    ]


def _section_summary(sections: list[dict], axis: str) -> dict[str, Any]:
    areas = [float(section["area"]) for section in sections]
    peak = max(areas, default=0.0)
    spread = max(areas, default=0.0) - min(areas, default=0.0)
    variation = spread / peak if peak > 1e-9 else 0.0
    return {
        "axis": axis.upper(),
        "samples": sections,
        "variation_ratio": round(variation, 4),
        "constant_section": variation <= 0.01,
    }


def _matches(actual: float, expected: float, tolerance: float) -> bool:
    return math.isclose(float(actual), float(expected), abs_tol=tolerance, rel_tol=0.0)


def _vector_matches(actual: Any, expected: Any, tolerance: float) -> bool:
    return (
        isinstance(actual, (list, tuple))
        and isinstance(expected, (list, tuple))
        and len(actual) == len(expected) == 3
        and all(_matches(a, e, tolerance) for a, e in zip(actual, expected, strict=True))
    )


def _group_matches(actual: dict[str, Any], expected: dict[str, Any], tolerance: float) -> bool:
    for key in ("diameter", "depth", "height", "pitch", "member_diameter"):
        if key in expected and not _matches(actual.get(key, math.inf), expected[key], tolerance):
            return False
    if "axis" in expected:
        actual_axis = actual.get("axis")
        if not isinstance(actual_axis, (list, tuple)) or len(actual_axis) != 3:
            return False
        if _axis_key(actual_axis) != _axis_key(expected["axis"]):
            return False
    if "direction" in expected:
        actual_direction = actual.get("direction")
        if not isinstance(actual_direction, (list, tuple)) or len(actual_direction) != 3:
            return False
        if _axis_key(actual_direction) != _axis_key(expected["direction"]):
            return False
    if "center" in expected and not _vector_matches(
        actual.get("center"), expected["center"], tolerance
    ):
        return False
    if "member_count" in expected and actual.get("member_count") != expected["member_count"]:
        return False
    for key in ("bottom", "type"):
        if key in expected and actual.get(key) != expected[key]:
            return False
    for key in ("cbore", "spotface"):
        if key in expected and actual.get(key) != expected[key]:
            return False
    return True


def _expectations_overlap(first: dict[str, Any], second: dict[str, Any], tolerance: float) -> bool:
    """Return whether one actual group could satisfy both expectation lines."""
    numeric = {"diameter", "depth", "height", "pitch", "member_diameter"}
    vectors = {"center"}
    axes = {"axis", "direction"}
    exact = {"member_count", "bottom", "type", "cbore", "spotface"}
    for key in set(first) & set(second) - {"count"}:
        if key in numeric and not _matches(first[key], second[key], tolerance):
            return False
        if key in vectors and not _vector_matches(first[key], second[key], tolerance):
            return False
        if key in axes and _axis_key(first[key]) != _axis_key(second[key]):
            return False
        if key in exact and first[key] != second[key]:
            return False
    return True


def _check_expected_groups(
    kind: str, actual: list[dict], expected: list[dict], tolerance: float
) -> list[str]:
    mismatches = []
    matched_counts = [0] * len(expected)
    matched_groups = [0] * len(expected)
    for group in actual:
        matches = [
            index
            for index, wanted in enumerate(expected)
            if _group_matches(group, wanted, tolerance)
        ]
        if not matches:
            mismatches.append(f"unexpected {kind} group: {json.dumps(group, sort_keys=True)}")
        elif len(matches) > 1:
            mismatches.append(
                f"ambiguous {kind} group matches multiple expectations: "
                f"{json.dumps(group, sort_keys=True)}"
            )
        else:
            matched_counts[matches[0]] += int(group["count"])
            matched_groups[matches[0]] += 1

    qualifier_keys = (
        "diameter",
        "depth",
        "height",
        "pitch",
        "member_diameter",
        "member_count",
        "axis",
        "direction",
        "center",
        "bottom",
        "type",
    )
    for index, (wanted, actual_count) in enumerate(zip(expected, matched_counts, strict=True)):
        if matched_groups[index] > 1:
            mismatches.append(
                f"underspecified {kind} expectation matched {matched_groups[index]} distinct "
                "actual groups; add axis, depth, bottom, or other distinguishing qualifiers"
            )
        wanted_count = int(wanted.get("count", 1))
        if actual_count != wanted_count:
            qualifiers = ", ".join(
                f"{key}={wanted[key]}" for key in qualifier_keys if key in wanted
            )
            mismatches.append(
                f"expected {wanted_count} {kind} feature(s) matching [{qualifiers}], found {actual_count}"
            )
    return mismatches


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _validate_vector(value: Any, label: str) -> None:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must be a 3-number JSON array")
    for index, component in enumerate(value):
        _finite_number(component, f"{label}[{index}]")


def _validate_expectation(expectation: dict[str, Any]) -> None:
    unknown = set(expectation) - _TOP_LEVEL_EXPECTATION_KEYS
    if unknown:
        raise ValueError(f"expected contains unsupported key(s): {', '.join(sorted(unknown))}")
    tolerance = _finite_number(expectation.get("tolerance", 0.1), "expected.tolerance")
    if tolerance < 0:
        raise ValueError("expected.tolerance must be non-negative")
    if not (set(expectation) - {"tolerance"}):
        raise ValueError("expected must contain at least one supported expectation")
    if "solid_count" in expectation:
        value = expectation["solid_count"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("expected.solid_count must be a non-negative integer")
    if "section_varying" in expectation and not isinstance(expectation["section_varying"], bool):
        raise ValueError("expected.section_varying must be a boolean")

    if "bbox" in expectation:
        bbox = expectation["bbox"]
        if isinstance(bbox, list):
            _validate_vector(bbox, "expected.bbox")
        elif isinstance(bbox, dict):
            bad_axes = set(bbox) - {"x", "y", "z"}
            if bad_axes or not bbox:
                raise ValueError("expected.bbox must contain only x, y, and/or z")
            for axis, value in bbox.items():
                _finite_number(value, f"expected.bbox.{axis}")
        else:
            raise ValueError("expected.bbox must be a 3-number array or an axis object")

    for kind, allowed_keys in _GROUP_KEYS.items():
        if kind not in expectation:
            continue
        groups = expectation[kind]
        if not isinstance(groups, list):
            raise ValueError(f"expected.{kind} must be a JSON array")
        for index, group in enumerate(groups):
            label = f"expected.{kind}[{index}]"
            if not isinstance(group, dict):
                raise ValueError(f"{label} must be a JSON object")
            unknown_group_keys = set(group) - allowed_keys
            if unknown_group_keys:
                raise ValueError(
                    f"{label} contains unsupported key(s): {', '.join(sorted(unknown_group_keys))}"
                )
            count = group.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{label}.count must be a non-negative integer")
            for key in ("diameter", "depth", "height", "pitch", "member_diameter"):
                if key in group:
                    _finite_number(group[key], f"{label}.{key}")
            if "member_count" in group:
                member_count = group["member_count"]
                if (
                    isinstance(member_count, bool)
                    or not isinstance(member_count, int)
                    or member_count < 0
                ):
                    raise ValueError(f"{label}.member_count must be a non-negative integer")
            for key in ("axis", "direction", "center"):
                if key in group:
                    _validate_vector(group[key], f"{label}.{key}")
        for first_index, first in enumerate(groups):
            for second_index in range(first_index + 1, len(groups)):
                if _expectations_overlap(first, groups[second_index], tolerance):
                    raise ValueError(
                        f"expected.{kind}[{first_index}] overlaps "
                        f"expected.{kind}[{second_index}]; combine them or add distinguishing "
                        "qualifiers"
                    )


def _expectation_mismatches(report: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    tolerance = float(expected.get("tolerance", 0.1))
    mismatches = []
    if "solid_count" in expected and report["topology"]["solids"] != expected["solid_count"]:
        mismatches.append(
            f"expected {expected['solid_count']} solid(s), found {report['topology']['solids']}"
        )

    bbox_expected = expected.get("bbox")
    if bbox_expected:
        if isinstance(bbox_expected, list):
            bbox_expected = dict(zip(("x", "y", "z"), bbox_expected, strict=True))
        for axis in ("x", "y", "z"):
            if axis in bbox_expected:
                actual = report["bbox"][axis]
                if not _matches(actual, bbox_expected[axis], tolerance):
                    mismatches.append(f"expected bbox {axis}={bbox_expected[axis]}, found {actual}")

    for kind in ("holes", "bosses", "patterns"):
        if kind in expected:
            mismatches.extend(
                _check_expected_groups(kind[:-1], report[kind]["groups"], expected[kind], tolerance)
            )

    if "section_varying" in expected:
        actual = not report["sections"]["constant_section"]
        if actual != expected["section_varying"]:
            mismatches.append(
                f"expected section_varying={expected['section_varying']}, found {actual}"
            )
    return mismatches


def inspect_part(
    session,
    object_name: str = "",
    section_axis: str = "Z",
    section_slices: int = 7,
    expected: str = "",
) -> str:
    """Return a compact structural inventory and optional expectation verdict.

    ``expected`` is a JSON object derived from the drawing/spec, for example::

        {"bbox":[100,80,20], "solid_count":1,
         "holes":[{"count":4,"diameter":6,"axis":[0,0,1],"bottom":"through"}],
         "bosses":[{"count":2,"diameter":12,"height":8}],
         "patterns":[{"count":1,"type":"bolt_circle"}],
         "section_varying":true, "tolerance":0.1}

    Omit ``expected`` for inventory-only use. No benchmark expectations are built in.
    """
    from build123d_mcp.tools._bounded import run_bounded_shape_op

    shape = _resolve_shape(session, object_name)
    try:
        expectation = json.loads(expected) if expected.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"expected must be valid JSON: {exc.msg}") from exc
    if not isinstance(expectation, dict):
        raise ValueError("expected must be a JSON object")
    if expected.strip():
        _validate_expectation(expectation)

    slices = max(2, min(int(section_slices), 15))
    return run_bounded_shape_op(
        session,
        "inspect_part",
        {"": shape},
        {
            "object_name": object_name,
            "section_axis": section_axis,
            "section_slices": slices,
            "expectation": expectation,
        },
        in_process=lambda: _inspect_part_report(
            shape, object_name, section_axis, slices, expectation
        ),
    )


def _inspect_part_report(
    shape,
    object_name: str,
    section_axis: str,
    section_slices: int,
    expectation: dict[str, Any],
) -> str:
    from build123d_drafting import find_bosses as recognise_bosses
    from build123d_drafting import find_hole_patterns as recognise_patterns
    from build123d_drafting import find_holes as recognise_holes

    from build123d_mcp.tools.measure import _cross_sections

    bbox = shape.bounding_box()
    holes_raw = list(recognise_holes(shape))
    bosses_raw = list(recognise_bosses(shape))
    patterns_raw = list(recognise_patterns(holes_raw))
    holes = [_record(item) for item in holes_raw]
    bosses = [_record(item) for item in bosses_raw]

    pattern_records = []
    for pattern in patterns_raw:
        record = _pattern_record(pattern)
        compact = {
            key: value
            for key, value in record.items()
            if key in {"type", "diameter", "pitch", "direction", "center"}
        }
        members = record.get("holes", [])
        compact["member_count"] = len(members)
        member_diameters = {member.get("diameter") for member in members}
        if len(member_diameters) == 1:
            compact["member_diameter"] = member_diameters.pop()
        pattern_records.append(compact)

    sections = _section_summary(_cross_sections(shape, section_axis, section_slices), section_axis)
    warnings: list[str] = []
    report: dict[str, Any] = {
        "object": object_name or "current_shape",
        "bbox": {
            "x": round(float(bbox.size.X), 4),
            "y": round(float(bbox.size.Y), 4),
            "z": round(float(bbox.size.Z), 4),
        },
        "topology": {
            "solids": len(shape.solids()),
            "faces": len(shape.faces()),
            "edges": len(shape.edges()),
            "vertices": len(shape.vertices()),
        },
        "holes": {
            "count": len(holes),
            "groups": _group_records(
                holes, ("axis", "diameter", "depth", "bottom", "cbore", "spotface")
            ),
        },
        "bosses": {
            "count": len(bosses),
            "groups": _group_records(bosses, ("axis", "diameter", "height")),
        },
        "patterns": {
            "count": len(patterns_raw),
            "groups": _group_patterns(pattern_records),
        },
        "sections": sections,
        "warnings": warnings,
    }

    axis_spans = {
        (1.0, 0.0, 0.0): report["bbox"]["x"],
        (0.0, 1.0, 0.0): report["bbox"]["y"],
        (0.0, 0.0, 1.0): report["bbox"]["z"],
    }
    for hole in holes:
        axis = _axis_key(hole["axis"])
        absolute_axis = (abs(axis[0]), abs(axis[1]), abs(axis[2]))
        span = axis_spans.get(absolute_axis)
        if span and hole.get("bottom") != "through" and float(hole["depth"]) < span * 0.1:
            warnings.append(
                f"review shallow partial cut: diameter {hole['diameter']}, depth {hole['depth']} "
                f"is <10% of the {round(span, 4)} axis span"
            )
    if sections["constant_section"]:
        warnings.append(
            f"section profile along {section_axis.upper()} is nearly constant; verify intended cores, "
            "pockets, steps, or open space"
        )

    mismatches = _expectation_mismatches(report, expectation) if expectation else []
    report["expectations_provided"] = bool(expectation)
    report["mismatches"] = mismatches
    report["passes_expectations"] = not mismatches if expectation else None
    report["status"] = (
        "PASS" if expectation and not mismatches else "FAIL" if mismatches else "INVENTORY"
    )
    return json.dumps(report, indent=2)
