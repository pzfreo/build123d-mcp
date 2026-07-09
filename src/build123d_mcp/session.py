import copy
import io
import signal
import time
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from build123d_mcp.security import (
    EXEC_TIMEOUT_SECONDS,
    ExecutionTimeout,
    check_ast,
    make_restricted_builtins,
)

# Names injected by the session itself — excluded from rollback and new-key detection.
_INJECTED = frozenset(
    {
        "__builtins__",
        "show",
        "named_face",
        "find_edges",
        "annotate",
        "register_centerline",
        "set_page",
        "save_json",
        # Analysis primitives (#366): the same computations as the measure/clearance/
        # cross_sections/find_holes tools, callable in code so results compose without
        # hand-copying numbers out of a JSON tool result.
        "measure",
        "clearance",
        "cross_sections",
        "find_holes",
        "find_bosses",
        "find_bored_bosses",
        "find_countersinks",
        "find_hole_patterns",
        "align_check",
    }
)


def _bounded_result(out: str):
    """Parse what run_bounded_shape_op returned into a Python object. The bounded path
    hands back an ``Error: ...`` string on timeout/failure — not JSON — so surface that
    as an exception, letting an in-namespace primitive fail loudly rather than return
    an unparseable string the caller would then try to subscript."""
    import json

    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError) as exc:
        # `out` is already an "Error: ..." string; strip the prefix so execute()'s own
        # "Error: {type}: {msg}" wrapping doesn't double it up.
        raise RuntimeError(out.removeprefix("Error: ")) from exc


# save_json: cap on the serialized payload, and the only characters allowed in
# a file stem (no path separators, so the write cannot leave _SAVE_JSON_DIR).
_SAVE_JSON_MAX_BYTES = 10_000_000
_SAVE_JSON_STEM = r"[A-Za-z0-9_.-]+"


