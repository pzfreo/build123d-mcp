import copy
import struct

from build123d_mcp.tools._paths import safe_output_path

_VALID_FORMATS = ("step", "stl", "dxf", "svg")


def _labelled_copy(shape, label: str):
    """Return a shallow copy of `shape` with `.label` set, preserving any
    existing color. Used to carry session names through to the exported
    file without mutating the original shape in session.objects."""
    c = copy.copy(shape)
    c.label = label
    return c


def _resolve_shape(session, object_name: str):
    if object_name == "*":
        if not session.objects:
            raise ValueError("No named objects in session. Use show() to register shapes first.")
        from build123d import Compound

        children = [_labelled_copy(s, name) for name, s in session.objects.items()]
        return Compound(label="assembly", children=children)
    if object_name:
        if object_name not in session.objects:
            raise ValueError(
                f"Unknown object '{object_name}'. Registered: {list(session.objects.keys())}"
            )
        return _labelled_copy(session.objects[object_name], object_name)
    if session.current_shape is None:
        raise ValueError("No shape in session. Execute code to create geometry first.")
    return session.current_shape


def _stl_write(shape, abs_path: str) -> None:
    verts, tris = shape.tessellate(0.001, 0.1)

    with open(abs_path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", len(tris)))
        for tri in tris:
            v0 = verts[tri[0]]
            v1 = verts[tri[1]]
            v2 = verts[tri[2]]
            # flat normal via cross product
            ax, ay, az = v1.X - v0.X, v1.Y - v0.Y, v1.Z - v0.Z
            bx, by, bz = v2.X - v0.X, v2.Y - v0.Y, v2.Z - v0.Z
            nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            if length > 0:
                nx, ny, nz = nx / length, ny / length, nz / length
            f.write(struct.pack("<3f", nx, ny, nz))
            for v in (v0, v1, v2):
                f.write(struct.pack("<3f", v.X, v.Y, v.Z))
            f.write(b"\x00\x00")  # attribute byte count


def _is_2d(shape) -> bool:
    """True if the shape has no solid content (Sketch, Compound of edges, etc.).
    Used to decide whether to write 2D formats (DXF/SVG) or fall through to
    3D formats (STEP/STL)."""
    try:
        return len(shape.solids()) == 0
    except Exception:
        return False


def _write_dxf(shape, abs_path: str) -> None:
    """Write a 2D shape (Sketch, Compound of edges/sketches) to DXF."""
    from build123d import ExportDXF

    label = getattr(shape, "label", "") or "drawing"
    exporter = ExportDXF()
    exporter.add_layer(label)
    exporter.add_shape(shape, layer=label)
    exporter.write(abs_path)


def _write_svg(shape, abs_path: str) -> None:
    """Write a 2D shape (Sketch, Compound of edges/sketches) to SVG."""
    from build123d import ExportSVG

    label = getattr(shape, "label", "") or "drawing"
    exporter = ExportSVG(margin=5)
    exporter.add_layer(label, line_weight=0.4)
    exporter.add_shape(shape, layer=label)
    exporter.write(abs_path)


def _write_one(shape, abs_path: str, fmt: str) -> None:
    if fmt == "step":
        from build123d import export_step

        export_step(shape, abs_path)
    elif fmt == "stl":
        _stl_write(shape, abs_path)
    elif fmt == "dxf":
        _write_dxf(shape, abs_path)
    elif fmt == "svg":
        _write_svg(shape, abs_path)
    else:
        raise ValueError(f"Unknown format '{fmt}'")


def export_file(session, filename: str, format: str = "step", object_name: str = "") -> str:
    shape = _resolve_shape(session, object_name)

    formats = [f.strip().lower() for f in format.split(",") if f.strip()]
    if not formats:
        raise ValueError("No format specified.")
    unknown = [f for f in formats if f not in _VALID_FORMATS]
    if unknown:
        raise ValueError(f"Unknown format(s) '{', '.join(unknown)}'. Use: step, stl, dxf, svg")

    # Sanity: 2D shapes can only export 2D formats; 3D shapes can only export 3D.
    is_2d = _is_2d(shape)
    if is_2d:
        bad_2d = [f for f in formats if f in ("step", "stl")]
        if bad_2d:
            raise ValueError(
                f"Cannot export 2D shape as {bad_2d}. Use 'dxf' or 'svg' for 2D drawings."
            )
    else:
        bad_3d = [f for f in formats if f in ("dxf", "svg")]
        if bad_3d:
            raise ValueError(
                f"Cannot export 3D shape as {bad_3d}. Use 'step' or 'stl' for 3D solids; "
                f'use render_view(format="dxf") for the projected 2D outline.'
            )

    exported = []
    for fmt in formats:
        path = filename
        ext_for_fmt = {"step": ".step", "stl": ".stl", "dxf": ".dxf", "svg": ".svg"}[fmt]
        existing_exts = (".step", ".stp") if fmt == "step" else (ext_for_fmt,)
        if not path.lower().endswith(existing_exts):
            path += ext_for_fmt
        abs_path = safe_output_path(path)
        _write_one(shape, abs_path, fmt)
        exported.append(abs_path)

    if len(exported) == 1:
        return f"Exported to {exported[0]}"
    return "Exported to:\n" + "\n".join(exported)
