import json

from build123d_mcp.tools.diff import _shape_diag
from build123d_mcp.tools.measure import _center_of_mass


def shape_compare(session, object_a: str, object_b: str) -> str:
    if object_a not in session.objects:
        raise ValueError(f"Unknown object '{object_a}'. Registered: {list(session.objects.keys())}")
    if object_b not in session.objects:
        raise ValueError(f"Unknown object '{object_b}'. Registered: {list(session.objects.keys())}")

    sa, sb = session.objects[object_a], session.objects[object_b]
    da, db = _shape_diag(sa), _shape_diag(sb)

    ca, cb = _center_of_mass(sa), _center_of_mass(sb)
    offset = round(
        ((cb["x"] - ca["x"]) ** 2 + (cb["y"] - ca["y"]) ** 2 + (cb["z"] - ca["z"]) ** 2) ** 0.5, 4
    )

    return json.dumps(
        {
            "a": {"name": object_a, **da, "center": ca},
            "b": {"name": object_b, **db, "center": cb},
            "delta": {
                "volume": round(db["volume"] - da["volume"], 4),
                "faces": db["faces"] - da["faces"],
                "edges": db["edges"] - da["edges"],
                "vertices": db["vertices"] - da["vertices"],
                "bbox": [round(db["bbox"][i] - da["bbox"][i], 4) for i in range(3)],
                "center_offset": offset,
            },
        },
        indent=2,
    )
