"""find_holes / find_bosses — feature recognition on session objects (#264).

Thin wrappers around ``build123d_drafting.find_holes`` / ``find_bosses``
(pzfreo/build123d-drafting-helpers#87): resolve the named session object,
run the recognition, serialise the dataclass records to JSON.
"""

import json
import math
import re
from dataclasses import asdict, is_dataclass

from build123d_mcp.tools.measure import _resolve_shape


def _snake(name: str) -> str:
    """CamelCase class name -> snake_case type tag (RectGrid -> rect_grid)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    if is_dataclass(value) and not isinstance(value, type):
        return _round(asdict(value))
    if isinstance(value, tuple):
        return [_round(v) for v in value]
    if isinstance(value, list):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def _record(feature) -> dict:
    return {k: _round(v) for k, v in asdict(feature).items()}


def _pattern_record(pattern) -> dict:
    """Serialise a recognised pattern consistently for all public tools."""
    from build123d_drafting import BoltCircle, LinearArray

    rec: dict = {"holes": [_record(h) for h in getattr(pattern, "holes", [])]}
    if isinstance(pattern, BoltCircle):
        rec["type"] = "bolt_circle"
        rec["center"] = [round(c, 4) for c in pattern.center]
        rec["diameter"] = pattern.diameter
    elif isinstance(pattern, LinearArray):
        rec["type"] = "linear_array"
        rec["pitch"] = pattern.pitch
        rec["direction"] = [round(c, 4) for c in pattern.direction]
    else:
        rec["type"] = _snake(type(pattern).__name__)
        if is_dataclass(pattern) and not isinstance(pattern, type):
            rec.update({k: _round(v) for k, v in asdict(pattern).items() if k != "holes"})
    return rec


def _vec3(value) -> tuple[float, float, float]:
    if hasattr(value, "X"):
        return (float(value.X), float(value.Y), float(value.Z))
    return (float(value[0]), float(value[1]), float(value[2]))


def _sub(a, b) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a) -> tuple[float, float, float]:
    n = _norm(a)
    if n <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


def _neg(a) -> tuple[float, float, float]:
    return (-a[0], -a[1], -a[2])


def _axis_distance(point, origin, axis) -> float:
    rel = _sub(point, origin)
    along = _dot(rel, axis)
    perp = (rel[0] - along * axis[0], rel[1] - along * axis[1], rel[2] - along * axis[2])
    return _norm(perp)


def _bbox_projection(shape, axis) -> tuple[float, float]:
    bb = shape.bounding_box()
    xs = (float(bb.min.X), float(bb.max.X))
    ys = (float(bb.min.Y), float(bb.max.Y))
    zs = (float(bb.min.Z), float(bb.max.Z))
    vals = [_dot((x, y, z), axis) for x in xs for y in ys for z in zs]
    return (min(vals), max(vals))


def _perp_basis(axis) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    ref = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _unit(_cross(axis, ref))
    return u, _unit(_cross(axis, u))


def _bbox_corners(shape) -> list[tuple[float, float, float]]:
    bb = shape.bounding_box()
    xs = (float(bb.min.X), float(bb.max.X))
    ys = (float(bb.min.Y), float(bb.max.Y))
    zs = (float(bb.min.Z), float(bb.max.Z))
    return [(x, y, z) for x in xs for y in ys for z in zs]


def _perp_spans(shape, axis) -> tuple[float, float]:
    u, v = _perp_basis(axis)
    corners = _bbox_corners(shape)
    us = [_dot(c, u) for c in corners]
    vs = [_dot(c, v) for c in corners]
    return (max(us) - min(us), max(vs) - min(vs))


def _radial_extent(shape, origin, axis) -> float:
    return max(_axis_distance(corner, origin, axis) for corner in _bbox_corners(shape))


def _face_geom_name(face) -> str:
    geom = getattr(face, "geom_type", "")
    return getattr(geom, "name", str(geom)).upper()


def _cap_faces_for_hole(shape, location, axis_into_part, bore_diameter: float) -> list[dict]:
    outward = _neg(axis_into_part)
    cap_plane_tol = max(0.08, bore_diameter * 0.03)
    radial_limit = max(10.0, bore_diameter * 3.0)
    cap_faces = []

    for idx, face in enumerate(shape.faces()):
        if "PLANE" not in _face_geom_name(face):
            continue
        try:
            center = _vec3(face.center())
            normal = _unit(_vec3(face.normal_at()))
            area = float(face.area)
        except Exception:
            continue
        plane_distance = abs(_dot(_sub(center, location), axis_into_part))
        if plane_distance > cap_plane_tol:
            continue
        normal_alignment = _dot(normal, outward)
        if abs(normal_alignment) < 0.85:
            continue
        radial_distance = _axis_distance(center, location, axis_into_part)
        if radial_distance > radial_limit:
            continue
        if area <= 1e-8:
            continue
        span_u, span_v = _perp_spans(face, axis_into_part)
        cap_faces.append(
            {
                "index": idx,
                "area": round(area, 4),
                "center": _round(center),
                "normal": _round(normal),
                "normal_alignment_to_outward_axis": round(normal_alignment, 4),
                "plane_distance": round(plane_distance, 4),
                "axis_distance_from_bore": round(radial_distance, 4),
                "radial_extent_from_bore": round(_radial_extent(face, location, axis_into_part), 4),
                "perpendicular_span": [round(span_u, 4), round(span_v, 4)],
            }
        )

    cap_faces.sort(key=lambda rec: (-rec["area"], rec["axis_distance_from_bore"]))
    return cap_faces


def _find_bored_boss_candidates(shape) -> list[dict]:
    from build123d_drafting import find_holes as _find_holes

    candidates = []
    envelope_tol = 0.25
    holes = list(_find_holes(shape))
    for hole_idx, hole in enumerate(holes):
        axis = _unit(_vec3(hole.axis))
        if axis == (0.0, 0.0, 0.0):
            continue
        location = _vec3(hole.location)
        outward = _neg(axis)
        diameter = float(hole.diameter)
        cap_faces = _cap_faces_for_hole(shape, location, axis, diameter)
        if not cap_faces:
            continue

        shape_span_u, shape_span_v = _perp_spans(shape, axis)
        local_cap_faces = [
            face
            for face in cap_faces
            if (
                face["perpendicular_span"][0] < shape_span_u * 0.85
                or face["perpendicular_span"][1] < shape_span_v * 0.85
            )
        ]
        if not local_cap_faces:
            continue
        cap_faces = local_cap_faces

        _min_proj, max_proj = _bbox_projection(shape, outward)
        loc_proj = _dot(location, outward)
        on_outer_envelope = abs(max_proj - loc_proj) <= max(envelope_tol, diameter * 0.05)
        split_cap = len(cap_faces) > 1
        risk_flags = []
        if split_cap:
            risk_flags.append("split_cap_front")
        if not on_outer_envelope:
            risk_flags.append("opening_not_on_outer_envelope")
        if getattr(hole, "bottom", "") != "through":
            risk_flags.append(f"bore_bottom_{hole.bottom}")

        if split_cap:
            construction_advice = (
                "The bore opening cap is split across multiple planar faces. Do not "
                "extrude one face as the boss profile; build a complete extension "
                "solid/sleeve from the measured axis/profile and re-cut the central "
                "bore continuously through old plus new material."
            )
        else:
            construction_advice = (
                "One planar cap face was found at the bore opening. A face-derived "
                "profile may be usable, but still verify the full outer wire and "
                "re-cut the bore continuously after any fuse."
            )

        candidates.append(
            {
                "hole_index": hole_idx,
                "location": _round(location),
                "axis_into_part": _round(axis),
                "outward_axis": _round(outward),
                "bore_diameter": round(diameter, 4),
                "bore_depth": round(float(hole.depth), 4),
                "bottom": hole.bottom,
                "counterbore": _round(getattr(hole, "cbore", None)),
                "spotface": _round(getattr(hole, "spotface", None)),
                "cap_face_count": len(cap_faces),
                "cap_area_total": round(sum(float(face["area"]) for face in cap_faces), 4),
                "cap_faces": cap_faces[:8],
                "is_split_cap": split_cap,
                "opening_on_outer_envelope": on_outer_envelope,
                "risk_flags": risk_flags,
                "construction_advice": construction_advice,
            }
        )

    candidates.sort(
        key=lambda rec: (
            not rec["opening_on_outer_envelope"],
            -rec["bore_diameter"],
            rec["hole_index"],
        )
    )
    return candidates


def find_holes(session, object_name: str = "") -> str:
    """Recognise drilled holes on a named session object."""
    from build123d_drafting import find_holes as _find_holes

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    holes = [_record(h) for h in _find_holes(shape)]
    return json.dumps({"count": len(holes), "holes": holes})


def find_hole_patterns(session, object_name: str = "") -> str:
    """Recognise bolt-circle / linear-array hole patterns on a session object."""
    from build123d_drafting import find_hole_patterns as _find_patterns
    from build123d_drafting import find_holes as _find_holes

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    patterns = [_pattern_record(pattern) for pattern in _find_patterns(_find_holes(shape))]
    # default=str so a generic field of an unknown future pattern type (a
    # Vector, enum, set, …) degrades to a string instead of raising TypeError.
    return json.dumps({"count": len(patterns), "patterns": patterns}, default=str)


def find_bosses(session, object_name: str = "") -> str:
    """Recognise external cylindrical bosses on a named session object."""
    from build123d_drafting import find_bosses as _find_bosses

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    bosses = [_record(b) for b in _find_bosses(shape)]
    return json.dumps({"count": len(bosses), "bosses": bosses})


def find_bored_bosses(session, object_name: str = "") -> str:
    """Find bored-boss edit candidates with bore/cap evidence.

    This is a diagnostic, not a proof that the user's target is one of these
    candidates. It helps agents avoid the common failure mode of extruding one
    split cap face and leaving an open shell or filled bore.
    """

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    candidates = _find_bored_boss_candidates(shape)
    return json.dumps(
        {
            "count": len(candidates),
            "candidates": candidates,
            "selection_advice": (
                "Treat this as a candidate table. Match against the request's visual "
                "and dimensional qualifiers, mark plausible candidates in a render, "
                "then measure the current boss length/depth before editing."
            ),
        }
    )
