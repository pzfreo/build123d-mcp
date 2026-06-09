import copy
import io
import signal
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
    {"__builtins__", "show", "named_face", "annotate", "register_centerline", "set_page"}
)


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
            print(f"Annotated '{name}': {meta.get('label_str', '')}")

        session_ref.namespace["annotate"] = annotate

        def show(shape: Any, name: str | None = None) -> None:
            if name is None:
                name = "shape"
            objects[name] = shape
            session_ref.current_shape = shape
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

        # Degenerate: previously had volume, now ≈ 0. Failed loft/extrude/intersection.
        if b["vol"] > 1e-9 and a["vol"] < 1e-9:
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
        excerpt: str | None = None
        if lineno is not None:
            lines = code.splitlines()
            start = max(0, lineno - 3)
            end = min(len(lines), lineno + 2)
            excerpt = "\n".join(
                f"{i + 1:3d}{'→ ' if i + 1 == lineno else '  '}{lines[i]}"
                for i in range(start, end)
            )
        return {"type": type(exc).__name__, "message": str(exc), "line": lineno, "excerpt": excerpt}

    def _update_current_shape(self, new_keys: set[str]) -> None:
        try:
            from build123d import BuildPart, Shape
        except ImportError:
            return

        ns = self.namespace

        # Always prefer explicit 'result', even if it pre-existed
        if "result" in ns and isinstance(ns["result"], Shape):
            self.current_shape = ns["result"]
            return

        # Scan newly created variables for BuildPart or Shape
        for key in new_keys:
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
        self._inject_builtins()
