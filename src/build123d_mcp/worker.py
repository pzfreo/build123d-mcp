"""Persistent worker subprocess and parent-side proxy for build123d-mcp sessions.

Architecture:
  WorkerSession (parent)  ←── multiprocessing.Pipe ──→  worker_main (child)
                                                             └─ Session + tools

The worker process owns the Session and calls all tool functions directly —
no forking, no namespace serialization per call. OCC/TBB threads are confined
to the worker; the parent process never touches OCC at all.

Timeout is managed at the WorkerSession level: if conn.poll() expires the
parent kills the worker with SIGKILL and restarts it (fresh session).
Within the worker, Session.execute() also applies a SIGALRM guard so that
a hanging execute() call returns an error rather than blocking indefinitely.
"""

import multiprocessing
from typing import Any

_WORKER_READY_TIMEOUT = 60  # seconds to wait for worker import + ready signal


def worker_main(
    conn: Any,
    library_path: str = "",
    exec_timeout: int = 120,
    allow_all_imports: bool = False,
    extra_allowed_imports: tuple[str, ...] = (),
) -> None:
    """Entry point run in the worker subprocess.

    Loops receiving requests until the parent closes the connection.
    """
    if allow_all_imports or extra_allowed_imports:
        import build123d_mcp.security as _sec
        if allow_all_imports:
            _sec.ALLOW_ALL_IMPORTS = True
        if extra_allowed_imports:
            _sec.EXTRA_ALLOWED_IMPORTS.update(extra_allowed_imports)

    from build123d_mcp.session import Session

    session = Session(exec_timeout=exec_timeout)
    library_index = None
    if library_path:
        from build123d_mcp.tools.library import _LibraryIndex
        library_index = _LibraryIndex(library_path)

    conn.send({"ready": True})

    while True:
        try:
            request = conn.recv()
        except EOFError:
            break

        op = request["op"]
        args = request.get("args", {})

        try:
            result = _dispatch(session, op, args, library_index)
            conn.send({"ok": True, "result": result})
        except Exception as exc:
            conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _dispatch(session: Any, op: str, args: dict, library_index: Any) -> Any:
    if op == "execute":
        return session.execute(args["code"])

    if op == "render_view":
        from build123d_mcp.tools.render import render_view
        return render_view(session, **args)

    if op == "export_file":
        from build123d_mcp.tools.export import export_file
        return export_file(session, **args)

    if op == "interference":
        from build123d_mcp.tools.interference import interference
        return interference(session, **args)

    if op == "measure":
        from build123d_mcp.tools.measure import measure
        return measure(session, **args)

    if op == "clearance":
        from build123d_mcp.tools.measure import clearance
        return clearance(session, **args)

    if op == "cross_sections":
        from build123d_mcp.tools.cross_sections import cross_sections
        return cross_sections(session, **args)

    if op == "save_snapshot":
        name = args["name"]
        session.save_snapshot(name)
        saved = (["current_shape"] if session.current_shape is not None else []) + list(session.snapshots[name]["objects"].keys())
        return f"Snapshot '{name}' saved. Geometry captured: {', '.join(saved) if saved else 'none'}."

    if op == "restore_snapshot":
        name = args["name"]
        try:
            session.restore_snapshot(name)
        except KeyError as e:
            return f"Error: {e}"
        restored = (["current_shape"] if session.current_shape is not None else []) + list(session.objects.keys())
        return f"Snapshot '{name}' restored. Active geometry: {', '.join(restored) if restored else 'none'}."

    if op == "reset":
        session.reset()
        return "Session reset."

    if op == "search_library":
        if library_index is None:
            return "No part library configured."
        from build123d_mcp.tools.library import search_library
        return search_library(library_index, args.get("query", ""))

    if op == "load_part":
        if library_index is None:
            return "No part library configured."
        from build123d_mcp.tools.library import load_part
        return load_part(session, library_index, args["name"], args.get("params", ""))

    if op == "diff_snapshot":
        from build123d_mcp.tools.diff import diff_snapshot
        return diff_snapshot(session, args["snapshot_a"], args.get("snapshot_b", ""), args.get("format", "text"))

    if op == "session_state":
        from build123d_mcp.tools.session_state import session_state
        return session_state(session)

    if op == "health_check":
        from build123d_mcp.tools.health_check import health_check
        return health_check(session)

    if op == "last_error":
        from build123d_mcp.tools.last_error import last_error
        return last_error(session)

    if op == "shape_compare":
        from build123d_mcp.tools.shape_compare import shape_compare
        return shape_compare(session, args["object_a"], args["object_b"])

    if op == "import_cad_file":
        from build123d_mcp.tools.import_step import import_cad_file
        return import_cad_file(session, args["path"], args.get("name", ""))

    if op == "inspect_drawing":
        from build123d_mcp.tools.inspect_drawing import inspect_drawing
        return inspect_drawing(session, args.get("objects", ""), args.get("svg_path", ""))

    if op == "view_axes":
        from build123d_mcp.tools.view_axes import view_axes
        return view_axes(
            tuple(args["viewport_origin"]),
            tuple(args.get("viewport_up", (0.0, 1.0, 0.0))),
            tuple(args.get("look_at", (0.0, 0.0, 0.0))),
        )

    if op == "lint_drawing":
        from build123d_mcp.tools.lint_drawing import lint_drawing
        return lint_drawing(session, args.get("svg_path", ""),
                            args.get("drawing_scale", 1.0),
                            args.get("view_shape_names"))

    if op == "render_drawing":
        from build123d_mcp.tools.render_drawing import render_drawing
        return render_drawing(
            args["svg_path"],
            args.get("width", 0),
            args.get("save_to", ""),
        )

    if op == "save_drawing_annotations":
        from build123d_mcp.tools.save_drawing_annotations import save_drawing_annotations
        return save_drawing_annotations(session, args["svg_path"])

    if op == "align_check":
        from build123d_mcp.tools.align_check import align_check
        return align_check(session, args["object_a"], args["object_b"],
                           axis=args.get("axis", "Z"), mode=args.get("mode", "flush"))

    if op == "resolve":
        from build123d_mcp.tools.resolve import resolve
        return resolve(session, args["object_name"], args["selector"], label=args.get("label", ""))

    if op == "suggest_view_layout":
        from build123d_mcp.tools.suggest_view_layout import suggest_view_layout
        return suggest_view_layout(
            session, args["object_name"], args.get("page_w", 297.0),
            args.get("page_h", 210.0), args.get("scale", 1.0), args.get("views"),
            args.get("title_block_w", 150.0), args.get("title_block_h", 30.0),
            args.get("margin", 10.0),
        )

    if op == "script":
        from build123d_mcp.tools.script import script
        return script(session, save_to=args.get("save_to", ""))

    raise ValueError(f"Unknown operation: '{op}'")


