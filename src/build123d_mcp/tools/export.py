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


def _n_solids(shape) -> int:
    try:
        return len(shape.solids())
    except Exception:  # noqa: BLE001 - no solid topology to reason about
        return 0


def _raw_step_write(shape, abs_path: str, *, single_solid: bool = False) -> bool:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    transfer_shape = shape.solids()[0] if single_solid else shape
    writer = STEPControl_Writer()
    writer.Transfer(transfer_shape.wrapped, STEPControl_AsIs)
    return writer.Write(abs_path) == IFSelect_ReturnStatus.IFSelect_RetDone


def _single_solid_step_is_flat(abs_path: str, n_solids: int) -> bool:
    return n_solids != 1 or _nauo_count(abs_path) == 0


def _flat_single_solid_copy(shape):
    """Return one located solid as identity-location geometry for flat CAF STEP."""
    from build123d import Solid
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.TopLoc import TopLoc_Location

    solid = shape.solids()[0]
    wrapped = solid.wrapped
    loc = wrapped.Location()
    if not loc.IsIdentity():
        wrapped = BRepBuilderAPI_Transform(wrapped, loc.Transformation(), True, True).Shape()
        wrapped.Location(TopLoc_Location())

    recon = Solid(wrapped)
    recon.label = getattr(shape, "label", "") or ""
    color = getattr(shape, "color", None)
    if color is not None:
        recon.color = color
    return recon


def _write_step(shape, abs_path: str) -> None:
    """Write a STEP, working around build123d's high-level writer failing.

    ``export_step`` goes through ``STEPCAFControl_Writer`` (the CAF writer that
    carries names/colours). On build123d 0.11 that path raises
    ``RuntimeError: Failed to write STEP file`` on a solid that came straight from
    ``import_step`` (gumyr/build123d#1356) — hit ~38% of editing-fixture runs,
    where the agent imports a STEP and re-exports the edited solid.

    The obvious retry — wrap the solid in a ``Compound`` — gets it through the CAF
    path, but a ``Compound`` with one child is written as an *assembly*
    (``PRODUCT('COMPOUND')`` -> child + ``NEXT_ASSEMBLY_USAGE_OCCURRENCE``). A CAD
    kernel (SolidWorks, Inventor) then opens the file as a one-component assembly,
    not a part: the body sits off the part origin under a nested component and can
    import blank in a stock document. Mesh viewers hide this because they flatten
    product structure, so it only bites in a real kernel.

    #1356 is about the build123d ``Solid`` wrapper, not the geometry. For one
    solid, accept the CAF writer only when the written file is actually flat
    (no ``NEXT_ASSEMBLY_USAGE_OCCURRENCE``); otherwise bake any location into the
    one solid and retry the CAF writer so names/colours survive. A genuine
    multi-solid keeps its ``Compound``, where the assembly structure is correct.
    Raw ``STEPControl_Writer`` is the last resort — single product, but drops CAF
    names/colours.
    """
    from build123d import export_step

    n_solids = _n_solids(shape)

    try:
        export_step(shape, abs_path)
        if _single_solid_step_is_flat(abs_path, n_solids):
            return
    except Exception:  # noqa: BLE001 - CAF writer failed; work around #1356 below
        pass

    if n_solids == 1:
        # Single part: reconstruct the ``Solid`` so the CAF writer takes
        # import-derived solids, and bake non-identity locations into the geometry
        # so fresh located wrappers do not become one-component assemblies.
        try:
            recon = _flat_single_solid_copy(shape)
            export_step(recon, abs_path)
            if _single_solid_step_is_flat(abs_path, n_solids):
                return
        except Exception:  # noqa: BLE001 - fall through to the raw writer
            pass

        if _raw_step_write(shape, abs_path, single_solid=True) and _single_solid_step_is_flat(
            abs_path, n_solids
        ):
            return
    else:
        # Genuine multi-solid: a ``Compound`` is the right structure, and wrapping
        # also gets it through #1356 with names intact. Constructing a ``Compound``
        # reparents its children, so save/restore ``shape.parent`` to leave the
        # caller's live session object untouched.
        try:
            from build123d import Compound

            saved_parent = shape.parent
            try:
                export_step(Compound(children=[shape]), abs_path)
                return
            finally:
                shape.parent = saved_parent
        except Exception:  # noqa: BLE001 - raw geometry-only fallback
            pass

        if _raw_step_write(shape, abs_path):
            return

    raise RuntimeError(
        "Failed to write STEP file (build123d export_step, the single-solid "
        "reconstruct retry / multi-solid Compound-wrap retry, and the raw "
        "STEPControl_Writer fallback all failed)"
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


def _nauo_count(step_path: str) -> int:
    """How many assembly component links (``NEXT_ASSEMBLY_USAGE_OCCURRENCE``) a
    STEP file declares. Zero means a flat, single-product part."""
    try:
        with open(step_path, encoding="utf-8", errors="ignore") as f:
            return f.read().count("NEXT_ASSEMBLY_USAGE_OCCURRENCE")
    except Exception:  # noqa: BLE001 - unreadable file is flagged by the gate already
        return 0


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
                "a CAD scorer would reject this file (score zero). Fix the solid and re-export "
                "(the build123d://skill/repair resource has the defect-class repair ladder)."
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
            from build123d_mcp.tools.validate import MeshGateResult, _run_mesh_gate_subprocess

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
                mesh_override=_mesh if _mesh is not None else MeshGateResult.unchecked(),
            )
            if not report["passes_gate"]:
                suffix += (
                    "\n⚠ VALIDITY GATE FAIL — a CAD scorer would reject this file (score zero): "
                    + "; ".join(report["reasons"])
                    + ". Fix the solid and re-export (run validate() for detail; the "
                    "build123d://skill/repair resource has the defect-class repair ladder)."
                )
            elif report.get("mesh_check") == "skipped":
                suffix += (
                    "\n⚠ NOTE — the part was too large to mesh-check within the time "
                    "budget, so only B-rep checks ran; a mesh-level defect (open/"
                    "non-manifold/refined face-tessellation) would not be caught here."
                )
        else:
            # STL-only export (no STEP path to hand the subprocess): gate in-process.
            report = _gate_report(gate_shape, exact=True)
            if not report["passes_gate"]:
                suffix += (
                    "\n⚠ VALIDITY GATE FAIL — a CAD scorer would reject this file (score zero): "
                    + "; ".join(report["reasons"])
                    + ". Fix the solid and re-export (run validate() for detail; the "
                    "build123d://skill/repair resource has the defect-class repair ladder)."
                )

        # A single solid must land as a single STEP product. The #1356 ``Compound``
        # workaround (or any stray wrapper) writes it as ``PRODUCT('COMPOUND')`` ->
        # child + ``NEXT_ASSEMBLY_USAGE_OCCURRENCE``, which a CAD kernel opens as a
        # one-component assembly rather than a part. Cheap to check off the file we
        # re-imported.
        if step_path is not None and gate_shape is not None:
            try:
                if len(gate_shape.solids()) == 1 and _nauo_count(step_path) > 0:
                    suffix += (
                        "\n⚠ STRUCTURE — one solid written as a one-component assembly "
                        "(STEP carries NEXT_ASSEMBLY_USAGE_OCCURRENCE); a CAD kernel opens "
                        "this as an assembly, not a part. Re-export as a single solid."
                    )
            except Exception:  # noqa: BLE001 - structure hint is best-effort
                pass
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
