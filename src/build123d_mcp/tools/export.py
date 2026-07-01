import copy
import struct
import time

from build123d_mcp.tools._budget import op_budget
from build123d_mcp.tools._paths import safe_output_path

# Headroom under the op budget for the in-process B-rep checks + subprocess
# teardown + parent poll granularity; and the least time worth starting the
# out-of-process mesh gate with. Matches the locate/shape_compare convention.
_EXPORT_MESH_MARGIN_S = 15
_EXPORT_MESH_MIN_S = 10

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


def _write_step(shape, abs_path: str) -> None:
    """Write a STEP, resilient to build123d's high-level writer failing.

    build123d's ``export_step`` goes through ``STEPCAFControl_Writer`` (the CAF
    writer that carries colours/layers/names). On build123d 0.11.0 that path
    raises ``RuntimeError: Failed to write STEP file`` on a solid that came
    straight from ``import_step`` (gumyr/build123d#1356) — observed on ~38% of
    editing-fixture runs, where the agent imports a STEP and exports the (valid)
    edited solid. Two fallbacks, best first:

    1. **Wrap in a ``Compound`` and retry ``export_step``.** The same solid
       writes through the CAF path once wrapped, so body names/colours survive.
    2. **Raw ``STEPControl_Writer``.** Writes the geometry but drops CAF
       names/colours.

    Geometry round-trips identically in all three; a CAD scorer needs the
    geometry, and the wrap retry additionally keeps the CAF metadata when it can.
    """
    from build123d import export_step

    try:
        export_step(shape, abs_path)
        return
    except Exception:  # noqa: BLE001 - high-level-writer failure → try the wrap retry
        pass

    # Wrap-and-retry (gumyr/build123d#1356). Constructing a Compound reparents its
    # children, so save/restore ``shape.parent`` to keep the fallback free of side
    # effects on the caller's shape (which may be the live session object).
    try:
        from build123d import Compound

        _saved_parent = shape.parent
        try:
            export_step(Compound(children=[shape]), abs_path)
            return
        finally:
            shape.parent = _saved_parent
    except Exception:  # noqa: BLE001 - still failing → raw geometry-only fallback
        pass

    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(shape.wrapped, STEPControl_AsIs)
    if writer.Write(abs_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(
            "Failed to write STEP file (build123d export_step, the Compound-wrap "
            "retry, and the raw STEPControl_Writer fallback all failed)"
        )


def _write_one(shape, abs_path: str, fmt: str) -> None:
    if fmt == "step":
        _write_step(shape, abs_path)
    elif fmt == "stl":
        _stl_write(shape, abs_path)
    elif fmt == "dxf":
        _write_dxf(shape, abs_path)
    elif fmt == "svg":
        _write_svg(shape, abs_path)
    else:
        raise ValueError(f"Unknown format '{fmt}'")


def export_file(session, filename: str, format: str = "step", object_name: str = "") -> str:
    _t0 = time.monotonic()  # op entry — used to bound the out-of-process gate
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

    # Echo what was written so the (typically final) export step doubles as a
    # sanity check that the right, non-degenerate object landed in the file (#241).
    sanity = _sanity_line(shape)
    suffix = f"\n{sanity}" if sanity else ""
    # For 3D solids, run the validity gate: a CAD scorer rejects a non-watertight
    # / non-manifold / non-solid STEP or STL outright (score zero), so flag it at
    # the last possible moment rather than letting an invalid artifact ship.
    if not is_2d:
        from build123d_mcp.tools.validate import _gate_report

        # Gate the WRITTEN-AND-REIMPORTED STEP, not the in-memory shape. A CAD
        # scorer re-imports the file, and serialization can degrade a shape that
        # passed in memory (drop a solid, break BRep validity) — so validating the
        # in-memory object gives a false PASS while shipping an invalid file. Re-
        # import what we just wrote and gate that; it is the authoritative artifact
        # (export runs once, so the extra import + exact mesh check is fine).
        step_path = next((p for p in exported if p.lower().endswith((".step", ".stp"))), None)
        gate_shape = shape
        if step_path is not None:
            try:
                from build123d import import_step

                gate_shape = import_step(step_path)
            except Exception:
                gate_shape = None  # not even loadable — a scorer would reject it
        if gate_shape is None:
            suffix += (
                "\n⚠ VALIDITY GATE FAIL — the written STEP could not be re-imported; "
                "a CAD scorer would reject this file (score zero). Fix the solid and re-export."
            )
        elif step_path is not None:
            # Run the mesh check OUT OF PROCESS for STEP exports. The mesh stitch is
            # dominated by OCC BRepMesh — an un-interruptible native call no
            # in-process budget can stop — so running it in-process risks blocking
            # the worker past the op-timeout (which kills the session). Bound it by
            # the time LEFT in this op's budget (NOT the full budget — the re-import
            # and B-rep checks already spent some), so the subprocess is always
            # killed before the parent kills the worker. B-rep checks run in-process
            # (cheap). On timeout the mesh check is skipped (B-rep only) + a warning.
            from build123d_mcp.tools.validate import _run_mesh_gate_subprocess

            # Margin covers the B-rep checks that still run in-process after this
            # (fast — BRepCheck, not meshing) + subprocess teardown + parent poll
            # granularity, so worker total stays under the parent op-budget.
            _remaining = op_budget(session) - (time.monotonic() - _t0) - _EXPORT_MESH_MARGIN_S
            _mesh = (
                _run_mesh_gate_subprocess(step_path, timeout=_remaining)
                if _remaining >= _EXPORT_MESH_MIN_S
                else None
            )
            report = _gate_report(
                gate_shape,
                exact=True,
                mesh_override=_mesh if _mesh is not None else (0, 0, 0, 0, False),
            )
            if not report["passes_gate"]:
                suffix += (
                    "\n⚠ VALIDITY GATE FAIL — a CAD scorer would reject this file (score zero): "
                    + "; ".join(report["reasons"])
                    + ". Fix the solid and re-export (run validate() for detail)."
                )
            elif report.get("mesh_check") == "skipped":
                suffix += (
                    "\n⚠ NOTE — the part was too large to mesh-check within the time "
                    "budget, so only B-rep checks ran; a mesh-level defect (open/"
                    "non-manifold) would not be caught here."
                )
        else:
            # STL-only export (no STEP path to hand the subprocess): gate in-process.
            report = _gate_report(gate_shape, exact=True)
            if not report["passes_gate"]:
                suffix += (
                    "\n⚠ VALIDITY GATE FAIL — a CAD scorer would reject this file (score zero): "
                    + "; ".join(report["reasons"])
                    + ". Fix the solid and re-export (run validate() for detail)."
                )
    if len(exported) == 1:
        return f"Exported to {exported[0]}{suffix}"
    return "Exported to:\n" + "\n".join(exported) + suffix


def _sanity_line(shape) -> str:
    try:
        bb = shape.bounding_box()
        size = f"{bb.size.X:.4g}×{bb.size.Y:.4g}×{bb.size.Z:.4g} mm"
        if _is_2d(shape):
            return f"2D drawing: bbox {size}, {len(shape.edges())} edges"
        return f"volume {shape.volume:.4g} mm³, bbox {size}, {len(shape.faces())} faces"
    except Exception:
        return ""