class WorkerSession:
    """Parent-side proxy to the persistent worker subprocess.

    Exposes the same interface as Session so server.py can use either.
    """

    _RENDER_TIMEOUT = 120
    _EXPORT_TIMEOUT = 60
    _INTERFERENCE_TIMEOUT = 30
    _SHORT_TIMEOUT = 10

    def __init__(
        self,
        exec_timeout: int = 120,
        library_path: str = "",
        allow_all_imports: bool = False,
        extra_allowed_imports: tuple[str, ...] = (),
    ) -> None:
        self._exec_timeout = exec_timeout
        self._library_path = library_path
        self._allow_all_imports = allow_all_imports
        self._extra_allowed_imports = tuple(extra_allowed_imports)
        self._conn: Any = None
        self._proc: Any = None
        self._start_worker()

    def _start_worker(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe()
        self._proc = ctx.Process(
            target=worker_main,
            args=(
                child_conn,
                self._library_path,
                self._exec_timeout,
                self._allow_all_imports,
                self._extra_allowed_imports,
            ),
            daemon=True,
        )
        self._proc.start()
        child_conn.close()
        self._conn = parent_conn

        if not self._conn.poll(_WORKER_READY_TIMEOUT):
            self._proc.kill()
            self._proc.join(5)
            raise RuntimeError("Worker process failed to start within timeout.")
        self._conn.recv()  # consume the ready signal

    def _kill_worker(self) -> None:
        try:
            self._proc.kill()
            self._proc.join(5)
        except Exception:
            pass

    def _call(self, op: str, args: dict, timeout: int) -> Any:
        if not self._proc.is_alive():
            self._start_worker()
            raise RuntimeError("Worker crashed; session restarted. Re-run your setup code.")

        self._conn.send({"op": op, "args": args})

        if not self._conn.poll(timeout):
            self._kill_worker()
            self._start_worker()
            from build123d_mcp.security import ExecutionTimeout
            if op == "execute":
                raise ExecutionTimeout(
                    f"Code exceeded the {timeout}s execution time limit. "
                    f"All session state (variables, shapes, named objects) has been lost — "
                    f"the worker was restarted.\n"
                    f"For complex builds with many booleans (IsoThread, multi-body fillets, "
                    f"high-face-count solids): write the build as a plain Python script and "
                    f"run it with Bash, then load the result with import_cad_file() and use "
                    f"render_view() / measure() here. "
                    f"The timeout limit can be raised with the --exec-timeout flag or "
                    f"BUILD123D_EXEC_TIMEOUT env var."
                )
            raise RuntimeError(f"Operation '{op}' timed out after {timeout}s.")

        try:
            response = self._conn.recv()
        except EOFError:
            self._start_worker()
            raise RuntimeError("Worker process crashed unexpectedly; session restarted.")

        if response["ok"]:
            return response["result"]
        raise RuntimeError(response["error"])

    # --- Session-compatible interface ---

    def execute(self, code: str) -> str:
        from build123d_mcp.security import ExecutionTimeout
        try:
            return self._call("execute", {"code": code}, self._exec_timeout)
        except (RuntimeError, ExecutionTimeout) as e:
            return f"Error: {e}"

    def render_view(
        self,
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
        return self._call(
            "render_view",
            {
                "direction": direction,
                "objects": objects,
                "quality": quality,
                "clip_plane": clip_plane,
                "clip_at": clip_at,
                "azimuth": azimuth,
                "elevation": elevation,
                "save_to": save_to,
                "format": format,
                "label_objects": label_objects,
                "highlights": highlights,
                "colors": colors,
                "mode": mode,
            },
            self._RENDER_TIMEOUT,
        )

    def export_file(self, filename: str, format: str = "step", object_name: str = "") -> str:
        return self._call(
            "export_file",
            {"filename": filename, "format": format, "object_name": object_name},
            self._EXPORT_TIMEOUT,
        )

    def interference(self, object_a: str, object_b: str) -> str:
        return self._call(
            "interference",
            {"object_a": object_a, "object_b": object_b},
            self._INTERFERENCE_TIMEOUT,
        )

    def measure(self, object_name: str = "") -> str:
        return self._call("measure", {"object_name": object_name}, self._SHORT_TIMEOUT)

    def clearance(self, object_a: str, object_b: str) -> str:
        return self._call("clearance", {"object_a": object_a, "object_b": object_b}, self._SHORT_TIMEOUT)

    def cross_sections(self, object_name: str = "", axis: str = "Z", num_slices: int = 10) -> str:
        return self._call(
            "cross_sections",
            {"object_name": object_name, "axis": axis, "num_slices": num_slices},
            self._SHORT_TIMEOUT,
        )

    def save_snapshot(self, name: str) -> str:
        return self._call("save_snapshot", {"name": name}, self._SHORT_TIMEOUT)

    def restore_snapshot(self, name: str) -> str:
        return self._call("restore_snapshot", {"name": name}, self._SHORT_TIMEOUT)

    def reset(self) -> str:
        if not self._proc.is_alive():
            self._start_worker()
            return "Session reset."
        return self._call("reset", {}, self._SHORT_TIMEOUT)

    def diff_snapshot(self, snapshot_a: str, snapshot_b: str = "", format: str = "text") -> str:
        return self._call("diff_snapshot", {"snapshot_a": snapshot_a, "snapshot_b": snapshot_b, "format": format}, self._SHORT_TIMEOUT)

    def session_state(self) -> str:
        return self._call("session_state", {}, self._SHORT_TIMEOUT)

    def health_check(self) -> str:
        return self._call("health_check", {}, self._RENDER_TIMEOUT)

    def last_error(self) -> str:
        return self._call("last_error", {}, self._SHORT_TIMEOUT)

    def shape_compare(self, object_a: str, object_b: str) -> str:
        return self._call("shape_compare", {"object_a": object_a, "object_b": object_b}, self._SHORT_TIMEOUT)

    def import_cad_file(self, path: str, name: str = "") -> str:
        return self._call("import_cad_file", {"path": path, "name": name}, self._EXPORT_TIMEOUT)

    def inspect_drawing(self, objects: str = "", svg_path: str = "") -> str:
        return self._call(
            "inspect_drawing",
            {"objects": objects, "svg_path": svg_path},
            self._SHORT_TIMEOUT,
        )

    def view_axes(
        self,
        viewport_origin: tuple,
        viewport_up: tuple = (0.0, 1.0, 0.0),
        look_at: tuple = (0.0, 0.0, 0.0),
    ) -> str:
        return self._call(
            "view_axes",
            {"viewport_origin": viewport_origin, "viewport_up": viewport_up, "look_at": look_at},
            self._SHORT_TIMEOUT,
        )

    def lint_drawing(
        self, svg_path: str = "", drawing_scale: float = 1.0,
        view_shape_names: list[str] | None = None,
    ) -> str:
        return self._call(
            "lint_drawing",
            {"svg_path": svg_path, "drawing_scale": drawing_scale,
             "view_shape_names": view_shape_names},
            self._SHORT_TIMEOUT,
        )

    def render_drawing(self, svg_path: str, width: int = 0, save_to: str = "") -> dict:
        return self._call(
            "render_drawing",
            {"svg_path": svg_path, "width": width, "save_to": save_to},
            self._RENDER_TIMEOUT,
        )

    def save_drawing_annotations(self, svg_path: str) -> str:
        return self._call(
            "save_drawing_annotations",
            {"svg_path": svg_path},
            self._SHORT_TIMEOUT,
        )

    def align_check(self, object_a: str, object_b: str, axis: str = "Z", mode: str = "flush") -> str:
        return self._call(
            "align_check",
            {"object_a": object_a, "object_b": object_b, "axis": axis, "mode": mode},
            self._SHORT_TIMEOUT,
        )

    def resolve(self, object_name: str, selector: str, label: str = "") -> str:
        return self._call(
            "resolve",
            {"object_name": object_name, "selector": selector, "label": label},
            self._SHORT_TIMEOUT,
        )

    def suggest_view_layout(
        self,
        object_name: str,
        page_w: float = 297.0,
        page_h: float = 210.0,
        scale: float = 1.0,
        views: list[str] | None = None,
        title_block_w: float = 150.0,
        title_block_h: float = 30.0,
        margin: float = 10.0,
    ) -> str:
        return self._call(
            "suggest_view_layout",
            {"object_name": object_name, "page_w": page_w, "page_h": page_h,
             "scale": scale, "views": views, "title_block_w": title_block_w,
             "title_block_h": title_block_h, "margin": margin},
            self._SHORT_TIMEOUT,
        )

    def script(self, save_to: str = "") -> str:
        return self._call("script", {"save_to": save_to}, self._SHORT_TIMEOUT)

    def search_library(self, query: str = "") -> str:
        return self._call("search_library", {"query": query}, self._SHORT_TIMEOUT)

    def load_part(self, name: str, params: str = "") -> str:
        return self._call("load_part", {"name": name, "params": params}, self._SHORT_TIMEOUT)
