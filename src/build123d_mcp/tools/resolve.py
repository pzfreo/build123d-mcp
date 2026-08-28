import json

from build123d_mcp.security import check_ast, make_restricted_builtins

# Per-entity detail for a list-valued selector is capped: the descriptor is read by
# an agent, and an unbounded dump of every matched face is the kind of output #437
# is about. The count is always exact.
_MAX_LIST_ENTITIES = 50


def _xyz(vec) -> list[float]:
    return [round(vec.X, 6), round(vec.Y, 6), round(vec.Z, 6)]


def _geom_type(shape) -> str:
    return str(shape.geom_type).rsplit(".", 1)[-1]


def _entity_center(shape):
    """The centre a caller means when they ask where an entity is.

    build123d's default is ``CenterOf.GEOMETRY``, the parametric midpoint. On a
    closed curve or a cylindrical surface that midpoint lies ON the entity, a full
    radius from the axis — so resolve() named a hole's wall a radius away from the
    hole while planar faces stayed correct, which is what made it easy to miss
    (#456). Use the arc centre for a circular edge and the area/mass centroid
    otherwise; for a full cylinder that centroid is the axis point.
    """
    from build123d import CenterOf, GeomType
    from build123d import Edge as _Edge

    if isinstance(shape, _Edge) and shape.geom_type in (GeomType.CIRCLE, GeomType.ELLIPSE):
        return shape.arc_center
    try:
        return shape.center(CenterOf.MASS)
    except TypeError:
        # ShapeList.center() takes no argument and returns an aggregate that
        # inherits the same offset; callers average _entity_center instead.
        return shape.center()


def _radius_of(shape):
    try:
        radius = shape.radius
    except Exception:
        return None
    return round(float(radius), 6) if radius is not None else None


def _describe(result) -> dict:
    """Geometry fields for one resolved entity (or list of them)."""
    from build123d import Edge as _Edge
    from build123d import Face as _Face
    from build123d import GeomType, ShapeList

    out: dict = {}

    if isinstance(result, _Face):
        out["geom_type"] = _geom_type(result)
        out["area"] = round(result.area, 6)
        out["center"] = _xyz(_entity_center(result))
        if result.geom_type == GeomType.PLANE:
            try:
                out["normal"] = _xyz(result.normal_at())
            except Exception:
                pass
        else:
            # A curved face has no single normal. normal_at() answers for one
            # surface point, which reads as though it described the whole face, so
            # report the surface's axis instead where it has one — that is also the
            # value a caller wants for a mating axis or a hole callout.
            try:
                # A sphere has no distinguished axis — every axis through its
                # centre is one, so reporting the parametrisation's +Z would be
                # the same kind of arbitrary answer as a cylinder's "normal".
                # Its centre and radius already say everything.
                axis = None if result.geom_type == GeomType.SPHERE else result.axis_of_rotation
                if axis is not None:
                    out["axis"] = {
                        "origin": _xyz(axis.position),
                        "direction": _xyz(axis.direction),
                    }
            except Exception:
                pass
            radius = _radius_of(result)
            if radius is not None:
                out["radius"] = radius
        return out

    if isinstance(result, _Edge):
        out["geom_type"] = _geom_type(result)
        out["length"] = round(result.length, 6)
        out["center"] = _xyz(_entity_center(result))
        radius = _radius_of(result)
        if radius is not None:
            out["radius"] = radius
        return out

    if isinstance(result, ShapeList):
        # A list-valued selector used to collapse to one aggregate centre with no
        # count, no per-entity data and no area, so there was no way to tell how
        # many entities matched (#456).
        out["count"] = len(result)
        # Averaged from the corrected per-entity centres, so the aggregate and the
        # individual descriptors cannot disagree — and over EVERY entity, not just
        # the ones detailed below, since a centre taken from the first 50 of 121
        # would be a sample presented as the whole.
        centers = []
        for item in result:
            try:
                centers.append(_xyz(_entity_center(item)))
            except Exception:
                centers = []
                break
        if centers:
            out["center"] = [round(sum(c[i] for c in centers) / len(centers), 6) for i in range(3)]
        entities = []
        for item in result[:_MAX_LIST_ENTITIES]:
            try:
                entities.append(_describe(item))
            except Exception:
                pass
        if entities:
            out["entities"] = entities
            if len(result) > _MAX_LIST_ENTITIES:
                out["entities_truncated"] = True
        return out

    try:
        out["center"] = _xyz(_entity_center(result))
    except Exception:
        pass
    return out


def resolve(session, object_name: str, selector: str, label: str = "") -> str:
    """Evaluate a selector expression against a named object and return a face/edge descriptor.

    Args:
        object_name: name from show()
        selector: Python expression suffix, e.g. ".faces().filter_by(Axis.Z).last()"
        label: optional name to store the descriptor in session.geometry_refs

    Returns:
        JSON descriptor with label, ref, object, selector, type, geom_type,
        area/length and center. A planar face also carries its normal; a curved
        face carries the surface axis and radius instead, because a curved face
        has no single normal. A circular edge carries its radius, and its center
        is the arc centre rather than a point on the arc. A list-valued selector
        carries a count.
    """
    if not object_name or object_name not in session.objects:
        return json.dumps(
            {
                "error": f"Unknown object '{object_name}'.",
                "registered": list(session.objects.keys()),
            }
        )

    obj = session.objects[object_name]

    # Build a namespace with build123d imports plus the shape as `obj`
    try:
        import build123d as _bd
        from build123d import (  # noqa: F401
            Axis,
            Compound,
            Edge,
            Face,
            Shape,
            ShapeList,
            Solid,
            Vector,
            Vertex,
        )

        namespace = {k: getattr(_bd, k) for k in dir(_bd) if not k.startswith("_")}
    except ImportError as exc:
        return json.dumps({"error": f"build123d import failed: {exc}"})

    namespace["obj"] = obj

    # The selector is model/user-controlled (resolve() is MCP-exposed), so route
    # it through the same sandbox checks execute() uses: reject dunder traversal,
    # blocked builtins, and disallowed imports before evaluating (issue #186).
    expression = f"obj{selector}"
    try:
        check_ast(expression)
    except ValueError as exc:
        return json.dumps(
            {
                "error": f"Selector rejected: {exc}",
                "selector": selector,
            }
        )

    namespace["__builtins__"] = make_restricted_builtins()
    try:
        result = eval(expression, namespace)  # noqa: S307
    except Exception as exc:
        return json.dumps(
            {
                "error": f"Selector evaluation failed: {exc}",
                "selector": selector,
            }
        )

    # Build descriptor
    type_name = type(result).__name__
    descriptor: dict = {
        "label": label or "",
        "ref": f"@cad[{object_name}#{label}]" if label else f"@cad[{object_name}#{selector}]",
        "object": object_name,
        "selector": selector,
        "type": type_name,
    }

    try:
        descriptor.update(_describe(result))
    except Exception:
        pass

    if label:
        session.geometry_refs[label] = descriptor

    return json.dumps(descriptor, indent=2)
