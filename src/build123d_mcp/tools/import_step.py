import json
import os

from build123d_mcp.tools._paths import check_input_size, safe_input_path

_STEP_EXTS = frozenset({".step", ".stp"})
_STL_EXTS = frozenset({".stl"})
_3MF_EXTS = frozenset({".3mf"})
_ALLOWED_EXTS = _STEP_EXTS | _STL_EXTS | _3MF_EXTS


def import_cad_file(session, path: str, name: str = "") -> str:
    # Reject reads outside the allowed roots (traversal / symlink escape)
    # before touching the filesystem, mirroring safe_output_path on writes.
    resolved = safe_input_path(path)
    # Reject an oversized file before the (expensive) OCC import begins (#189).
    check_input_size(resolved, "cad")
    if not os.path.isfile(resolved):
        raise ValueError(f"File not found: '{path}'")
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in _ALLOWED_EXTS:
        raise ValueError(f"Expected a .step, .stp, .stl, or .3mf file, got '{ext}'")

    obj_name = name or os.path.splitext(os.path.basename(resolved))[0]

    if ext in _STEP_EXTS:
        shapes = [_load_step(resolved)]
        fmt = "step"
    elif ext in _STL_EXTS:
        shapes = [_load_stl(resolved)]
        fmt = "stl"
    else:
        shapes = _load_3mf(resolved)
        fmt = "3mf"

    if not shapes:
        raise ValueError(f"{fmt.upper()} file contains no geometry")
    shape = shapes[0] if len(shapes) == 1 else _compound(shapes)
    session.objects[obj_name] = shape
    session.current_shape = shape
    member_names = []
    if len(shapes) > 1:
        for index, member in enumerate(shapes, start=1):
            member_name = f"{obj_name}_{index}"
            session.objects[member_name] = member
            member_names.append(member_name)

    result = _shape_summary(shape)
    result.update({"imported": obj_name, "format": fmt, "path": resolved})
    if member_names:
        result["members"] = [
            {"name": member_name, **_shape_summary(member)}
            for member_name, member in zip(member_names, shapes, strict=True)
        ]
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


def _load_3mf(resolved: str) -> list:
    from build123d import Mesher

    shapes = Mesher().read(resolved)
    if not shapes:
        raise ValueError("3MF file contains no geometry")
    return shapes


def _compound(shapes: list):
    from build123d import Compound

    return Compound(shapes)


def _shape_summary(shape) -> dict:
    bb = shape.bounding_box()
    return {
        "volume": round(shape.volume, 4),
        "solids": len(shape.solids()),
        "faces": len(shape.faces()),
        "edges": len(shape.edges()),
        "vertices": len(shape.vertices()),
        "bbox": {
            "xsize": round(bb.size.X, 4),
            "ysize": round(bb.size.Y, 4),
            "zsize": round(bb.size.Z, 4),
        },
    }
