"""Bounded, caller-frame STEP feature inspection."""

import json
from collections import defaultdict

from build123d_mcp.tools._recognition import resolve_analysis

_MAX_FEATURES = 100
_MAX_BYTES = 32_000
_DIMENSIONS = ("diameter", "radius", "height", "depth", "width", "length")
# Empty families are still queryable on a plain box.
_FAMILIES = {
    "holes",
    "bosses",
    "fillets",
    "blends",
    "flats",
    "hole_patterns",
    "cylindrical_profiles",
    "freeform_surfaces",
    "step_levels",
    "risers",
    "polygonal_bosses",
    "slots",
    "chamfers",
    "section_recesses",
}


def compact(value):
    """Keep scalar/vector geometry; collapse arrays of members or raw faces."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            # Product-frame projection bookkeeping is not a caller-frame
            # geometric descriptor. Selected faces provide exact locations.
            if key in {"body_key", "w_center", "lo", "hi", "d_lo", "d_hi", "open_sign"}:
                continue
            if isinstance(item, list) and (
                key.endswith("faces")
                or len(item) > 16
                or any(isinstance(v, (dict, list)) for v in item)
            ):
                out[f"{key}_count"] = len(item)
            else:
                out[key] = compact(item)
        return out
    if isinstance(value, (tuple, list)):
        return [compact(item) for item in value]
    return value


def _bounded_rows(rows, limit):
    out = []
    for row in rows[:limit]:
        if len(json.dumps(out + [row])) > _MAX_BYTES:
            break
        out.append(row)
    return out


def inspect_features(
    session, target: str = "", families: str = "", feature_ids: str = "", max_features: int = 50
) -> str:
    """Inspect a STEP path or session object; default to the current shape.

    No filters returns measures and family counts/ranges. families selects
    caller-frame records without face arrays; feature_ids selects analytic face
    descriptors. Expanded records are bounded to 1..100, with exact counts.
    """
    try:
        if (
            isinstance(max_features, bool)
            or not isinstance(max_features, int)
            or not 1 <= max_features <= _MAX_FEATURES
        ):
            raise ValueError("max_features must be between 1 and 100")
        if families and feature_ids:
            raise ValueError("choose families or feature_ids, not both")
        analysis = resolve_analysis(session, target)
        features = analysis.document["features"]
        groups = defaultdict(list)
        for feature in features:
            groups[feature["family"]].append(feature)
        if not families and not feature_ids:
            inventory = {}
            for family, entries in sorted(groups.items()):
                summary: dict = {"count": len(entries)}
                for key in _DIMENSIONS:
                    values = [
                        e["record"][key]
                        for e in entries
                        if isinstance(e["record"].get(key), (int, float))
                    ]
                    if values:
                        summary[key] = [round(min(values), 3), round(max(values), 3)]
                inventory[family] = summary
            result = {"measures": compact(analysis.measures), "inventory": inventory}
            text = json.dumps(result, separators=(",", ":"))
            if len(text.encode()) >= 2000:
                # Keep every count AND range: a shared column table saves
                # repeated field names on inventories with many families.
                columns = [
                    key for key in _DIMENSIONS if any(key in row for row in inventory.values())
                ]
                result["inventory_columns"] = ["count", *columns]
                result["inventory"] = {
                    family: [row["count"], *(row.get(key) for key in columns)]
                    for family, row in inventory.items()
                }
                text = json.dumps(result, separators=(",", ":"))
            if len(text.encode()) >= 2000:
                raise ValueError("inventory exceeds 2 KB; query specific families")
            return text
        if feature_ids:
            requested = list(dict.fromkeys(f.strip() for f in feature_ids.split(",") if f.strip()))
            by_id = {f["id"]: f for f in features}
            unknown = set(requested) - by_id.keys()
            if unknown:
                raise ValueError(f"unknown or stale feature ids: {', '.join(sorted(unknown))}")
            faces = {f["id"]: compact(f) for f in analysis.document["faces"]}
            rows = [
                {"id": fid, "faces": [faces[i] for i in by_id[fid]["faces"]]} for fid in requested
            ]
        else:
            known = _FAMILIES | groups.keys()
            requested = []
            for raw in families.split(","):
                name = raw.strip().lower().replace(" ", "_").replace("-", "_")
                if not name:
                    continue
                family = next(
                    (f for f in (name, name + "s", name + "es", name[:-1] + "ies") if f in known),
                    None,
                )
                if family is None:
                    raise ValueError(
                        f"unknown family {name!r}; available: {', '.join(sorted(known))}"
                    )
                requested.append(family)
            rows = [
                {"id": f["id"], "family": f["family"], **compact(f["record"])}
                for f in features
                if f["family"] in requested
            ]
        returned = _bounded_rows(rows, max_features)
        return json.dumps(
            {
                "features": returned,
                "matched": len(rows),
                "returned": len(returned),
                "truncated": len(returned) < len(rows),
            },
            separators=(",", ":"),
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)[:1500]})
