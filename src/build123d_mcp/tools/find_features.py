"""find_holes / find_bosses — feature recognition on session objects (#264).

Thin wrappers around ``build123d_drafting.find_holes`` / ``find_bosses``
(pzfreo/build123d-drafting-helpers#87): resolve the named session object,
run the recognition, serialise the dataclass records to JSON.
"""

import json
import re
from dataclasses import asdict, is_dataclass

from build123d_mcp.tools.measure import _resolve_shape


def _snake(name: str) -> str:
    """CamelCase class name -> snake_case type tag (RectGrid -> rect_grid)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, tuple):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def _record(feature) -> dict:
    return {k: _round(v) for k, v in asdict(feature).items()}


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
    from build123d_drafting import BoltCircle, LinearArray
    from build123d_drafting import find_hole_patterns as _find_patterns
    from build123d_drafting import find_holes as _find_holes

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    patterns = []
    for p in _find_patterns(_find_holes(shape)):
        rec: dict = {"holes": [_record(h) for h in getattr(p, "holes", [])]}
        if isinstance(p, BoltCircle):
            rec["type"] = "bolt_circle"
            rec["center"] = [round(c, 4) for c in p.center]
            rec["diameter"] = p.diameter
        elif isinstance(p, LinearArray):
            rec["type"] = "linear_array"
            rec["pitch"] = p.pitch
            rec["direction"] = [round(c, 4) for c in p.direction]
        else:
            # Forward-compatible: build123d_drafting can return other pattern
            # types (e.g. RectGrid). Don't assume LinearArray's .pitch/.direction
            # — tag the type and serialise its fields generically so the tool can
            # never crash here (was: AttributeError: 'RectGrid' has no 'pitch').
            rec["type"] = _snake(type(p).__name__)
            if is_dataclass(p) and not isinstance(p, type):  # a dataclass instance
                rec.update({k: _round(v) for k, v in asdict(p).items() if k != "holes"})
        patterns.append(rec)
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
