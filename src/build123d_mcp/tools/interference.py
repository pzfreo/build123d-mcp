import json
from typing import Any


def _compute_interference(shape_a: Any, shape_b: Any) -> tuple:
    try:
        inter = shape_a & shape_b
        vol = inter.volume
    except Exception:
        return (False, 0.0, None)

    if vol < 1e-6:
        return (False, 0.0, None)

    bb = inter.bounding_box()
    return (
        True,
        vol,
        {
            "xmin": bb.min.X,
            "xmax": bb.max.X,
            "ymin": bb.min.Y,
            "ymax": bb.max.Y,
            "zmin": bb.min.Z,
            "zmax": bb.max.Z,
        },
    )


def interference(session, object_a: str, object_b: str) -> str:
    for name in (object_a, object_b):
        if not name or name not in session.objects:
            raise ValueError(f"Unknown object '{name}'. Registered: {list(session.objects.keys())}")

    shape_a = session.objects[object_a]
    shape_b = session.objects[object_b]

    interferes, volume, bounds = _compute_interference(shape_a, shape_b)

    if not interferes:
        return json.dumps({"interferes": False, "volume": 0.0}, indent=2)

    return json.dumps(
        {
            "interferes": True,
            "volume": volume,
            "bounds": bounds,
        },
        indent=2,
    )
