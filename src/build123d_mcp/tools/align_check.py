import json


def align_check(session, object_a: str, object_b: str, axis: str = "Z", mode: str = "flush") -> str:
    """Check alignment between two named objects along an axis.

    Args:
        object_a: name from show()
        object_b: name from show()
        axis: "X", "Y", or "Z"
        mode:
            "flush"     — signed distance between bbox extremes on axis
                          (positive = A extends further in axis direction)
            "center"    — offset between bbox centroids projected onto axis
            "clearance" — gap between nearest faces on axis
                          (positive = apart, negative = overlap)

    Returns:
        JSON: {delta, axis, mode, object_a, object_b, interpretation}
    """
    for name in (object_a, object_b):
        if not name or name not in session.objects:
            return json.dumps(
                {
                    "error": f"Unknown object '{name}'.",
                    "registered": list(session.objects.keys()),
                }
            )
    return json.dumps(
        _align_check(
            session.objects[object_a], session.objects[object_b], axis, mode, object_a, object_b
        ),
        indent=2,
    )


def _align_check(
    shape_a, shape_b, axis: str = "Z", mode: str = "flush", name_a="A", name_b="B"
) -> dict:
    """Pure alignment computation on two shapes — shared by the tool and the in-namespace
    align_check primitive (#366). Returns {delta, axis, mode, object_a, object_b,
    interpretation}, or {error} for a bad axis/mode/bbox."""
    axis = axis.upper()
    object_a, object_b = name_a, name_b
    if axis not in ("X", "Y", "Z"):
        return {"error": f"Invalid axis '{axis}'. Use X, Y, or Z."}
    if mode not in ("flush", "center", "clearance"):
        return {"error": f"Invalid mode '{mode}'. Use flush, center, or clearance."}

    try:
        bb_a = shape_a.bounding_box()
        bb_b = shape_b.bounding_box()
    except Exception as exc:
        return {"error": f"Failed to compute bounding box: {exc}"}

    if axis == "X":
        a_min, a_max = bb_a.min.X, bb_a.max.X
        b_min, b_max = bb_b.min.X, bb_b.max.X
        a_cen = (a_min + a_max) / 2.0
        b_cen = (b_min + b_max) / 2.0
    elif axis == "Y":
        a_min, a_max = bb_a.min.Y, bb_a.max.Y
        b_min, b_max = bb_b.min.Y, bb_b.max.Y
        a_cen = (a_min + a_max) / 2.0
        b_cen = (b_min + b_max) / 2.0
    else:  # Z
        a_min, a_max = bb_a.min.Z, bb_a.max.Z
        b_min, b_max = bb_b.min.Z, bb_b.max.Z
        a_cen = (a_min + a_max) / 2.0
        b_cen = (b_min + b_max) / 2.0

    if mode == "flush":
        delta = round(a_max - b_max, 6)
        if abs(delta) < 1e-4:
            interp = f"{object_a} and {object_b} are flush on {axis} axis."
        elif delta > 0:
            interp = (
                f"{object_a} extends {abs(delta):.4g} mm further than {object_b} on {axis} axis."
            )
        else:
            interp = (
                f"{object_b} extends {abs(delta):.4g} mm further than {object_a} on {axis} axis."
            )

    elif mode == "center":
        delta = round(a_cen - b_cen, 6)
        if abs(delta) < 1e-4:
            interp = f"Centers of {object_a} and {object_b} are aligned on {axis} axis."
        else:
            interp = (
                f"Center of {object_a} is {abs(delta):.4g} mm "
                f"{'above' if delta > 0 else 'below'} center of {object_b} on {axis} axis."
            )

    else:  # clearance
        # Gap between nearest faces: b_min - a_max (A below B) vs a_min - b_max (B below A)
        # Positive = gap, negative = overlap
        gap1 = b_min - a_max  # gap if A is below B on axis
        gap2 = a_min - b_max  # gap if B is below A on axis
        # Use the one with the smaller absolute value (the actual nearest-face gap)
        if abs(gap1) <= abs(gap2):
            delta = round(gap1, 6)
        else:
            delta = round(gap2, 6)
        if delta > 1e-4:
            interp = f"{object_a} and {object_b} are {abs(delta):.4g} mm apart on {axis} axis."
        elif delta < -1e-4:
            interp = f"{object_a} and {object_b} overlap by {abs(delta):.4g} mm on {axis} axis."
        else:
            interp = f"{object_a} and {object_b} are touching on {axis} axis."

    return {
        "delta": delta,
        "axis": axis,
        "mode": mode,
        "object_a": object_a,
        "object_b": object_b,
        "interpretation": interp,
    }
