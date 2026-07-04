import math
import os
import sys

_xvfb_started = False


def _ensure_display() -> None:
    """Spawn Xvfb on Linux when no DISPLAY is set, mirroring what
    pyvista.start_xvfb() did before pyvista 0.48 removed it.

    Idempotent. No-op on macOS/Windows (which have a display) or when
    DISPLAY is already set. If the Xvfb binary is missing, leaves
    DISPLAY unset and lets the VTK render fail; the caller's SVG
    fallback then takes over.
    """
    global _xvfb_started
    if _xvfb_started or sys.platform != "linux" or os.environ.get("DISPLAY"):
        return

    import random
    import shutil
    import subprocess
    import time

    if not shutil.which("Xvfb"):
        return

    for _ in range(5):
        display_num = random.randint(100, 999)
        lock_file = f"/tmp/.X{display_num}-lock"
        if os.path.exists(lock_file):
            continue
        try:
            proc = subprocess.Popen(
                ["Xvfb", f":{display_num}", "-screen", "0", "1024x768x24", "-ac"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return

        for _ in range(30):
            time.sleep(0.1)
            if os.path.exists(lock_file):
                break

        if proc.poll() is None and os.path.exists(lock_file):
            os.environ["DISPLAY"] = f":{display_num}"
            _xvfb_started = True
            return

        try:
            proc.kill()
        except Exception:
            pass


_PALETTE = [
    "lightblue",
    "lightcoral",
    "lightgreen",
    "lightyellow",
    "plum",
    "peachpuff",
    "lightcyan",
]

_QUALITY = {
    "standard": {"linear_deflection": 0.001, "angular_deflection": 0.1},
    "high": {"linear_deflection": 0.0005, "angular_deflection": 0.02},
}

_VALID_FORMATS = ("png", "svg", "dxf", "both")

# Wall-clock budget for tessellating ALL of a render's shapes out-of-process.
# Kept safely below WorkerSession._RENDER_TIMEOUT (120s) so the bounded subprocess
# is always killed — returning a clean error with the session intact — before the
# parent op-timeout SIGKILLs the whole worker (which would destroy session state).
# OCC BRepMesh is un-interruptible, so this is the only way to bound it; the
# worker is a daemon, so it must be a real subprocess, not multiprocessing.
_TESS_BUDGET_S = 75
# VTK's macOS render, likewise bounded in a real subprocess (#357). The two stages
# run sequentially, so _TESS_BUDGET_S + _VTK_BUDGET_S + _RENDER_OVERHEAD_MARGIN_S
# must stay <= WorkerSession._RENDER_TIMEOUT, so an overrun in either fires its own
# clean guard before the parent's watchdog SIGKILLs the whole session. The margin
# covers parent-side work (per-shape STEP export, pickle, subprocess spawn, PNG
# read) that runs outside either budget; test_worker_timeouts pins the inequality.
_VTK_BUDGET_S = 60
_RENDER_OVERHEAD_MARGIN_S = 15


class _RenderBudgetExceeded(RuntimeError):
    """A render stage (tessellation or VTK) blew its own subprocess budget. Raised
    instead of a bare RuntimeError so render_view can surface it directly rather
    than spending the *remaining* op-budget on the unbounded SVG fallback — which,
    after both stages are already near their limits, could push the op past the
    parent watchdog and SIGKILL the session (the very failure the budgets prevent)."""


def _resolve_shapes(session, objects: str):
    """Return list of (name, shape, color_or_None) tuples based on objects selector.

    Each entry in the comma-separated objects string may be 'name' or 'name:color'.
    """
    if objects:
        result = []
        for entry in [e.strip() for e in objects.split(",") if e.strip()]:
            if ":" in entry:
                name, color = entry.split(":", 1)
                name, color = name.strip(), color.strip()
            else:
                name, color = entry, None
            if name not in session.objects:
                raise ValueError(f"Unknown object(s): {name}")
            result.append((name, session.objects[name], color))
        return result
    if session.objects:
        return [(n, s, None) for n, s in session.objects.items()]
    if session.current_shape is not None:
        return [("shape", session.current_shape, None)]
    raise ValueError("No shape in session. Execute code to create geometry first.")


def _color_to_rgb(name: str) -> tuple[float, float, float]:
    from matplotlib.colors import to_rgb

    return to_rgb(name)


def _camera_direction(
    direction: str,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return (position_unit_vector, view_up) for the named view direction."""
    if direction == "top":
        return (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)
    if direction == "front":
        return (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)
    if direction == "side":
        return (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    return (1.0, 1.0, 1.0), (0.0, 0.0, 1.0)  # iso


def _resolve_object_labels(shapes) -> list[tuple[tuple[float, float, float], str]]:
    """Return [(position, name)] for each rendered shape with a non-default name."""
    out = []
    for name, shape, _color in shapes:
        if not name or name == "shape":
            # auto-name from bare current_shape — labelling it "shape" adds noise
            continue
        try:
            c = shape.center()
            out.append(((c.X, c.Y, c.Z), name))
        except Exception:
            try:
                bb = shape.bounding_box()
                c = bb.center
                out.append(((c.X, c.Y, c.Z), name))
            except Exception:
                pass
    return out


def _resolve_highlights(
    session, shapes, highlights
) -> list[tuple[tuple[float, float, float], str]]:
    """Validate and resolve highlight specs to [(position, label)].

    Each highlight must be a dict with keys: object, type, index, label.
    Raises ValueError for any malformed or unresolvable entry.
    """
    if not highlights:
        return []
    rendered_names = {name for name, _, _ in shapes}
    out = []
    for h in highlights:
        if not isinstance(h, dict):
            raise ValueError(f"highlight must be a dict, got {type(h).__name__}: {h!r}")
        missing = [k for k in ("object", "type", "index", "label") if k not in h]
        if missing:
            raise ValueError(f"highlight missing required key(s) {missing}: {h!r}")
        obj_name = h["object"]
        ent_type = h["type"]
        index = h["index"]
        label = str(h["label"])

        if obj_name not in session.objects:
            raise ValueError(
                f"highlight references unknown object '{obj_name}'. "
                f"Register it first with show(shape, '{obj_name}')."
            )
        if obj_name not in rendered_names:
            raise ValueError(
                f"highlight references '{obj_name}' which is registered but not in the rendered set. "
                f"Add it to objects= or omit the highlight."
            )
        if ent_type not in ("face", "edge", "vertex"):
            raise ValueError(
                f"highlight type must be 'face', 'edge', or 'vertex', got '{ent_type}'"
            )
        if not isinstance(index, int):
            raise ValueError(f"highlight index must be int, got {type(index).__name__}: {index!r}")

        shape = session.objects[obj_name]
        items = {"face": shape.faces(), "edge": shape.edges(), "vertex": shape.vertices()}[ent_type]
        n = len(items)
        if not (0 <= index < n):
            raise ValueError(
                f"highlight {ent_type} index {index} out of range for '{obj_name}' (valid: 0..{n - 1})"
            )
        entity = items[index]
        try:
            c = entity.center()
            position = (c.X, c.Y, c.Z)
        except Exception:
            try:
                position = (entity.X, entity.Y, entity.Z)
            except Exception as exc:
                raise ValueError(
                    f"could not compute position for {obj_name}.{ent_type}[{index}]: {exc}"
                )
        out.append((position, label))
    return out


def _add_label_actors(renderer, labels) -> None:
    """Add billboard text actors at each (position, text) pair.

    The renderer should be a depth-cleared overlay layer so labels at interior
    points (e.g. a solid's centroid) aren't occluded by the geometry.
    """
    if not labels:
        return
    import vtk

    for position, text in labels:
        actor = vtk.vtkBillboardTextActor3D()
        actor.SetPosition(*position)
        actor.SetInput(str(text))
        prop = actor.GetTextProperty()
        prop.SetFontSize(16)
        prop.SetColor(0.0, 0.0, 0.0)
        prop.SetBold(True)
        prop.SetBackgroundColor(1.0, 1.0, 1.0)
        prop.SetBackgroundOpacity(0.85)
        prop.SetFrame(True)
        prop.SetFrameColor(0.2, 0.2, 0.2)
        renderer.AddActor(actor)


def _vtk_render_tesselated(
    shape_data, direction, clip_plane, clip_at, azimuth, elevation, labels=None
) -> bytes:
    """Pure VTK render from pre-tessellated mesh data.

    shape_data: list of (name, [(x,y,z), ...], [(i,j,k), ...], color_or_None)

    Separated from _do_render_png so it can be called in a subprocess on macOS,
    where VTK's Cocoa context creation freezes the window server on cold-start.
    """
    import tempfile

    import vtk

    # Silence VTK's warning stream before any VTK object is created. On macOS the
    # Cocoa backend floods the caller's terminal with "Failed to get alpha color
    # buffer size" warnings via its own output channel, escaping stderr
    # redirection (#208). These warnings do not affect the rendered output.
    # Guard to macOS only: on Linux, VTK uses its warning channel for OSMesa/EGL
    # init failures; silencing it there would make blank-PNG returns undiagnosable.
    if sys.platform == "darwin":
        vtk.vtkObject.GlobalWarningDisplayOff()

    _ensure_display()

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(1.0, 1.0, 1.0)

    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
    render_window.SetSize(800, 600)
    render_window.AddRenderer(renderer)

    # Labels live on an overlay renderer that draws after the depth buffer is
    # cleared, so a label sitting at a solid's centroid stays readable instead
    # of being occluded by the surrounding geometry.
    label_renderer = None
    if labels:
        label_renderer = vtk.vtkRenderer()
        label_renderer.SetActiveCamera(renderer.GetActiveCamera())
        label_renderer.SetLayer(1)
        render_window.SetNumberOfLayers(2)
        render_window.AddRenderer(label_renderer)

    actor_count = 0

    for i, (name, vert_tuples, tri_list, obj_color) in enumerate(shape_data):
        points = vtk.vtkPoints()
        for v in vert_tuples:
            points.InsertNextPoint(v[0], v[1], v[2])

        cells = vtk.vtkCellArray()
        for tri in tri_list:
            cells.InsertNextCell(3)
            cells.InsertCellPoint(tri[0])
            cells.InsertCellPoint(tri[1])
            cells.InsertCellPoint(tri[2])

        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)

        if clip_plane:
            if clip_at is not None:
                origin = {"x": (clip_at, 0, 0), "y": (0, clip_at, 0), "z": (0, 0, clip_at)}[
                    clip_plane
                ]
            else:
                bounds = poly.GetBounds()  # xmin, xmax, ymin, ymax, zmin, zmax
                cx = (bounds[0] + bounds[1]) / 2
                cy = (bounds[2] + bounds[3]) / 2
                cz = (bounds[4] + bounds[5]) / 2
                origin = {"x": (cx, 0, 0), "y": (0, cy, 0), "z": (0, 0, cz)}[clip_plane]
            normal = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[clip_plane]

            plane = vtk.vtkPlane()
            plane.SetOrigin(*origin)
            plane.SetNormal(*normal)

            clipper = vtk.vtkClipPolyData()
            clipper.SetInputData(poly)
            clipper.SetClipFunction(plane)
            clipper.SetInsideOut(False)
            clipper.Update()
            poly = clipper.GetOutput()

        # Compute vertex normals so Phong shading works on both B-rep tessellations
        # and imported mesh shells (STL), where face orientations may be inconsistent.
        normals_filter = vtk.vtkPolyDataNormals()
        normals_filter.SetInputData(poly)
        normals_filter.ComputePointNormalsOn()
        normals_filter.ConsistencyOn()
        normals_filter.AutoOrientNormalsOn()
        normals_filter.Update()
        poly = normals_filter.GetOutput()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        r, g, b = _color_to_rgb(obj_color if obj_color else _PALETTE[i % len(_PALETTE)])
        prop = actor.GetProperty()
        prop.SetColor(r, g, b)
        prop.SetAmbient(0.3)
        prop.SetDiffuse(0.7)
        prop.SetSpecular(0.2)
        prop.SetInterpolationToPhong()  # smooth shading

        renderer.AddActor(actor)
        actor_count += 1

    if actor_count == 0:
        raise RuntimeError("No geometry to render")

    if label_renderer is not None:
        _add_label_actors(label_renderer, labels)

    # Camera setup
    camera = renderer.GetActiveCamera()
    camera.SetParallelProjection(False)
    pos, up = _camera_direction(direction)
    camera.SetPosition(*pos)
    camera.SetFocalPoint(0.0, 0.0, 0.0)
    camera.SetViewUp(*up)
    renderer.ResetCamera()

    if azimuth != 0.0 or elevation != 0.0:
        camera.Azimuth(azimuth)
        camera.Elevation(elevation)
        camera.OrthogonalizeViewUp()
        renderer.ResetCameraClippingRange()

    render_window.Render()

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(render_window)
    w2i.Update()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        png_path = os.path.join(tmpdir, "render.png")
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(png_path)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()

        with open(png_path, "rb") as f:
            return f.read()


def _tessellate_in_process(shapes, tess) -> tuple[dict, list[str]]:
    """Tessellate in the current process — the fallback for hosts that block
    child-process creation (#143 / InProcessSession), where ``subprocess.run``
    raises ``OSError``. Unbounded, but those hosts also run no worker op-timeout,
    so there is no worker to SIGKILL — nothing to bound against."""
    meshes: dict = {}
    failed: list[str] = []
    for name, shape, _color in shapes:
        try:
            verts, tris = shape.tessellate(tess["linear_deflection"], tess["angular_deflection"])
            meshes[name] = ([(v.X, v.Y, v.Z) for v in verts], [list(t) for t in tris])
        except Exception as exc:  # noqa: BLE001 - skip a shape that won't tessellate
            failed.append(f"{name}: {exc}")
    return meshes, failed


def _tessellate_shapes_bounded(shapes, tess) -> tuple[dict, list[str]]:
    """Tessellate every shape OUT OF PROCESS, hard-bounded by ``_TESS_BUDGET_S``.

    Each shape is written to a temp STEP, then ONE subprocess imports and
    tessellates all of them (interpreter startup amortised across the render). On
    timeout the subprocess is hard-killed and a clean RuntimeError is raised — the
    worker and the whole session survive, instead of the un-interruptible BRepMesh
    blowing the op-timeout and SIGKILLing the session. Returns
    ``({name: (verts_xyz, tris)}, [failure strings])``.
    """
    import json
    import os
    import pickle
    import subprocess
    import sys
    import tempfile

    from build123d_mcp.tools.export import _write_step

    work = tempfile.mkdtemp(prefix="b123d_tess_")
    out_pkl = os.path.join(work, "meshes.pkl")
    man_path = os.path.join(work, "manifest.json")
    temp_files = [out_pkl, man_path]
    manifest, failed = [], []
    try:
        for i, (name, shape, _color) in enumerate(shapes):
            step = os.path.join(work, f"s{i}.step")
            try:
                _write_step(shape, step)
                manifest.append({"name": name, "step": step})
                temp_files.append(step)
            except Exception as exc:  # noqa: BLE001 - skip an un-exportable shape
                failed.append(f"{name}: export for render failed: {exc}")
        if not manifest:
            return {}, failed
        with open(man_path, "w") as f:
            json.dump(manifest, f)
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build123d_mcp._tessellate_subprocess",
                    man_path,
                    out_pkl,
                    repr(tess["linear_deflection"]),
                    repr(tess["angular_deflection"]),
                ],
                capture_output=True,
                text=True,
                timeout=_TESS_BUDGET_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise _RenderBudgetExceeded(
                f"Rendering exceeded the {_TESS_BUDGET_S}s tessellation budget — this part is too "
                "complex to render at this quality. Try quality='standard', render fewer objects, "
                "or inspect it numerically with measure()/cross_sections()."
            ) from exc
        except OSError:
            # The host blocks child-process creation (#143 / InProcessSession) —
            # no subprocess available, and no worker op-timeout to kill us, so fall
            # back to tessellating in-process (the pre-subprocess behaviour).
            meshes, in_failed = _tessellate_in_process(shapes, tess)
            return meshes, failed + in_failed
        if proc.returncode != 0 or not os.path.exists(out_pkl):
            raise RuntimeError("Tessellation subprocess failed: " + (proc.stderr or "")[-300:])
        try:
            with open(out_pkl, "rb") as f:
                result = pickle.load(f)
            meshes = result["meshes"]
            sub_failed = result.get("failed", [])
        except (pickle.UnpicklingError, EOFError, KeyError, TypeError) as exc:
            raise RuntimeError(
                "Tessellation produced an unreadable result (the render may be too large or "
                "complex). Try quality='standard' or render fewer objects."
            ) from exc
        return meshes, failed + sub_failed
    finally:
        for p in temp_files:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass


def _do_render_png(
    shapes, tess, direction, clip_plane, clip_at, azimuth, elevation, labels=None
) -> tuple[bytes, list[str]]:
    # Tessellate shapes out-of-process (bounded) so an un-interruptible BRepMesh
    # on a complex part can't blow the op-timeout and SIGKILL the session.
    meshes, failed = _tessellate_shapes_bounded(shapes, tess)
    shape_data = [
        (name, meshes[name][0], meshes[name][1], obj_color)
        for name, shape, obj_color in shapes
        if name in meshes
    ]

    if not shape_data:
        msg = (
            "All shapes failed to tessellate: " + "; ".join(failed)
            if failed
            else "No geometry to render"
        )
        raise RuntimeError(msg)

    if sys.platform == "darwin":
        png_bytes = _vtk_render_subprocess(
            shape_data, direction, clip_plane, clip_at, azimuth, elevation, labels
        )
    else:
        png_bytes = _vtk_render_tesselated(
            shape_data, direction, clip_plane, clip_at, azimuth, elevation, labels
        )
    return png_bytes, failed


def _vtk_render_subprocess(
    shape_data, direction, clip_plane, clip_at, azimuth, elevation, labels, timeout=_VTK_BUDGET_S
) -> bytes:
    """Render the tessellated shapes with VTK in an isolated subprocess (macOS).

    VTK's Cocoa backend touches the macOS window server on first context creation,
    which can freeze GUI apps — and hang — when called from a non-foreground
    process. Running it in a real ``subprocess.run`` child (a daemon worker cannot
    spawn ``multiprocessing`` children — the #357 bug, where this whole isolation
    silently degraded to an unbounded in-process render) both isolates the freeze
    and hard-bounds the render: on ``timeout`` the child is killed and a clean
    ``RuntimeError`` is raised, so the worker and its session survive instead of
    the parent's ``_RENDER_TIMEOUT`` watchdog SIGKILLing the whole session.

    ``timeout`` (``_VTK_BUDGET_S``) + ``_TESS_BUDGET_S`` must stay under
    ``WorkerSession._RENDER_TIMEOUT`` so this guard always fires first.
    """
    import os
    import pickle
    import subprocess
    import sys
    import tempfile

    work = tempfile.mkdtemp(prefix="b123d_vtk_")
    in_pkl = os.path.join(work, "in.pkl")
    out_png = os.path.join(work, "out.png")
    try:
        with open(in_pkl, "wb") as f:
            pickle.dump(
                {
                    "shape_data": shape_data,
                    "direction": direction,
                    "clip_plane": clip_plane,
                    "clip_at": clip_at,
                    "azimuth": azimuth,
                    "elevation": elevation,
                    "labels": labels,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build123d_mcp._vtk_render_subprocess_worker",
                    in_pkl,
                    out_png,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise _RenderBudgetExceeded(
                f"VTK rendering exceeded the {timeout}s budget — the offscreen render did not "
                "complete. Try quality='standard', render fewer objects, or a different direction."
            ) from exc
        except OSError:
            # Host blocks child-process creation (#143 / InProcessSession): no
            # subprocess, and no worker op-timeout to kill us — render in-process.
            return _vtk_render_tesselated(
                shape_data, direction, clip_plane, clip_at, azimuth, elevation, labels
            )
        if proc.returncode != 0 or not os.path.exists(out_png):
            raise RuntimeError("VTK render subprocess failed: " + (proc.stderr or "")[-300:])
        with open(out_png, "rb") as f:
            return f.read()
    finally:
        for p in (in_pkl, out_png):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass


def _viewport_origin_for(direction: str, shapes, azimuth: float, elevation: float):
    """Compute (origin, up, look_at) for build123d's project_to_viewport.

    Distance is chosen large enough to be effectively orthographic. Azimuth/
    elevation rotate the iso baseline around the look-at point. For top/front/
    side they rotate around the cardinal axis-aligned baseline.
    """
    from build123d import Vector

    # Aggregate bounding centre across all shapes for look_at
    centres = [shape.center() for _name, shape, _c in shapes]
    centre = sum(centres, Vector(0, 0, 0)) * (1.0 / len(centres))

    # Direction vector matches the VTK camera baseline
    dx, dy, dz = {
        "top": (0.0, 0.0, 1.0),
        "front": (0.0, -1.0, 0.0),
        "side": (1.0, 0.0, 0.0),
    }.get(direction, (1.0, 1.0, 1.0))
    up = (0.0, 1.0, 0.0) if direction == "top" else (0.0, 0.0, 1.0)

    # Apply azimuth/elevation mirroring VTK camera semantics: Azimuth()
    # rotates the position about the view-up axis, Elevation() about the
    # camera's right axis (direction x up); up is carried along with the
    # elevation rotation (OrthogonalizeViewUp).
    def _rodrigues(v, axis, angle_rad):
        k = Vector(*axis).normalized()
        vv = Vector(*v)
        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
        out = vv * cos_a + k.cross(vv) * sin_a + k * (k.dot(vv) * (1.0 - cos_a))
        return (out.X, out.Y, out.Z)

    d = (dx, dy, dz)
    if azimuth != 0.0:
        d = _rodrigues(d, up, math.radians(azimuth))
    if elevation != 0.0:
        right = Vector(*d).cross(Vector(*up))
        if right.length > 1e-9:
            axis = (right.X, right.Y, right.Z)
            e = math.radians(elevation)
            d = _rodrigues(d, axis, e)
            up = _rodrigues(up, axis, e)
    dx, dy, dz = d

    # Distance: 10x the largest bounding extent across all shapes (effectively orthographic)
    extents = []
    for _n, s, _c in shapes:
        bb = s.bounding_box()
        extents.extend([bb.size.X, bb.size.Y, bb.size.Z])
    distance = max(extents + [1.0]) * 10.0

    origin = (
        centre.X + dx * distance,
        centre.Y + dy * distance,
        centre.Z + dz * distance,
    )
    look_at = (centre.X, centre.Y, centre.Z)
    return origin, up, look_at


def _clip_shapes(shapes, clip_plane, clip_at):
    """Split each shape at the requested plane, keeping the +normal side.

    Falls back to the unclipped shape if split is unsupported for the type.
    """
    if not clip_plane:
        return list(shapes)

    from build123d import Plane, Vector

    clipped_shapes = []
    for name, shape, color in shapes:
        if clip_at is not None:
            origin = {"x": (clip_at, 0, 0), "y": (0, clip_at, 0), "z": (0, 0, clip_at)}[clip_plane]
        else:
            c = shape.center()
            origin = {"x": (c.X, 0, 0), "y": (0, c.Y, 0), "z": (0, 0, c.Z)}[clip_plane]
        normal = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[clip_plane]
        plane = Plane(origin=Vector(*origin), z_dir=Vector(*normal))
        try:
            halves = shape.split(plane, keep=None)
            # split with keep=None returns a tuple of (positive_side, negative_side);
            # invert=False in PNG path means we keep the +normal side
            kept = halves[0] if isinstance(halves, tuple) else halves
        except Exception:
            kept = shape  # fall back to unclipped if split is unsupported for the shape type
        clipped_shapes.append((name, kept, color))
    return clipped_shapes


def _do_render_svg(shapes, direction, clip_plane, clip_at, azimuth, elevation) -> bytes:
    """Produce an SVG via build123d's HLR projection.

    Multi-shape: each shape gets its own colored layer (matching the PNG palette).
    Visible edges are drawn solid; hidden edges dashed and lighter.
    Clip plane is honoured by splitting each shape at the plane and projecting
    only the keep-side.
    """
    import tempfile

    from build123d import ExportSVG

    clipped_shapes = _clip_shapes(shapes, clip_plane, clip_at)

    origin, up, look_at = _viewport_origin_for(direction, clipped_shapes, azimuth, elevation)

    exporter = ExportSVG(margin=5, line_weight=0.25)
    for i, (name, shape, obj_color) in enumerate(clipped_shapes):
        try:
            visible, hidden = shape.project_to_viewport(
                viewport_origin=origin,
                viewport_up=up,
                look_at=look_at,
            )
        except Exception:
            continue

        from build123d import Color

        rgb = _color_to_rgb(obj_color if obj_color else _PALETTE[i % len(_PALETTE)])
        line_color = Color(*rgb)

        layer_visible = f"{name or f'shape_{i}'}_visible"
        layer_hidden = f"{name or f'shape_{i}'}_hidden"
        exporter.add_layer(layer_visible, line_color=line_color, line_weight=0.4)
        exporter.add_layer(layer_hidden, line_color=line_color, line_weight=0.15)
        if visible:
            exporter.add_shape(visible, layer=layer_visible)
        if hidden:
            exporter.add_shape(hidden, layer=layer_hidden)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        svg_path = os.path.join(tmpdir, "render.svg")
        exporter.write(svg_path)
        with open(svg_path, "rb") as f:
            return f.read()


def _do_render_dxf(shapes, direction, clip_plane, clip_at, azimuth, elevation) -> bytes:
    """Produce a DXF via build123d's HLR projection.

    DXF is the standard 2D CAD interchange format. Use it when the LLM (or a
    downstream tool) needs the projected geometry as parseable polylines
    rather than as a raster — e.g. building a matplotlib annotation overlay
    on top of a faithful base layer instead of redrawing the shape by hand.

    Each input shape becomes two layers: <name>_visible (solid) and
    <name>_hidden (dashed). Clip-plane handling mirrors the SVG path.
    """
    import tempfile

    from build123d import ExportDXF, LineType

    clipped_shapes = _clip_shapes(shapes, clip_plane, clip_at)

    origin, up, look_at = _viewport_origin_for(direction, clipped_shapes, azimuth, elevation)

    exporter = ExportDXF()
    for i, (name, shape, _obj_color) in enumerate(clipped_shapes):
        try:
            visible, hidden = shape.project_to_viewport(
                viewport_origin=origin,
                viewport_up=up,
                look_at=look_at,
            )
        except Exception:
            continue

        layer_visible = f"{name or f'shape_{i}'}_visible"
        layer_hidden = f"{name or f'shape_{i}'}_hidden"
        exporter.add_layer(layer_visible)
        exporter.add_layer(layer_hidden, line_type=LineType.HIDDEN)
        if visible:
            exporter.add_shape(visible, layer=layer_visible)
        if hidden:
            exporter.add_shape(hidden, layer=layer_hidden)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        dxf_path = os.path.join(tmpdir, "render.dxf")
        exporter.write(dxf_path)
        with open(dxf_path, "rb") as f:
            return f.read()


def _shapes_are_2d(shapes) -> bool:
    """True if every input shape is purely 2D (no solids, flat in Z).

    Sketches, edges, wires, and Compounds composed via build123d.drafting
    have no solids AND lie flat in the XY plane (bbox Z extent ≈ 0). The
    z-extent check is essential — STL imports are 3D shells with no solids
    but real Z extent, and they belong on the 3D render path.
    """
    for _name, shape, _color in shapes:
        try:
            if len(shape.solids()) > 0:
                return False
            bb = shape.bounding_box()
            if bb.size.Z > 1e-6:
                return False
        except Exception:
            return False
    return True


def _resolve_2d_colors(shapes, colors: dict | None):
    """Build the per-name colour lookup for the 2D path.

    Resolution priority for an object's part colour:
        colors[name]  >  name:color from objects=  >  shared palette
    Special layer keys:
        colors["_dims"]   — dimensions/annotations colour (default dark blue)
        colors["_labels"] — object label colour (default = dims colour)
    Returns (part_color_for_index_fn, dim_color, label_color).
    """
    from build123d import Color

    colors = colors or {}

    def _to_color(token: str, fallback: "Color") -> "Color":
        try:
            return Color(*_color_to_rgb(token))
        except Exception:
            return fallback

    default_dim = Color(0, 0.2, 0.7)
    dim_color = _to_color(colors["_dims"], default_dim) if "_dims" in colors else default_dim
    label_color = _to_color(colors["_labels"], dim_color) if "_labels" in colors else dim_color

    def part_color_for(name: str | None, supplied: str | None, index: int) -> "Color":
        if name and colors.get(name):
            token = colors[name]
        elif supplied:
            token = supplied
        else:
            token = _PALETTE[index % len(_PALETTE)]
        return _to_color(token, Color(0, 0, 0))

    return part_color_for, dim_color, label_color


def _do_render_png_2d(shapes, label_objects: bool = False, colors: dict | None = None) -> bytes:
    """Rasterise a 2D dimensioned drawing to PNG via build123d ExportSVG +
    resvg-py.

    Why this pipeline: build123d.drafting renders witness ticks and arrowheads
    as thin closed polygons (filled rectangles, not strokes). The ezdxf+matplotlib
    path renders these as outlined rectangles (a "doubled" look) and converts
    text to outlined character boundaries. ExportSVG with the right layer
    settings produces clean strokes + filled text, and resvg-py rasterises
    cleanly with bundled Rust wheels (no native cairo dependency).

    Engineering convention applied:
    - Part geometry: black, line_weight=0.5
    - Dimensions/annotations: blue, line_weight=0.05, fill_color=line_color
      (the fill_color match makes the closed-rect witness ticks render as
      solid coloured lines instead of outlines). The same trick is documented
      in the build123d://drafting cookbook so an LLM doing custom exports
      can apply it directly.
    - Background: white (cairosvg default)

    label_objects: when True, adds a Text annotation below each named
    object's bbox (not on top of it) so the LLM can identify what it's
    looking at.
    """
    import os as _os
    import re as _re
    import tempfile as _tempfile

    import resvg_py
    from build123d import ExportSVG, Text

    part_color_for, dim_color, label_color = _resolve_2d_colors(shapes, colors)

    # Augment shapes with label Text if requested.
    label_shapes = []
    if label_objects:
        for name, shape, _color in shapes:
            if not name or name == "shape":
                continue
            try:
                bb = shape.bounding_box()
                cx = (bb.min.X + bb.max.X) / 2
                label_y = bb.min.Y - 6
                txt = Text(str(name), font_size=3.0)
                # Text builds at origin; translate to position
                txt = txt.translate((cx, label_y, 0))
                label_shapes.append(txt)
            except Exception:
                continue

    exporter = ExportSVG(margin=10)
    if len(shapes) == 1:
        name, shape, obj_color = shapes[0]
        single_part_color = part_color_for(name, obj_color, 0)
        # Per-object part colour, shared blue for dimensions/annotations
        exporter.add_layer("part", line_color=single_part_color, line_weight=0.5)
        exporter.add_layer(
            "dims",
            line_color=dim_color,
            fill_color=dim_color,
            line_weight=0.05,
        )
        # Walk children: edges → part layer; Sketch faces → dims layer
        for child in getattr(shape, "children", None) or [shape]:
            try:
                if len(child.faces()) > 0:
                    exporter.add_shape(child, layer="dims")
                else:
                    exporter.add_shape(child, layer="part")
            except Exception:
                try:
                    exporter.add_shape(child, layer="part")
                except Exception:
                    continue
    else:
        # Multi-object: each named object gets its own colour-coded part layer
        # plus a per-object dims layer in the shared annotation colour. Splits
        # children the same way as the single-object path so dims read clearly
        # against any part colour.
        for i, (name, shape, obj_color) in enumerate(shapes):
            base = name if name and name != "shape" else f"shape_{i}"
            obj_part_color = part_color_for(name, obj_color, i)
            part_layer = f"{base}_part"
            dims_layer = f"{base}_dims"
            exporter.add_layer(part_layer, line_color=obj_part_color, line_weight=0.5)
            exporter.add_layer(
                dims_layer,
                line_color=dim_color,
                fill_color=dim_color,
                line_weight=0.05,
            )
            for child in getattr(shape, "children", None) or [shape]:
                try:
                    if len(child.faces()) > 0:
                        exporter.add_shape(child, layer=dims_layer)
                    else:
                        exporter.add_shape(child, layer=part_layer)
                except Exception:
                    try:
                        exporter.add_shape(child, layer=part_layer)
                    except Exception:
                        continue

    if label_shapes:
        exporter.add_layer(
            "_labels",
            line_color=label_color,
            fill_color=label_color,
            line_weight=0.05,
        )
        for txt in label_shapes:
            try:
                exporter.add_shape(txt, layer="_labels")
            except Exception:
                continue

    with _tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        svg_path = _os.path.join(tmpdir, "drawing.svg")
        exporter.write(svg_path)
        with open(svg_path) as f:
            svg = f.read()
        # resvg requires unitless or pixel sizes; build123d emits mm. Strip
        # the unit suffix from the top-level width/height attributes.
        svg = _re.sub(
            r'(width|height)="([\d.]+)(mm|cm|in)"',
            r'\1="\2"',
            svg,
            count=2,
        )
        png_data = resvg_py.svg_to_bytes(
            svg_string=svg,
            width=1200,
            background="#ffffff",
        )
        return bytes(png_data)


def render_view(
    session,
    direction: str = "iso",
    objects: str = "",
    quality: str = "standard",
    clip_plane: str = "",
    clip_at: float | None = None,
    azimuth: float = 0.0,
    elevation: float = 0.0,
    save_to: str = "",
    format: str = "png",
    label_objects: bool = False,
    highlights: list[dict] | None = None,
    colors: dict[str, str] | None = None,
    mode: str = "auto",
) -> dict:
    """Render the active session geometry.

    Returns a dict with optional keys:
      - "png": bytes of the rasterised PNG (when format in ("png","both")
        or as automatic fallback failed)
      - "svg": bytes of the SVG document (when format in ("svg","both") or
        as automatic fallback when raster rendering failed)
      - "fallback": str, present when SVG was returned in place of a
        requested PNG because the VTK renderer failed
    """
    direction = direction.lower()
    if direction not in ("top", "front", "side", "iso"):
        raise ValueError(f"Unknown direction '{direction}'. Use: top, front, side, iso")

    quality = quality.lower()
    if quality not in _QUALITY:
        raise ValueError(f"Unknown quality '{quality}'. Use: standard, high")

    clip_plane = clip_plane.lower()
    if clip_plane and clip_plane not in ("x", "y", "z"):
        raise ValueError(f"Unknown clip_plane '{clip_plane}'. Use: x, y, z")

    format = format.lower()
    if format not in _VALID_FORMATS:
        raise ValueError(f"Unknown format '{format}'. Use: png, svg, dxf, both")

    shapes = _resolve_shapes(session, objects)
    tess = _QUALITY[quality]
    result: dict = {}

    mode = mode.lower()
    if mode not in ("auto", "2d", "3d"):
        raise ValueError(f"Unknown mode '{mode}'. Use: auto, 2d, 3d")

    # Decide which render path to use. "auto" applies the heuristic; "2d"/"3d"
    # force a path and error if the shapes don't fit. result["render_mode"]
    # always reports which path actually ran so the LLM can verify.
    detected_2d = _shapes_are_2d(shapes)
    if mode == "auto":
        use_2d = detected_2d
    elif mode == "2d":
        if not detected_2d:
            raise ValueError(
                "mode='2d' was requested but at least one shape is 3D "
                "(has solids or non-zero Z extent). Use mode='auto' or "
                "mode='3d', or pass shapes built via build123d.drafting."
            )
        use_2d = True
    else:  # mode == "3d"
        if detected_2d:
            raise ValueError(
                "mode='3d' was requested but every shape is flat 2D "
                "(no solids, zero Z extent). Use mode='auto' or "
                "mode='2d', or pass an actual 3D shape."
            )
        use_2d = False

    if use_2d:
        result["render_mode"] = "2d"
        if highlights:
            result["label_warnings"] = [
                "highlights are only supported for 3D shapes; ignored for 2D drawings."
            ]
        if format in ("png", "both"):
            try:
                result["png"] = _do_render_png_2d(
                    shapes,
                    label_objects=label_objects,
                    colors=colors,
                )
            except Exception as exc:
                result["png_error"] = f"{type(exc).__name__}: {exc}"
        if format in ("svg", "both"):
            # 2D Sketches → SVG via ExportSVG with per-object colour and
            # part/dims layer split (mirrors the PNG path).
            import tempfile as _tempfile

            from build123d import ExportSVG

            part_color_for, dim_col, _label_col = _resolve_2d_colors(shapes, colors)
            with _tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                svg_exporter = ExportSVG(margin=5)
                for i, (name, shape, obj_color) in enumerate(shapes):
                    base = name if name and name != "shape" else f"shape_{i}"
                    part_col = part_color_for(name, obj_color, i)
                    part_layer = f"{base}_part"
                    dims_layer = f"{base}_dims"
                    svg_exporter.add_layer(part_layer, line_color=part_col, line_weight=0.4)
                    svg_exporter.add_layer(
                        dims_layer, line_color=dim_col, fill_color=dim_col, line_weight=0.05
                    )
                    for child in getattr(shape, "children", None) or [shape]:
                        try:
                            target = dims_layer if len(child.faces()) > 0 else part_layer
                            svg_exporter.add_shape(child, layer=target)
                        except Exception:
                            try:
                                svg_exporter.add_shape(child, layer=part_layer)
                            except Exception:
                                continue
                svg_path = os.path.join(tmp, "drawing.svg")
                svg_exporter.write(svg_path)
                with open(svg_path, "rb") as f:
                    result["svg"] = f.read()
        if format == "dxf":
            # 2D Sketches → DXF via ExportDXF. DXF colours use the small ACI
            # palette, so per-object hues are layer-only here — most DXF
            # viewers let users colour by layer in their own preferences.
            import tempfile as _tempfile

            from build123d import ExportDXF

            with _tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                dxf_exporter = ExportDXF()
                for i, (name, shape, _obj_color) in enumerate(shapes):
                    base = name if name and name != "shape" else f"shape_{i}"
                    part_layer = f"{base}_part"
                    dims_layer = f"{base}_dims"
                    dxf_exporter.add_layer(part_layer)
                    dxf_exporter.add_layer(dims_layer)
                    for child in getattr(shape, "children", None) or [shape]:
                        try:
                            target = dims_layer if len(child.faces()) > 0 else part_layer
                            dxf_exporter.add_shape(child, layer=target)
                        except Exception:
                            try:
                                dxf_exporter.add_shape(child, layer=part_layer)
                            except Exception:
                                continue
                dxf_path = os.path.join(tmp, "drawing.dxf")
                dxf_exporter.write(dxf_path)
                with open(dxf_path, "rb") as f:
                    result["dxf"] = f.read()
        if save_to:
            from build123d_mcp.tools._paths import safe_output_path

            base, ext = os.path.splitext(save_to)
            if ext.lower() in (".png", ".svg", ".dxf"):
                save_to = base
            for key, suffix in (("png", ".png"), ("svg", ".svg"), ("dxf", ".dxf")):
                if key in result:
                    abs_path = safe_output_path(save_to + suffix)
                    with open(abs_path, "wb") as f:
                        f.write(result[key])
                    # Record the on-disk path so the MCP wrapper can use it
                    # for [SEND:] markers instead of writing a duplicate
                    # tempfile copy.
                    result[f"{key}_path"] = abs_path
        return result

    result["render_mode"] = "3d"

    # Resolve labels up-front so validation errors surface before any rendering work.
    labels: list[tuple[tuple[float, float, float], str]] = []
    if label_objects:
        labels.extend(_resolve_object_labels(shapes))
    labels.extend(_resolve_highlights(session, shapes, highlights))

    if (label_objects or highlights) and format in ("svg", "dxf", "both"):
        result["label_warnings"] = [
            "Labels are only rendered in PNG output; SVG/DXF output is unlabelled."
        ]

    if format in ("png", "both"):
        try:
            png_bytes, png_failed = _do_render_png(
                shapes,
                tess,
                direction,
                clip_plane,
                clip_at,
                azimuth,
                elevation,
                labels=labels,
            )
            result["png"] = png_bytes
            if png_failed:
                result["png_warnings"] = [
                    f"Skipped shapes (tessellation failed): {', '.join(png_failed)}"
                ]
        except _RenderBudgetExceeded:
            # The render already consumed its hard budget; the unbounded SVG (HLR)
            # fallback below could push the op past the parent watchdog and kill the
            # session. Surface the clean budget error instead, session intact (#357).
            raise
        except Exception as exc:
            if format == "png":
                # Auto-fallback: produce SVG so the AI still gets a visual.
                result["svg"] = _do_render_svg(
                    shapes,
                    direction,
                    clip_plane,
                    clip_at,
                    azimuth,
                    elevation,
                )
                result["format"] = "svg"
                result["fallback"] = (
                    f"VTK raster render failed ({type(exc).__name__}: {exc}). "
                    f"Returning SVG via build123d HLR projection. "
                    f"Common causes: no DISPLAY and no OSMesa/EGL backend on a headless host."
                )
            else:
                # 'both' was requested. Record the PNG failure and continue to SVG.
                result["png_error"] = f"{type(exc).__name__}: {exc}"

    if format in ("svg", "both") and "svg" not in result:
        result["svg"] = _do_render_svg(
            shapes,
            direction,
            clip_plane,
            clip_at,
            azimuth,
            elevation,
        )

    if format == "dxf":
        result["dxf"] = _do_render_dxf(
            shapes,
            direction,
            clip_plane,
            clip_at,
            azimuth,
            elevation,
        )

    if save_to:
        from build123d_mcp.tools._paths import safe_output_path

        # Strip a known extension so format='both' produces consistent <base>.png and <base>.svg
        base, ext = os.path.splitext(save_to)
        if ext.lower() in (".png", ".svg", ".dxf"):
            save_to = base
        for key, suffix in (("png", ".png"), ("svg", ".svg"), ("dxf", ".dxf")):
            if key in result:
                abs_path = safe_output_path(save_to + suffix)
                with open(abs_path, "wb") as f:
                    f.write(result[key])
                result[f"{key}_path"] = abs_path

    return result