class Session:
    def __init__(self, exec_timeout: int = EXEC_TIMEOUT_SECONDS):
        self.exec_timeout = exec_timeout
        self.namespace: dict[str, Any] = {}
        self.current_shape: Any = None
        self.objects: dict[str, Any] = {}
        self.snapshots: dict[str, Any] = {}
        self.last_error_detail: dict[str, Any] | None = None
        self.drawing_annotations: dict[str, Any] = {}
        self.drawing_page: dict[str, Any] | None = None
        self.geometry_refs: dict[str, Any] = {}
        self.execute_history: list[str] = []
        # Live-viewer delta tracking: name -> id(shape) at the last delta pull,
        # used to identity-diff changed shapes (see worker._op_pull_viewer_deltas).
        self._viewer_baseline: dict[str, int] = {}
        # True while the current execute() call has explicitly registered a
        # shape via show()/annotate()/register_centerline() — blocks the
        # post-exec variable scan from overriding that registration (#236).
        self._shape_explicitly_set = False
        self._inject_builtins()

    def _inject_builtins(self) -> None:
        self.namespace["__builtins__"] = make_restricted_builtins()
        objects = self.objects

        session_ref = self
        drawing_annotations = self.drawing_annotations

        def annotate(
            result: Any,
            name: str | None = None,
            label: str | None = None,
        ) -> None:
            """Register a drawing annotation for inspect_drawing.

            Accepts two flavours of input:

            1. build123d_drafting Dimension / Leader objects — extracted by
               attribute: label, measured_length, tip, elbow.
            2. Vanilla build123d.ExtensionLine / DimensionLine — measured
               length is read from the .dimension attribute that build123d
               sets during construction. The label string is NOT stored on
               the constructed object by build123d (gumyr/build123d#1315),
               so pass it explicitly via label="..." to enable lint.

            Stores annotation metadata and also calls show() so the shape
            is visible to render_view.

            Args:
                result: a Dimension, Leader, ExtensionLine,
                    DimensionLine, or any shape — anything else is registered
                    without annotation metadata.
                name: object name for the session registry. Defaults to
                    label_str (when available) or "annotation".
                label: explicit label string. Required for lint to check
                    axis swaps on vanilla ExtensionLine/DimensionLine — pass
                    the same string you gave to the ExtensionLine constructor
                    (e.g. label="40"). Omit only when you don't need lint
                    coverage. For automatic label capture without duplication
                    use Dimension() from build123d_drafting instead.
            """
            if name is None:
                name = getattr(result, "label", None) or "annotation"
            meta: dict[str, Any] = {"type": type(result).__name__}
            # Helper-library duck-typed extraction. (helpers 0.2.0 renamed the
            # text attribute label_str -> label; the stored key stays "label_str"
            # so the sidecar/inspect_drawing format is unchanged.)
            lbl = getattr(result, "label", None)
            if lbl:
                meta["label_str"] = lbl
            if getattr(result, "is_centerline", False):
                meta["is_centerline"] = True
            for attr in (
                "measured_length",
                "tip",
                "elbow",
                "label_bbox",
                "dim_level_y",
                "segments",
            ):
                val = getattr(result, attr, None)
                if val is not None:
                    meta[attr] = val
            # Vanilla build123d fallback: ExtensionLine/DimensionLine set
            # .dimension to the measured path length but do not store the
            # label string anywhere.
            if "measured_length" not in meta:
                dim = getattr(result, "dimension", None)
                if dim is not None:
                    meta["measured_length"] = dim
            if "label_str" not in meta:
                if label is not None:
                    meta["label_str"] = label
                # Do NOT auto-derive label_str from measured_length.
                # build123d doesn't expose the constructor label after
                # construction, so we can't distinguish ExtensionLine(label="99")
                # (axis-swap bug) from one built without a label. Deriving
                # label_str from measured_length makes lint always see
                # label==measured → silent false negatives. Leave label_str
                # absent so lint skips the check rather than falsely approving
                # a potentially wrong drawing. Pass label= explicitly or use
                # Dimension() from build123d_drafting to enable lint.
            # Store the dim-line level (the extreme Y away from the part edge)
            # so lint_drawing can compare levels without false positives from
            # extension lines that span from Y≈0 to the dim line.
            shape = getattr(result, "shape", result)
            if "dim_level_y" not in meta:
                try:
                    bb = shape.bounding_box()
                    # Whichever Y extreme is farther from the midpoint is the dim line.
                    if abs(bb.max.Y) >= abs(bb.min.Y):
                        meta["dim_level_y"] = bb.max.Y
                    else:
                        meta["dim_level_y"] = bb.min.Y
                except Exception:
                    pass
            drawing_annotations[name] = meta
            objects[name] = shape
            session_ref.current_shape = shape
            session_ref._shape_explicitly_set = True
            print(f"Annotated '{name}': {meta.get('label_str', '')}")

        session_ref.namespace["annotate"] = annotate

        def show(shape: Any, name: str | None = None) -> None:
            if name is None:
                name = "shape"
            objects[name] = shape
            session_ref.current_shape = shape
            session_ref._shape_explicitly_set = True
            try:
                vol = shape.volume
                faces = len(shape.faces())
                print(f"Registered '{name}': volume={vol:.4g} mm³, faces={faces}")
            except Exception:
                print(f"Registered '{name}'")

        self.namespace["show"] = show

        def set_page(width: float, height: float, margin: float = 5.0) -> None:
            """Register the drawing page extent for lint_drawing bounds checking.

            After calling set_page(), lint_drawing() will flag any annotation
            whose bounding box extends past the drawable area (page minus margin).

            Args:
                width:  page width in mm (e.g. 297 for A4 landscape).
                height: page height in mm (e.g. 210 for A4 landscape).
                margin: minimum clear border in mm (default 5). Annotations
                        must stay within (margin, margin) to (width-margin, height-margin).
            """
            session_ref.drawing_page = {
                "width": width,
                "height": height,
                "margin": margin,
                "min_x": margin,
                "min_y": margin,
                "max_x": width - margin,
                "max_y": height - margin,
            }
            print(
                f"Page set: {width}×{height} mm, margin={margin} mm "
                f"(drawable area {width - 2 * margin}×{height - 2 * margin} mm)"
            )

        self.namespace["set_page"] = set_page

        def register_centerline(shape: Any, name: str | None = None) -> None:
            """Register a centerline shape for lint_drawing() overlap detection.

            After calling register_centerline(), lint_drawing() will flag any
            dimension annotation whose label bbox overlaps this centerline.

            Args:
                shape: the centerline compound/edge (e.g. from centerline() helper
                    or Edge.make_line wrapped in Compound).
                name:  object name for the session registry.
                    Defaults to "centerline".
            """
            if name is None:
                name = "centerline"
            drawing_annotations[name] = {"type": "centerline"}
            objects[name] = shape
            session_ref.current_shape = shape
            session_ref._shape_explicitly_set = True
            print(f"Registered centerline '{name}'")

        self.namespace["register_centerline"] = register_centerline

        def named_face(shape: Any, name: str) -> Any:
            """Return a face of shape by semantic name: top/bottom/front/back/left/right."""
            from build123d import Axis

            match name.lower():
                case "top":
                    return shape.faces().sort_by(Axis.Z)[-1]
                case "bottom":
                    return shape.faces().sort_by(Axis.Z)[0]
                case "front":
                    return shape.faces().sort_by(Axis.Y)[-1]
                case "back":
                    return shape.faces().sort_by(Axis.Y)[0]
                case "right":
                    return shape.faces().sort_by(Axis.X)[-1]
                case "left":
                    return shape.faces().sort_by(Axis.X)[0]
                case _:
                    raise ValueError(
                        f"Unknown face name '{name}'. Use: top, bottom, front, back, left, right"
                    )

        self.namespace["named_face"] = named_face

        def find_edges(
            shape: Any,
            geom: str | None = None,
            radius: float | None = None,
            at_z: float | None = None,
            length: float | None = None,
            tol: float = 0.05,
        ) -> Any:
            """Filter shape.edges() by geometry type, radius, Z position, and/or
            length — the bread-and-butter selection for fillet/chamfer on turned
            parts ("the circular edge of radius 4.25 at Z=10.2") without
            hand-rolled filtering (#239).

            Args:
                shape: the part to select edges from.
                geom: edge geometry type name, e.g. 'circle', 'line', 'bspline'.
                radius: keep circular edges with this radius (within tol).
                at_z: keep edges whose center Z is within tol of this value.
                length: keep edges with this length (within tol).
                tol: tolerance for radius/at_z/length matching (default 0.05 mm).

            Returns a ShapeList; prints the match count plus the radii and Z
            levels found so a wrong selection is visible immediately.
            """
            from build123d import GeomType, ShapeList

            def _radius(e: Any) -> float | None:
                try:
                    return e.radius
                except Exception:
                    return None

            edges = shape.edges()
            if geom is not None:
                try:
                    gt = GeomType[geom.upper()]
                except KeyError:
                    names = ", ".join(g.name.lower() for g in GeomType)
                    raise ValueError(f"Unknown geom '{geom}'. Use one of: {names}") from None
                edges = edges.filter_by(gt)
            if radius is not None:
                edges = [
                    e for e in edges if (r := _radius(e)) is not None and abs(r - radius) <= tol
                ]
            if at_z is not None:
                edges = [e for e in edges if abs(e.center().Z - at_z) <= tol]
            if length is not None:
                edges = [e for e in edges if abs(e.length - length) <= tol]

            result = ShapeList(edges)
            radii = sorted({round(r, 3) for e in result if (r := _radius(e)) is not None})
            zs = sorted({round(e.center().Z, 3) for e in result})

            def _fmt(vals: list) -> str:
                return str(vals[:8])[:-1] + ", …]" if len(vals) > 8 else str(vals)

            desc = f"find_edges: {len(result)} edge(s) matched"
            if radii:
                desc += f", radii {_fmt(radii)}"
            if zs:
                desc += f", Z levels {_fmt(zs)}"
            print(desc)
            return result

        self.namespace["find_edges"] = find_edges

        def save_json(name: str, obj: Any) -> str:
            """Write obj as JSON to a server-controlled scratch file; return its path.

            The sanctioned structured-output channel (#259): the sandbox blocks
            open()/os, and large prints are fragile in transit, so analysis
            results (face inventories, hole tables, section data) should leave
            the session as a file the caller Reads back.

            Args:
                name: file stem — letters, digits, ., _, - only (no path
                    separators); the file lands in a per-process scratch dir,
                    so two servers running side by side cannot clobber each
                    other's files.
                obj: any JSON-serializable structure; non-serializable values
                    fall back to str().

            Returns the absolute path of the written .json file.
            """
            import json
            import os
            import re
            import tempfile
            from pathlib import Path

            from build123d_mcp.tools._paths import safe_output_path

            if not re.fullmatch(_SAVE_JSON_STEM, name):
                raise ValueError(
                    f"save_json name must match {_SAVE_JSON_STEM} (no path "
                    f"separators), got {name!r}"
                )
            payload = json.dumps(obj, indent=2, default=str)
            if len(payload) > _SAVE_JSON_MAX_BYTES:
                raise ValueError(
                    f"save_json payload is {len(payload)} bytes; the cap is "
                    f"{_SAVE_JSON_MAX_BYTES}. Split the data into smaller files."
                )
            out_dir = Path(tempfile.gettempdir()) / "build123d-mcp" / f"pid-{os.getpid()}"
            out_dir.mkdir(parents=True, exist_ok=True)
            # Belt and braces: route the final path through the same write
            # policy every file-writing tool uses.
            path = Path(safe_output_path(str(out_dir / f"{name}.json")))
            path.write_text(payload)
            print(f"Saved {len(payload)} bytes of JSON to {path}")
            return str(path)

        self.namespace["save_json"] = save_json

        # --- Analysis primitives (#366) -------------------------------------- #
        # Same computations as the measure/clearance/cross_sections/find_holes tools,
        # but callable in code and returning real Python objects, so an agent composes
        # over results (arithmetic, filtering) instead of hand-copying numbers out of a
        # JSON tool result. measure/clearance/cross_sections route through the SAME
        # bounded-subprocess path as the tools (run_bounded_shape_op) so a large shape
        # can't SIGKILL the session (#360); the JSON they carry is parsed back to a dict.

        def _exec_budget_left() -> int:
            # execute()'s watchdog counts from execute() start, so a primitive called
            # after heavy pre-work has only the REMAINING budget. Bound the subprocess to
            # that, not the full exec_timeout — else on Windows (no SIGALRM backstop) it
            # could outlive the parent poll and orphan the child (#376 review).
            started = getattr(session_ref, "_execute_started", None)
            if started is None:
                return session_ref.exec_timeout
            return int(session_ref.exec_timeout - (time.monotonic() - started))

        def measure(shape: Any = None, density: float = 0.0, material: str = "") -> dict:
            """Measure a shape → dict (volume, area, bbox, topology, center_of_mass,
            inertia, face_inventory). Compose in code: measure(part)["volume"]. shape
            defaults to the current shape. Large shapes run out-of-process, bounded."""
            from build123d_mcp.tools._bounded import run_bounded_shape_op
            from build123d_mcp.tools.measure import _measure_report, _resolve_density

            s = shape if shape is not None else session_ref.current_shape
            if s is None:
                raise ValueError("measure(): no shape given and no current shape in the session")
            rho = _resolve_density(density, material)
            data = _bounded_result(
                run_bounded_shape_op(
                    session_ref,
                    "measure",
                    {"": s},
                    {"rho": rho},
                    in_process=lambda: _measure_report(s, rho),
                    budget=_exec_budget_left(),  # remaining execute() budget, not op_budget
                )
            )
            print(
                f"measure: volume={data['volume']}, area={data['area']}, "
                f"faces={data['topology']['faces']}"
            )
            return data

        self.namespace["measure"] = measure

        def clearance(a: Any, b: Any) -> dict:
            """Spatial relationship between two shapes → dict (clearance, status,
            containment, intersection_volume, …). clearance(a, b)["clearance"] is a
            float you can branch on. Large shapes run out-of-process, bounded."""
            from build123d_mcp.tools._bounded import run_bounded_shape_op
            from build123d_mcp.tools.measure import _clearance_report

            data = _bounded_result(
                run_bounded_shape_op(
                    session_ref,
                    "clearance",
                    {"a": a, "b": b},
                    {},
                    in_process=lambda: _clearance_report(a, b),
                    budget=_exec_budget_left(),  # remaining execute() budget, not op_budget
                )
            )
            print(f"clearance: {data['status']}, clearance={data['clearance']}")
            return data

        self.namespace["clearance"] = clearance

        def cross_sections(shape: Any = None, axis: str = "Z", num_slices: int = 10) -> list:
            """Cross-sectional areas along an axis → list of {position, area} dicts.
            shape defaults to the current shape. Large shapes run out-of-process, bounded."""
            from build123d_mcp.tools._bounded import run_bounded_shape_op
            from build123d_mcp.tools.cross_sections import _cross_sections_report

            s = shape if shape is not None else session_ref.current_shape
            if s is None:
                raise ValueError("cross_sections(): no shape given and no current shape")
            data = _bounded_result(
                run_bounded_shape_op(
                    session_ref,
                    "cross_sections",
                    {"": s},
                    {"axis": axis, "num_slices": num_slices},
                    in_process=lambda: _cross_sections_report(s, axis, num_slices),
                    budget=_exec_budget_left(),  # remaining execute() budget, not op_budget
                )
            )
            print(f"cross_sections: {len(data)} slice(s) along {axis}")
            return data

        self.namespace["cross_sections"] = cross_sections

        def find_holes(shape: Any = None) -> list:
            """Recognise drilled holes → the recogniser's records (with .location — an
            (x, y, z) tuple — plus .diameter, .depth, .axis, …), so you can filter in
            code: [h for h in find_holes(part) if h.location[0] < 5]. Full precision
            (unlike the JSON tool, which rounds). shape defaults to the current shape.
            Runs in-process — same cost as the find_holes tool on a large solid."""
            from build123d_drafting import find_holes as _recognise_holes

            s = shape if shape is not None else session_ref.current_shape
            if s is None:
                raise ValueError("find_holes(): no shape given and no current shape")
            holes = list(_recognise_holes(s))
            print(f"find_holes: {len(holes)} hole(s)")
            return holes

        self.namespace["find_holes"] = find_holes

        def _resolve(shape: Any, who: str) -> Any:
            s = shape if shape is not None else session_ref.current_shape
            if s is None:
                raise ValueError(f"{who}(): no shape given and no current shape")
            return s

        def find_bosses(shape: Any = None) -> list:
            """Recognise external cylindrical bosses → records (.location (x,y,z) tuple,
            .diameter, .height, .axis). Filter in code. shape defaults to current shape."""
            from build123d_drafting import find_bosses as _recognise

            bosses = list(_recognise(_resolve(shape, "find_bosses")))
            print(f"find_bosses: {len(bosses)} boss(es)")
            return bosses

        self.namespace["find_bosses"] = find_bosses

        def find_bored_bosses(shape: Any = None) -> list:
            """Candidate bored bosses with bore/cap evidence.

            Returns dict records: bore location/axis/diameter/depth, cap face
            indices at the opening, split-cap risk flags, and construction
            advice. Use before extending a square/rounded-square boss with a
            central bore.
            """
            from build123d_mcp.tools.find_features import _find_bored_boss_candidates

            candidates = _find_bored_boss_candidates(_resolve(shape, "find_bored_bosses"))
            print(f"find_bored_bosses: {len(candidates)} candidate(s)")
            return candidates

        self.namespace["find_bored_bosses"] = find_bored_bosses

        def find_countersinks(shape: Any = None) -> list:
            """Recognise conical countersinks → list of dicts (major/drill diameter,
            included angle, depth). Filter in code. shape defaults to current shape."""
            from build123d_mcp.tools.recognizers.countersink import recognise_countersinks

            cs = recognise_countersinks(_resolve(shape, "find_countersinks"))
            print(f"find_countersinks: {len(cs)} countersink(s)")
            return cs

        self.namespace["find_countersinks"] = find_countersinks

        def find_hole_patterns(shape: Any = None) -> list:
            """Recognise bolt-circle / linear-array hole patterns → the recogniser's
            pattern records (BoltCircle: .center/.diameter/.holes; LinearArray:
            .pitch/.direction/.holes). shape defaults to the current shape."""
            from build123d_drafting import find_hole_patterns as _patterns
            from build123d_drafting import find_holes as _holes

            s = _resolve(shape, "find_hole_patterns")
            pats = list(_patterns(_holes(s)))
            print(f"find_hole_patterns: {len(pats)} pattern(s)")
            return pats

        self.namespace["find_hole_patterns"] = find_hole_patterns

        def align_check(a: Any, b: Any, axis: str = "Z", mode: str = "flush") -> dict:
            """Alignment between two shapes along an axis → dict (delta, interpretation).
            mode: 'flush' (bbox-extreme offset), 'center' (centroid offset), 'clearance'
            (nearest-face gap). align_check(a, b)["delta"] is a float you can branch on."""
            from build123d_mcp.tools.align_check import _align_check

            result = _align_check(a, b, axis, mode, "a", "b")
            if "error" in result:
                raise ValueError(result["error"])
            print(f"align_check ({mode}/{axis}): delta={result['delta']}")
            return result

        self.namespace["align_check"] = align_check

    def _shape_summary(self, shape) -> dict | None:
        """Pull volume/bbox/topology from a shape; None if anything errors."""
        try:
            bb = shape.bounding_box()
            return {
                "vol": shape.volume,
                "size_x": bb.size.X,
                "size_y": bb.size.Y,
                "size_z": bb.size.Z,
                "center_x": bb.center().X,
                "center_y": bb.center().Y,
                "center_z": bb.center().Z,
                "faces": len(shape.faces()),
                "edges": len(shape.edges()),
                "verts": len(shape.vertices()),
            }
        except Exception:
            return None

    def _diagnose_change(self, shape, before) -> tuple[str, list[str]]:
        """Format the current_shape diagnostic line, with deltas vs `before`
        when available, plus warnings for two specific silent-failure modes:
        boolean no-op and degenerate (zero-volume) result."""
        after = self._shape_summary(shape)
        if after is None:
            return "", []

        before_summary = self._shape_summary(before) if before is not None else None

        if before_summary is None:
            diag = (
                f"--- current_shape ---\n"
                f"volume: {after['vol']:.4g} mm³  |  "
                f"bbox: {after['size_x']:.4g}×{after['size_y']:.4g}×{after['size_z']:.4g} mm  |  "
                f"{after['faces']}f {after['edges']}e {after['verts']}v"
            )
            return diag, []

        b = before_summary
        a = after
        dvol = a["vol"] - b["vol"]
        dpct = (dvol / b["vol"] * 100) if abs(b["vol"]) > 1e-9 else None

        def fmt_int_delta(d: int) -> str:
            return "" if d == 0 else f" ({d:+d})"

        def fmt_vol_delta(d: float, pct: float | None) -> str:
            if abs(d) < 1e-9:
                return ""
            if pct is None:
                return f" ({d:+.3g})"
            return f" ({d:+.3g}, {pct:+.1f}%)"

        diag = (
            f"--- current_shape ---\n"
            f"volume: {a['vol']:.4g}{fmt_vol_delta(dvol, dpct)} mm³  |  "
            f"bbox: {a['size_x']:.4g}×{a['size_y']:.4g}×{a['size_z']:.4g} mm  |  "
            f"{a['faces']}f{fmt_int_delta(a['faces'] - b['faces'])} "
            f"{a['edges']}e{fmt_int_delta(a['edges'] - b['edges'])} "
            f"{a['verts']}v{fmt_int_delta(a['verts'] - b['verts'])}"
        )

        warnings: list[str] = []

        # Boolean no-op: shape was rebound (different object) but every
        # measurable property is bit-identical. Likely the boolean missed.
        identical = (
            shape is not before
            and a["vol"] == b["vol"]
            and a["faces"] == b["faces"]
            and a["edges"] == b["edges"]
            and a["verts"] == b["verts"]
            and a["size_x"] == b["size_x"]
            and a["size_y"] == b["size_y"]
            and a["size_z"] == b["size_z"]
            and a["center_x"] == b["center_x"]
            and a["center_y"] == b["center_y"]
            and a["center_z"] == b["center_z"]
        )
        if identical:
            warnings.append(
                "Warning: shape was rebound but volume/topology/bbox unchanged "
                "— boolean may have missed (no intersection?)"
            )

        # Degenerate: previously had volume, now ≈ 0. Failed loft/extrude/intersection —
        # unless the shape is live 2D/1D geometry (a sketch, face, or wire), which has
        # faces/edges but no solids; an empty boolean result has none of the three (#236).
        if b["vol"] > 1e-9 and a["vol"] < 1e-9:
            try:
                has_solids = bool(shape.solids())
            except Exception:
                has_solids = True  # can't tell — keep the strong warning
            if not has_solids and (a["faces"] > 0 or a["edges"] > 0):
                warnings.append(
                    f"Note: current shape is a {type(shape).__name__} with no solid "
                    "volume (2D/1D geometry). If this call built a solid, register it "
                    "with show(part, 'name') or assign it to `result`."
                )
            else:
                warnings.append(
                    "Warning: resulting shape has volume ≈ 0 — degenerate "
                    "(failed loft/extrude/intersection?)"
                )

        return diag, warnings

    def execute(self, code: str) -> str:
        # Layer 1: AST check before anything runs
        try:
            check_ast(code)
        except ValueError as e:
            self.last_error_detail = {
                "type": "SecurityError",
                "message": str(e),
                "line": None,
                "excerpt": None,
            }
            return f"Error: SecurityError: {e}"

        try:
            compiled = compile(code, "<mcp>", "exec")
        except SyntaxError as e:
            excerpt = self._syntax_excerpt(code, e.lineno)
            self.last_error_detail = {
                "type": "SyntaxError",
                "message": str(e),
                "line": e.lineno,
                "excerpt": excerpt,
            }
            return f"Error: SyntaxError: {e}"

        values_before = {
            k: v.copy() if isinstance(v, (list, dict, set)) else v
            for k, v in self.namespace.items()
            if k not in _INJECTED
        }
        shape_before = self.current_shape
        objects_before = dict(self.objects)
        annotations_before = dict(self.drawing_annotations)
        self._shape_explicitly_set = False
        # Reference point for the execute() timeout: an in-namespace analysis primitive
        # (#366) bounds any subprocess it spawns to the REMAINING budget from here.
        self._execute_started = time.monotonic()

        buf = io.StringIO()
        exc: Exception | None = None

        # Layer 3: SIGALRM timeout (Unix main-thread only; silently skipped otherwise).
        # Fire 2s before the parent's conn.poll() deadline so the worker always
        # returns a response before the parent kills it.
        _alarm_set = False
        _old_handler: Any = None
        try:

            def _timeout_handler(signum: int, frame: Any) -> None:
                raise ExecutionTimeout(
                    f"Code exceeded the {self.exec_timeout}s execution time limit."
                )

            _old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(max(1, self.exec_timeout - 2))
            _alarm_set = True
        except (OSError, ValueError, AttributeError):
            pass  # Windows (no SIGALRM) or non-main thread; no timeout protection

        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                exec(compiled, self.namespace)  # noqa: S102
            # Cancel alarm immediately so it cannot fire during post-exec shape detection.
            if _alarm_set:
                signal.alarm(0)
        except ExecutionTimeout as e:
            self._rollback_namespace(values_before)
            self.current_shape = shape_before
            self.objects.clear()
            self.objects.update(objects_before)
            self.drawing_annotations.clear()
            self.drawing_annotations.update(annotations_before)
            self.last_error_detail = {
                "type": "ExecutionTimeout",
                "message": str(e),
                "line": None,
                "excerpt": None,
            }
            return f"Error: ExecutionTimeout: {e}"
        except AssertionError as e:
            self._rollback_namespace(values_before)
            self.current_shape = shape_before
            self.objects.clear()
            self.objects.update(objects_before)
            self.drawing_annotations.clear()
            self.drawing_annotations.update(annotations_before)
            msg = str(e) or "Constraint failed"
            self.last_error_detail = {
                "type": "AssertionError",
                "message": msg,
                "line": None,
                "excerpt": None,
            }
            return f"Constraint failed: {e}" if str(e) else "Constraint failed"
        except Exception as e:
            exc = e
        finally:
            if _alarm_set:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, _old_handler)

        if exc is not None:
            # Preserve namespace, objects, and current_shape — partial execution results
            # (variables defined before the error, show() calls that succeeded) are kept
            # so iterative workflows can continue without losing context.
            self.last_error_detail = self._make_error_detail(exc, code)
            return f"Error: {type(exc).__name__}: {exc}"

        self.last_error_detail = None
        self.execute_history.append(code)
        new_keys = {k for k in self.namespace if k not in _INJECTED} - values_before.keys()
        self._update_current_shape(new_keys)

        output = buf.getvalue() or "OK"
        if self.current_shape is not None and self.current_shape is not shape_before:
            diag, warnings = self._diagnose_change(self.current_shape, shape_before)
            if diag:
                shape_name = next(
                    (k for k, v in self.objects.items() if v is self.current_shape), None
                )
                if shape_name:
                    diag = diag.replace(
                        "--- current_shape ---",
                        f'--- current_shape ("{shape_name}") ---',
                    )
                output = output.rstrip("\n") + "\n" + diag
            for w in warnings:
                output += "\n" + w

        # Append a summary of new/changed scalar variables so the caller can verify
        # that assignments took effect. This makes each execution's output distinct
        # even when the code structure is similar, preventing stale context references
        # from being silently mistaken for a successful re-run with updated values.
        var_summary = self._summarise_var_changes(values_before)
        if var_summary:
            output = output.rstrip("\n") + "\n# vars: " + var_summary

        return output

    def objects_types(self) -> dict:
        """Return {name: description} for each named object in the session."""
        result = {}
        for name, shape in self.objects.items():
            try:
                nf = len(shape.faces())
                result[name] = (
                    f"{type(shape).__name__}, {nf} faces" if nf > 0 else type(shape).__name__
                )
            except Exception:
                result[name] = type(shape).__name__
        return result

    def _rollback_namespace(self, values_before: dict[str, Any]) -> None:
        # Delete keys that didn't exist before; restore values for keys that were overwritten.
        current = {k for k in self.namespace if k not in _INJECTED}
        for k in current - values_before.keys():
            del self.namespace[k]
        for k, v in values_before.items():
            self.namespace[k] = v

    def _syntax_excerpt(self, code: str, lineno: int | None) -> str | None:
        if lineno is None:
            return None
        lines = code.splitlines()
        start = max(0, lineno - 3)
        end = min(len(lines), lineno + 2)
        return "\n".join(
            f"{i + 1:3d}{'→ ' if i + 1 == lineno else '  '}{lines[i]}" for i in range(start, end)
        )

    def _make_error_detail(self, exc: Exception, code: str) -> dict[str, Any]:
        import traceback as tb_module

        frames = tb_module.extract_tb(exc.__traceback__)
        mcp_frames = [f for f in frames if f.filename == "<mcp>"]
        lineno: int | None = mcp_frames[-1].lineno if mcp_frames else None
        excerpt = self._syntax_excerpt(code, lineno)
        return {"type": type(exc).__name__, "message": str(exc), "line": lineno, "excerpt": excerpt}

    def _update_current_shape(self, new_keys: set[str]) -> None:
        # show()/annotate()/register_centerline() set current_shape explicitly,
        # and that registration must win: the new-variable scan below iterates
        # an unordered set and can land on an incidental leftover (e.g. the
        # sketch a solid was revolved from), making the degenerate-shape
        # warning fire on the wrong object (#236).
        if self._shape_explicitly_set:
            return

        try:
            from build123d import BuildPart, Shape
        except ImportError:
            return

        ns = self.namespace

        # Always prefer explicit 'result', even if it pre-existed
        if "result" in ns and isinstance(ns["result"], Shape):
            self.current_shape = ns["result"]
            return

        # Scan newly created variables for BuildPart or Shape. Sorted so that
        # which variable wins is deterministic (set order is hash-seeded).
        for key in sorted(new_keys):
            if key.startswith("_"):
                continue
            obj = ns.get(key)
            if isinstance(obj, BuildPart):
                try:
                    self.current_shape = obj.part
                    return
                except Exception:
                    pass
            elif isinstance(obj, Shape):
                self.current_shape = obj
                return

    @staticmethod
    def _copy_shape(shape: Any) -> Any:
        if shape is None:
            return None
        try:
            return copy.copy(shape)
        except Exception:
            return shape

    def save_snapshot(self, name: str) -> None:
        self.snapshots[name] = {
            "current_shape": self._copy_shape(self.current_shape),
            "objects": {k: self._copy_shape(v) for k, v in self.objects.items()},
        }

    def restore_snapshot(self, name: str) -> None:
        if name not in self.snapshots:
            raise KeyError(f"No snapshot named '{name}'. Available: {list(self.snapshots.keys())}")
        snap = self.snapshots[name]
        self.current_shape = snap["current_shape"]
        self.objects.clear()
        self.objects.update(snap["objects"])

    def _summarise_var_changes(self, before: dict) -> str:
        """Return a compact summary of scalar variables added or changed since *before*.

        Only covers simple value types (bool, int, float, complex, str, tuple).
        Shapes, functions, modules, and private names are excluded — shapes are
        already reported by the shape diagnostic.
        """
        _SCALAR = (bool, int, float, complex, str, bytes)
        _sentinel = object()
        changed = []
        for k in sorted(self.namespace):
            if k in _INJECTED or k.startswith("_"):
                continue
            v = self.namespace[k]
            if isinstance(v, tuple):
                if not all(isinstance(e, _SCALAR) for e in v):
                    continue
            elif not isinstance(v, _SCALAR):
                continue
            old = before.get(k, _sentinel)
            try:
                same = old is not _sentinel and old == v
            except Exception:
                same = False
            if not same:
                r = repr(v)
                if len(r) > 60:
                    r = r[:57] + "..."
                changed.append(f"{k}={r}")
        return ", ".join(changed)

    def reset(self) -> None:
        self.namespace.clear()
        self.current_shape = None
        self.objects.clear()
        self.snapshots.clear()
        self.drawing_annotations.clear()
        self.drawing_page = None
        self.last_error_detail = None
        self.geometry_refs.clear()
        self.execute_history = []
        self._viewer_baseline.clear()
        self._inject_builtins()
