import json


def resolve(session, object_name: str, selector: str, label: str = "") -> str:
    """Evaluate a selector expression against a named object and return a face/edge descriptor.

    Args:
        object_name: name from show()
        selector: Python expression suffix, e.g. ".faces().filter_by(Axis.Z).last()"
        label: optional name to store the descriptor in session.geometry_refs

    Returns:
        JSON descriptor with label, ref, object, selector, type, area/length, center,
        and (for Face) normal.
    """
    if not object_name or object_name not in session.objects:
        return json.dumps({
            "error": f"Unknown object '{object_name}'.",
            "registered": list(session.objects.keys()),
        })

    obj = session.objects[object_name]

    # Build a namespace with build123d imports plus the shape as `obj`
    try:
        from build123d import (  # noqa: F401
            Axis, Edge, Face, Shape, Compound, Solid, Vector, Vertex,
            ShapeList,
        )
        import build123d as _bd
        namespace = {k: getattr(_bd, k) for k in dir(_bd) if not k.startswith("_")}
    except ImportError as exc:
        return json.dumps({"error": f"build123d import failed: {exc}"})

    namespace["obj"] = obj

    try:
        result = eval(f"obj{selector}", namespace)  # noqa: S307
    except Exception as exc:
        return json.dumps({
            "error": f"Selector evaluation failed: {exc}",
            "selector": selector,
        })

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
        from build123d import Face as _Face, Edge as _Edge
        if isinstance(result, _Face):
            descriptor["area"] = round(result.area, 6)
            bb = result.bounding_box()
            cen = result.center()
            descriptor["center"] = [round(cen.X, 6), round(cen.Y, 6), round(cen.Z, 6)]
            try:
                n = result.normal_at()
                descriptor["normal"] = [round(n.X, 6), round(n.Y, 6), round(n.Z, 6)]
            except Exception:
                pass
        elif isinstance(result, _Edge):
            descriptor["length"] = round(result.length, 6)
            cen = result.center()
            descriptor["center"] = [round(cen.X, 6), round(cen.Y, 6), round(cen.Z, 6)]
        else:
            # Vertex or other
            try:
                cen = result.center()
                descriptor["center"] = [round(cen.X, 6), round(cen.Y, 6), round(cen.Z, 6)]
            except Exception:
                pass
    except Exception:
        pass

    if label:
        session.geometry_refs[label] = descriptor

    return json.dumps(descriptor, indent=2)
