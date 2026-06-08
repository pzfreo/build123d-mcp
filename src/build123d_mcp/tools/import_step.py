import json
import os

from build123d_mcp.tools._paths import safe_input_path

_STEP_EXTS = frozenset({".step", ".stp"})
_STL_EXTS = frozenset({".stl"})
_ALLOWED_EXTS = _STEP_EXTS | _STL_EXTS


def import_cad_file(session, path: str, name: str = "") -> str:
    # Reject reads outside the allowed roots (traversal / symlink escape)
    # before touching the filesystem, mirroring safe_output_path on writes.
    resolved = safe_input_path(path)
    if not os.path.isfile(resolved):
        raise ValueError(f"File not found: '{path}'")
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in _ALLOWED_EXTS:
        raise ValueError(f"Expected a .step, .stp, or .stl file, got '{ext}'")

    obj_name = name or os.path.splitext(os.path.basename(resolved))[0]

    if ext in _STEP_EXTS:
        shape = _load_step(resolved)
        fmt = "step"
    else:
        shape = _load_stl(resolved)
        fmt = "stl"

    session.objects[obj_name] = shape
    session.current_shape = shape

    bb = shape.bounding_box()
    result = {
        "imported": obj_name,
        "format": fmt,
        "path": resolved,
        "volume": round(shape.volume, 4),
        "faces": len(shape.faces()),
        "edges": len(shape.edges()),
        "vertices": len(shape.vertices()),
        "bbox": {
            "xsize": round(bb.size.X, 4),
            "ysize": round(bb.size.Y, 4),
            "zsize": round(bb.size.Z, 4),
        },
    }
    return json.dumps(result, indent=2)


def _load_step(resolved: str):
    from build123d import import_step as _import_step

    imported = _import_step(resolved)
    # Multi-body STEP returns an iterable without a .wrapped attribute
    if hasattr(imported, "__iter__") and not hasattr(imported, "wrapped"):
        shapes = list(imported)
        if not shapes:
            raise ValueError("STEP file contains no geometry")
        shape = shapes[0]
        for s in shapes[1:]:
            shape = shape + s  # type: ignore[assignment]
        return shape
    return imported


def _load_stl(resolved: str):
    from build123d import import_stl

    return import_stl(resolved)
