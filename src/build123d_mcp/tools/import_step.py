import json
import os

from build123d_mcp.tools._paths import (
    check_archive_expansion,
    check_input_size,
    safe_input_path,
)

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
        check_archive_expansion(resolved, "cad")
        shapes = _load_3mf(resolved)
        fmt = "3mf"

    if not shapes:
        raise ValueError(f"{fmt.upper()} file contains no geometry")
    shape = shapes[0] if len(shapes) == 1 else _compound(shapes)
    member_names = (
        tuple(f"{obj_name}_{index}" for index in range(1, len(shapes) + 1))
        if len(shapes) > 1
        else ()
    )
    result = _shape_summary(shape)
    result.update({"imported": obj_name, "format": fmt, "path": resolved})
    if member_names:
        result["members"] = [
            {"name": member_name, **_shape_summary(member)}
            for member_name, member in zip(member_names, shapes, strict=True)
        ]

    owned_before = set(session.object_groups.get(obj_name, ()))
    owned_elsewhere = {
        member
        for aggregate, members in session.object_groups.items()
        if aggregate != obj_name
        for member in members
    }
    if obj_name in owned_elsewhere:
        raise ValueError(
            f"Cannot import as '{obj_name}': that name is a member of another imported aggregate."
        )
    collisions = sorted(
        member_name
        for member_name in member_names
        if member_name in session.objects and member_name not in owned_before
    )
    if collisions:
        raise ValueError(
            "Cannot register 3MF members because these generated names already exist: "
            + ", ".join(collisions)
        )

    for old_member in owned_before:
        session.objects.pop(old_member, None)
    session.objects[obj_name] = shape
    # Single-object imports have empty member_names; multi-object imports pair 1:1.
    for member_name, member in zip(member_names, shapes, strict=False):
        session.objects[member_name] = member
    if member_names:
        session.object_groups[obj_name] = member_names
    else:
        session.object_groups.pop(obj_name, None)
    session.current_shape = shape
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
