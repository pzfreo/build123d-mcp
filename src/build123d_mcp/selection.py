"""Stateless analytic selection of live faces, in the model's own frame."""

import math


def _descriptor(face):
    from build123d import CenterOf
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    surface = BRepAdaptor_Surface(face.wrapped)
    kind = face.geom_type.name.lower()
    row = {"kind": kind, "centroid": tuple(face.center(CenterOf.MASS)), "area": face.area}
    if kind == "plane":
        row["direction"] = tuple(face.normal_at())
    elif kind in {"cylinder", "cone", "sphere", "torus"}:
        analytic = getattr(surface, kind.capitalize())()
        row["direction"] = analytic.Position().Axis().Direction().Coord()
        row["origin"] = analytic.Location().Coord()
        if kind in {"cylinder", "sphere"}:
            row["radius"] = analytic.Radius()
        elif kind == "cone":
            row["radius"] = analytic.RefRadius()
            row["semi_angle"] = analytic.SemiAngle()
        else:
            row["radius"] = analytic.MinorRadius()
            row["major_radius"] = analytic.MajorRadius()
            row["minor_radius"] = analytic.MinorRadius()
    return row


def select_faces(
    shape, *, faces=None, kind=None, centroid=None, area=None, direction=None, radius=None, tol=None
):
    """Return a ShapeList of live faces by geometry, never by STEP face id.

    Paste one descriptor dict or a list from inspect_features(feature_ids=...).
    Each descriptor must uniquely match; keyword filters return every match.
    All supplied geometric fields are checked; descriptor id is metadata only.
    Plane directions are outward normals; curved-surface axes are sign independent.
    Origins on cylinder/cone axes match by distance to the axis (STEP may move
    the axis origin along itself).

    Defaults: length max(1e-5 mm, bbox diagonal * 1e-7), area
    max(1e-5 mm², face area * 1e-7), unit direction/angle 1e-6.
    A finite nonnegative tol overrides all three absolute tolerances (mm,
    mm², and unit-vector distance/radians respectively).
    """
    from build123d import ShapeList

    if tol is not None and (not math.isfinite(tol) or tol < 0):
        raise ValueError("select_faces: tol must be finite and nonnegative")
    length_tol = tol if tol is not None else max(1e-5, shape.bounding_box().diagonal * 1e-7)
    direction_tol = tol if tol is not None else 1e-6
    filters = {
        "kind": kind,
        "centroid": centroid,
        "area": area,
        "direction": direction,
        "radius": radius,
    }
    filters = {k: v for k, v in filters.items() if v is not None}
    descriptors = [faces] if isinstance(faces, dict) else faces
    if descriptors is not None and not isinstance(descriptors, (list, tuple)):
        raise ValueError("select_faces: faces must be a descriptor dict or a list of dicts")

    def vector(value):
        if len(value) != 3 or not all(math.isfinite(v) for v in value):
            raise ValueError("select_faces: expected a finite three-component vector")
        return tuple(value)

    def unit(value):
        value = vector(value)
        norm = math.sqrt(sum(v * v for v in value))
        if norm == 0:
            raise ValueError("select_faces: direction must be nonzero")
        return tuple(v / norm for v in value)

    def matches(actual, wanted):
        unknown = wanted.keys() - {
            "id",
            "kind",
            "centroid",
            "area",
            "direction",
            "origin",
            "radius",
            "major_radius",
            "minor_radius",
            "semi_angle",
        }
        if unknown:
            raise ValueError(f"select_faces: unsupported descriptor fields: {sorted(unknown)}")
        for key, value in wanted.items():
            if key == "id":
                continue
            if key not in actual:
                return False
            found = actual[key]
            if key == "kind":
                if str(value).lower() == "other" and found not in {
                    "plane",
                    "cylinder",
                    "cone",
                    "torus",
                }:
                    continue
                if found != str(value).lower():
                    return False
            elif key == "direction":
                a, b = unit(found), unit(value)
                distance = math.dist(a, b)
                if actual["kind"] != "plane":
                    distance = min(distance, math.dist(a, tuple(-v for v in b)))
                if distance > direction_tol:
                    return False
            elif key in {"centroid", "origin"}:
                a, b = vector(found), vector(value)
                delta = tuple(x - y for x, y in zip(a, b, strict=True))
                if key == "origin" and actual["kind"] in {"cylinder", "cone"}:
                    axis = unit(actual["direction"])
                    along = sum(x * y for x, y in zip(delta, axis, strict=True))
                    delta = tuple(x - along * y for x, y in zip(delta, axis, strict=True))
                if math.sqrt(sum(v * v for v in delta)) > length_tol:
                    return False
            else:
                tolerance = length_tol
                if key == "area":
                    tolerance = tol if tol is not None else max(1e-5, abs(found) * 1e-7)
                elif key == "semi_angle":
                    tolerance = direction_tol
                if not math.isfinite(value) or abs(found - value) > tolerance:
                    return False
        return True

    live = [(face, _descriptor(face)) for face in shape.faces()]
    if descriptors is None:
        return ShapeList(face for face, row in live if matches(row, filters))
    selected = []
    for index, descriptor in enumerate(descriptors):
        if not isinstance(descriptor, dict) or not descriptor.keys() - {"id"}:
            raise ValueError(f"select_faces: descriptor {index} must contain geometric properties")
        found = [face for face, row in live if matches(row, descriptor)]
        if len(found) != 1:
            raise ValueError(
                f"select_faces: descriptor {index} {descriptor!r} matched {len(found)} faces; "
                "expected exactly 1 (refine the descriptor or tolerance)"
            )
        if matches(_descriptor(found[0]), filters) and found[0] not in selected:
            selected.append(found[0])
    return ShapeList(selected)
