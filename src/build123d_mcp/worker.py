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

InProcessSession (end of this module) is the no-subprocess fallback for MCP
hosts that block child-process creation (#143). It trades away the isolation
above: the Session and OCC run inside the server process, with no crash
containment and no operation timeouts.
"""

import functools
import inspect
import multiprocessing
import threading
from collections.abc import Callable
from typing import Any, NamedTuple, TypeVar, cast

from build123d_mcp.tools._budget import OP_BUDGET_FLOOR_S

_WORKER_READY_TIMEOUT = 60  # seconds to wait for worker import + ready signal


def _build_session(
    library_path: str,
    exec_timeout: int,
    allow_all_imports: bool,
    extra_allowed_imports: tuple[str, ...],
    no_sandbox: bool = False,
) -> tuple[Any, Any]:
    """Apply security overrides and build the Session (+ optional library index).

    Shared by worker_main (subprocess mode) and InProcessSession so a future
    setup step cannot be added to one mode and forgotten in the other.
    """
    if allow_all_imports or extra_allowed_imports or no_sandbox:
        import build123d_mcp.security as _sec

        if no_sandbox:
            _sec.DISABLE_SANDBOX = True
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
    return session, library_index


def worker_main(
    conn: Any,
    library_path: str = "",
    exec_timeout: int = 120,
    allow_all_imports: bool = False,
    extra_allowed_imports: tuple[str, ...] = (),
    memory_limit_mb: int | None = None,
    cpu_limit_s: int | None = None,
    no_sandbox: bool = False,
) -> None:
    """Entry point run in the worker subprocess.

    Loops receiving requests until the parent closes the connection.
    """
    if memory_limit_mb is not None or cpu_limit_s is not None:
        try:
            import resource

            if memory_limit_mb is not None and memory_limit_mb > 0:
                import sys as _sys

                if _sys.platform == "darwin":
                    # macOS accepts setrlimit(RLIMIT_DATA) without error but silently
                    # ignores it — the kernel never enforces it.  Skip the call and
                    # send a warning so the parent can surface it to the operator.
                    conn.send(
                        {
                            "warning": (
                                "--memory-limit-mb has no effect on macOS: "
                                "RLIMIT_DATA is a documented no-op on this platform. "
                                "Use container/VM memory limits instead."
                            )
                        }
                    )
                else:
                    # RLIMIT_DATA caps the heap/BSS data segment.  Safe to set after
                    # shared libraries are loaded (VAS mappings don't count against it).
                    # Note: large mmap() allocations (>128 KB glibc default) are not
                    # covered; use container cgroup limits for comprehensive control.
                    limit = memory_limit_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
            if cpu_limit_s is not None and cpu_limit_s > 0:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_s, cpu_limit_s))
        except (ImportError, AttributeError):
            pass  # Windows has no resource module
        except (OSError, ValueError) as exc:
            # OSError(EPERM): soft limit exceeds the container/system hard limit.
            # ValueError: limit value is out of range.
            # Send the error over the pipe so the parent receives it before the
            # worker exits — avoids the parent seeing a bare EOFError from recv().
            conn.send({"error": f"Failed to apply resource limit: {exc}"})
            return

    session, library_index = _build_session(
        library_path, exec_timeout, allow_all_imports, extra_allowed_imports, no_sandbox
    )

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


# --------------------------------------------------------------------------- #
# Op table — single source of truth for worker-routed operations (issue #220). #
# --------------------------------------------------------------------------- #

# Parent-side time budgets (seconds). Geometry-heavy queries (boolean ops,
# per-face walks, BREP analysis, part construction) can legitimately exceed
# 10s on complex parts, and a timeout here kills the worker and destroys all
# session state — so the budget errs generous (issue #214). _SHORT_TIMEOUT is
# only for ops that read session bookkeeping without touching geometry kernels.
_RENDER_TIMEOUT = 120
# The export/geometry floor is shared with the tool side (tools/_budget.py) so a
# worker-run tool's self-imposed budget provably tracks this parent op budget.
_EXPORT_TIMEOUT = OP_BUDGET_FLOOR_S
_GEOMETRY_TIMEOUT = 60
_SHORT_TIMEOUT = 10


def _exec_budget(ws: "WorkerSession") -> int:
    return ws._exec_timeout


def _import_cad_budget(ws: "WorkerSession") -> int:
    # STEP import of heavy geometry (threads, gears — lots of BSpline faces)
    # can outlast _EXPORT_TIMEOUT, and a timeout kills the whole session.
    # Honour the user's exec-timeout knob (--exec-timeout /
    # BUILD123D_EXEC_TIMEOUT) when it is the larger budget (#229).
    return max(_EXPORT_TIMEOUT, ws._exec_timeout)


def _load_part_budget(ws: "WorkerSession") -> int:
    # Library part scripts can build heavy geometry (threads, gears) just
    # like a STEP import, so honour the exec-timeout knob here too (#229).
    return max(_GEOMETRY_TIMEOUT, ws._exec_timeout)


def _export_budget(ws: "WorkerSession") -> int:
    # export() re-imports the written STEP and runs the authoritative validity
    # gate (the open-edge deflection ladder on a large part can take tens of
    # seconds — it is internally time-bounded, but give the op real headroom so
    # the parent never kills the worker mid-gate). Honour --exec-timeout when
    # larger, like the import/load budgets.
    return max(_EXPORT_TIMEOUT, ws._exec_timeout)


class _OpSpec(NamedTuple):
    """One worker-routed operation.

    handler: (session, args, library_index) -> result, run inside the worker.
    timeout: parent-side wait budget in seconds, or a callable on the
        WorkerSession for budgets derived from the exec-timeout knob.
    params: positional parameter order of the WorkerSession proxy method,
        derived from the stub signature by @_op — the wire-interface
        documentation. Optionals a caller omits fall through to the tool
        function's own defaults.
    """

    handler: "Callable[[Any, dict, Any], Any]"
    timeout: "int | Callable[[WorkerSession], int]"
    params: tuple[str, ...]


def _tool(path: str) -> "Callable[[Any, dict, Any], Any]":
    """Handler for the common case: lazily import ``module:function`` and call
    ``fn(session, **args)``. Wire arg names must equal the function's parameter
    names, with defaults living on the function itself."""
    module_name, _, func_name = path.partition(":")

    def handler(session: Any, args: dict, library_index: Any) -> Any:
        import importlib

        fn = getattr(importlib.import_module(module_name), func_name)
        return fn(session, **args)

    # Lets tests pin stub defaults to the tool function's (the operative ones).
    handler.__tool_path__ = path  # type: ignore[attr-defined]
    return handler


# --- Ops with logic beyond fn(session, **args) ---


def _op_execute(session: Any, args: dict, library_index: Any) -> Any:
    return session.execute(args["code"])


def _op_objects_types(session: Any, args: dict, library_index: Any) -> Any:
    return session.objects_types()


def _op_save_snapshot(session: Any, args: dict, library_index: Any) -> Any:
    name = args["name"]
    session.save_snapshot(name)
    saved = (["current_shape"] if session.current_shape is not None else []) + list(
        session.snapshots[name]["objects"].keys()
    )
    return f"Snapshot '{name}' saved. Geometry captured: {', '.join(saved) if saved else 'none'}."


def _op_restore_snapshot(session: Any, args: dict, library_index: Any) -> Any:
    name = args["name"]
    try:
        session.restore_snapshot(name)
    except KeyError as e:
        return f"Error: {e}"
    restored = (["current_shape"] if session.current_shape is not None else []) + list(
        session.objects.keys()
    )
    return f"Snapshot '{name}' restored. Active geometry: {', '.join(restored) if restored else 'none'}."


def _op_reset(session: Any, args: dict, library_index: Any) -> Any:
    session.reset()
    return "Session reset."


def _op_search_library(session: Any, args: dict, library_index: Any) -> Any:
    if library_index is None:
        return "No part library configured."
    from build123d_mcp.tools.library import search_library

    return search_library(library_index, args.get("query", ""))


def _op_load_part(session: Any, args: dict, library_index: Any) -> Any:
    if library_index is None:
        return "No part library configured."
    from build123d_mcp.tools.library import load_part

    return load_part(session, library_index, args["name"], args.get("params", ""))


def _op_view_axes(session: Any, args: dict, library_index: Any) -> Any:
    # Pure helper: takes no session; sequence args normalised to tuples.
    from build123d_mcp.tools.view_axes import view_axes

    return view_axes(
        tuple(args["viewport_origin"]),
        tuple(args.get("viewport_up", (0.0, 1.0, 0.0))),
        tuple(args.get("look_at", (0.0, 0.0, 0.0))),
    )


def _op_render_drawing(session: Any, args: dict, library_index: Any) -> Any:
    # Pure helper: rasterises an SVG file from disk; takes no session.
    from build123d_mcp.tools.render_drawing import render_drawing

    return render_drawing(args["svg_path"], args.get("width", 0), args.get("save_to", ""))


def _op_pull_viewer_deltas(session: Any, args: dict, library_index: Any) -> Any:
    """Return per-shape mesh deltas since the last pull, for the live viewer.

    Diffs ``session.objects`` by identity against a worker-held baseline and
    tessellates only the changed shapes (via the same bounded, hard-killable
    out-of-process path render_view uses), returning raw vertex/triangle arrays
    the server encodes to glb. Called by server.py after each geometry-mutating
    tool, only while a viewer is attached.
    """
    baseline = session._viewer_baseline
    objects = session.objects
    upsert_names = [name for name, shape in objects.items() if baseline.get(name) != id(shape)]
    remove = [name for name in baseline if name not in objects]

    meshes: dict = {}
    if upsert_names:
        from build123d_mcp.tools.render import _QUALITY, _tessellate_shapes_bounded

        shapes = [(name, objects[name], None) for name in upsert_names]
        meshes, _failed = _tessellate_shapes_bounded(shapes, _QUALITY["standard"])

    # Advance the baseline only for shapes we actually tessellated (drop removed
    # ones). A shape that failed tessellation is left dirty, so the next pull
    # retries it instead of skipping it forever.
    new_baseline = {name: ident for name, ident in baseline.items() if name in objects}
    for name in upsert_names:
        if name in meshes:
            new_baseline[name] = id(objects[name])
        else:
            new_baseline.pop(name, None)
    session._viewer_baseline = new_baseline
    return {"upsert": meshes, "remove": remove}


_T = "build123d_mcp.tools"

# Populated by the @_op decorator on WorkerSession's typed stub methods, plus
# the two ops whose proxies need parent-side logic and are written by hand.
_OPS: dict[str, _OpSpec] = {
    "execute": _OpSpec(_op_execute, _exec_budget, ("code",)),
    "reset": _OpSpec(_op_reset, _SHORT_TIMEOUT, ()),
}

_F = TypeVar("_F", bound=Callable[..., Any])


def _op(
    handler: "Callable[[Any, dict, Any], Any]",
    timeout: "int | Callable[[WorkerSession], int]",
) -> "Callable[[_F], _F]":
    """Register a typed WorkerSession stub method as a worker-routed op.

    Records the op in ``_OPS`` (handler + timeout + parameter order from the
    signature) and replaces the body — which never runs — with bind-and-send:
    supplied arguments are mapped through the real signature and shipped to
    the worker; omitted optionals are not sent, falling through to the tool
    function's own defaults. The ``Callable[[_F], _F]`` typing keeps the
    declared signature visible to mypy and IDEs at every call site.
    """

    def deco(fn: _F) -> _F:
        name = fn.__name__
        sig = inspect.signature(fn)
        _OPS[name] = _OpSpec(handler, timeout, tuple(sig.parameters)[1:])

        @functools.wraps(fn)
        def proxy(self: "WorkerSession", *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(self, *args, **kwargs)
            payload = {k: v for k, v in bound.arguments.items() if k != "self"}
            t = timeout(self) if callable(timeout) else timeout
            return self._call(name, payload, t)

        return cast(_F, proxy)

    return deco


def _dispatch(session: Any, op: str, args: dict, library_index: Any) -> Any:
    spec = _OPS.get(op)
    if spec is None:
        raise ValueError(f"Unknown operation: '{op}'")
    return spec.handler(session, args, library_index)


class WorkerSession:
    """Parent-side proxy to the persistent worker subprocess.

    Exposes the same interface as Session so server.py can use either.
    """

    def __init__(
        self,
        exec_timeout: int = 120,
        library_path: str = "",
        allow_all_imports: bool = False,
        extra_allowed_imports: tuple[str, ...] = (),
        memory_limit_mb: int | None = None,
        cpu_limit_s: int | None = None,
        no_sandbox: bool = False,
    ) -> None:
        self._exec_timeout = exec_timeout
        self._library_path = library_path
        self._allow_all_imports = allow_all_imports
        self._extra_allowed_imports = tuple(extra_allowed_imports)
        self._memory_limit_mb = memory_limit_mb
        self._cpu_limit_s = cpu_limit_s
        self._no_sandbox = no_sandbox
        self._conn: Any = None
        self._proc: Any = None
        # Serialises the request/reply critical section (send -> poll -> recv)
        # over the single worker Pipe. Under HTTP transport one WorkerSession is
        # shared across concurrent requests (FastMCP runs sync tools off the
        # event loop); without this lock two threads can interleave on the pipe
        # and one can recv() the other's response. A single OCC worker is serial
        # anyway, so serialising costs no real throughput. (#322)
        self._lock = threading.Lock()
        # Optional callback fired after the worker is (re)started following a
        # crash/timeout restart, used by the live viewer to emit a RESET so
        # clients clear their now-stale scene. Not fired on the first start.
        self._on_restart: Callable[[], None] | None = None
        self._started_once = False
        self._start_worker()

    @property
    def has_library(self) -> bool:
        """Whether a part library was configured (drives search_library/load_part)."""
        return bool(self._library_path)

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
                self._memory_limit_mb,
                self._cpu_limit_s,
                self._no_sandbox,
            ),
            daemon=True,
        )
        self._proc.start()
        child_conn.close()
        self._conn = parent_conn

        if not self._conn.poll(_WORKER_READY_TIMEOUT):
            exitcode = self._proc.exitcode  # read before kill: None means still running
            self._proc.kill()
            self._proc.join(5)
            detail = (
                f"the worker exited with code {exitcode} before signalling ready"
                if exitcode is not None
                else f"the worker did not signal ready within {_WORKER_READY_TIMEOUT}s"
            )
            raise RuntimeError(
                f"Worker process failed to start: {detail}. If your MCP host blocks "
                "subprocess creation (seen with sandboxed hosts on Windows, issue #143), "
                "relaunch the server with --in-process or BUILD123D_IN_PROCESS=1 — a "
                "degraded mode without crash containment or operation timeouts."
            )
        try:
            msg = self._conn.recv()  # ready signal or startup-error dict
        except EOFError:
            # Worker exited before sending any message (uncaught exception in
            # worker_main before the rlimit try block, or a spawn failure).
            self._proc.join(5)
            exitcode = self._proc.exitcode
            raise RuntimeError(
                f"Worker process failed to start: exited with code {exitcode} "
                "before signalling ready. If your MCP host blocks subprocess "
                "creation (seen with sandboxed hosts on Windows, issue #143), "
                "relaunch the server with --in-process or BUILD123D_IN_PROCESS=1."
            )
        if "warning" in msg:
            import sys as _sys

            print(f"WARNING: {msg['warning']}", file=_sys.stderr)
            # Warning message is followed by the real ready/error signal.
            # Re-apply the timeout: poll() was already satisfied by the warning,
            # so without a second poll() the recv() below would block forever if
            # the worker hangs in _build_session() (e.g. loading a large library).
            if not self._conn.poll(_WORKER_READY_TIMEOUT):
                self._proc.kill()
                self._proc.join(5)
                raise RuntimeError(
                    f"Worker process did not signal ready within {_WORKER_READY_TIMEOUT}s "
                    "after sending a startup warning."
                )
            try:
                msg = self._conn.recv()
            except EOFError:
                self._proc.join(5)
                exitcode = self._proc.exitcode
                raise RuntimeError(
                    f"Worker process failed to start: exited with code {exitcode} "
                    "after sending a startup warning but before signalling ready."
                )
        if "error" in msg:
            self._proc.join(5)
            raise RuntimeError(
                f"Worker process failed to start: {msg['error']}. "
                "Relaunch with --in-process to skip subprocess resource limits."
            )

        # A successful (re)start past the first one means the previous worker
        # died and its session state is gone, so notify observers (the viewer).
        if self._started_once and self._on_restart is not None:
            try:
                self._on_restart()
            except Exception:  # noqa: BLE001 - a viewer callback must never break the restart
                pass
        self._started_once = True

    def set_on_restart(self, callback: "Callable[[], None] | None") -> None:
        """Register a callback fired after each worker restart (not the first)."""
        self._on_restart = callback

    def _kill_worker(self) -> None:
        try:
            self._proc.kill()
            self._proc.join(5)
        except Exception:
            pass

    def _call(self, op: str, args: dict, timeout: int) -> Any:
        # Serialise the IPC critical section so concurrent callers (HTTP shared
        # session, pipelined clients) can't interleave on the pipe. Subclasses
        # override _do_call, not _call, so they inherit this guard. (#322)
        with self._lock:
            return self._do_call(op, args, timeout)

    def _do_call(self, op: str, args: dict, timeout: int) -> Any:
        if not self._proc.is_alive():
            self._start_worker()
            raise RuntimeError(
                "Worker crashed; session restarted. All session state (variables, "
                "shapes, named objects, snapshots) has been lost — re-run your setup code."
            )

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
            raise RuntimeError(
                f"Operation '{op}' timed out after {timeout}s. The worker was killed "
                f"and restarted — all session state (variables, shapes, named objects, "
                f"snapshots) has been lost. Re-run your setup code."
            )

        try:
            response = self._conn.recv()
        except EOFError:
            self._start_worker()
            raise RuntimeError(
                "Worker process crashed unexpectedly; session restarted. All session "
                "state (variables, shapes, named objects, snapshots) has been lost — "
                "re-run your setup code."
            )

        if response["ok"]:
            return response["result"]
        raise RuntimeError(response["error"])

    # --- Session-compatible interface ---
    #
    # Methods below are typed stubs registered with the @_op decorator: the
    # signature is the single typed definition of the wire interface; the
    # decorator records the op in _OPS and replaces the body (which never
    # runs) with bind-and-send. Only ops needing parent-side behaviour beyond
    # "send and wait" (execute, reset) are written out by hand.

    def execute(self, code: str) -> str:
        from build123d_mcp.security import ExecutionTimeout

        try:
            return self._call("execute", {"code": code}, self._exec_timeout)
        except (RuntimeError, ExecutionTimeout) as e:
            return f"Error: {e}"

    def reset(self) -> str:
        if not self._proc.is_alive():
            self._start_worker()
            return "Session reset."
        return self._call("reset", {}, _SHORT_TIMEOUT)

    @_op(_tool(f"{_T}.render:render_view"), _RENDER_TIMEOUT)
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
        raise NotImplementedError

    @_op(_op_pull_viewer_deltas, _RENDER_TIMEOUT)
    def pull_viewer_deltas(self) -> dict:
        raise NotImplementedError

    @_op(_tool(f"{_T}.export:export_file"), _export_budget)
    def export_file(self, filename: str, format: str = "step", object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.measure:measure"), _GEOMETRY_TIMEOUT)
    def measure(self, object_name: str = "", density: float = 0.0, material: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.validate:validate"), _GEOMETRY_TIMEOUT)
    def validate(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.locate:locate_gate_defects"), _export_budget)
    def locate_gate_defects(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.design_audit:design_audit"), _export_budget)
    def design_audit(self, epsilon: float = 0.1, max_params: int = 8) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.verify_spec:verify_spec"), _GEOMETRY_TIMEOUT)
    def verify_spec(self, spec: str = "", spec_path: str = "", object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.verify_spec:suggest_spec"), _GEOMETRY_TIMEOUT)
    def suggest_spec(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.measure:clearance"), _GEOMETRY_TIMEOUT)
    def clearance(self, object_a: str, object_b: str) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.cross_sections:cross_sections"), _GEOMETRY_TIMEOUT)
    def cross_sections(self, object_name: str = "", axis: str = "Z", num_slices: int = 10) -> str:
        raise NotImplementedError

    @_op(_op_save_snapshot, _GEOMETRY_TIMEOUT)
    def save_snapshot(self, name: str) -> str:
        raise NotImplementedError

    @_op(_op_restore_snapshot, _SHORT_TIMEOUT)
    def restore_snapshot(self, name: str) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.diff:diff_snapshot"), _GEOMETRY_TIMEOUT)
    def diff_snapshot(self, snapshot_a: str, snapshot_b: str = "", format: str = "text") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.session_state:session_state"), _SHORT_TIMEOUT)
    def session_state(self) -> str:
        raise NotImplementedError

    @_op(_op_objects_types, _SHORT_TIMEOUT)
    def objects_types(self) -> dict:
        raise NotImplementedError

    @_op(_tool(f"{_T}.analyze_printability:analyze_printability"), _GEOMETRY_TIMEOUT)
    def analyze_printability(
        self,
        object_name: str = "",
        support_angle: float = 45.0,
        nozzle: float = 0.4,
        min_perimeters: int = 2,
        build_volume: str = "",
        bed_tol: float = 0.001,
        min_feature: float = 0.5,
    ) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.health_check:health_check"), _RENDER_TIMEOUT)
    def health_check(self) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.last_error:last_error"), _SHORT_TIMEOUT)
    def last_error(self) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.shape_compare:shape_compare"), _export_budget)
    def shape_compare(self, object_a: str, object_b: str) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.import_step:import_cad_file"), _import_cad_budget)
    def import_cad_file(self, path: str, name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.inspect_drawing:inspect_drawing"), _SHORT_TIMEOUT)
    def inspect_drawing(self, objects: str = "", svg_path: str = "") -> str:
        raise NotImplementedError

    @_op(_op_view_axes, _SHORT_TIMEOUT)
    def view_axes(
        self,
        viewport_origin: tuple,
        viewport_up: tuple = (0.0, 1.0, 0.0),
        look_at: tuple = (0.0, 0.0, 0.0),
    ) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.lint_drawing:lint_drawing"), _SHORT_TIMEOUT)
    def lint_drawing(
        self,
        svg_path: str = "",
        drawing_scale: float = 1.0,
        view_shape_names: list[str] | None = None,
    ) -> str:
        raise NotImplementedError

    @_op(_op_render_drawing, _RENDER_TIMEOUT)
    def render_drawing(self, svg_path: str, width: int = 0, save_to: str = "") -> dict:
        raise NotImplementedError

    @_op(_tool(f"{_T}.save_drawing_annotations:save_drawing_annotations"), _SHORT_TIMEOUT)
    def save_drawing_annotations(self, svg_path: str) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.drafting_api:drafting_api"), _SHORT_TIMEOUT)
    def drafting_api(self) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.find_features:find_holes"), _GEOMETRY_TIMEOUT)
    def find_holes(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.recognizers.countersink:find_countersinks"), _GEOMETRY_TIMEOUT)
    def find_countersinks(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.find_features:find_bosses"), _GEOMETRY_TIMEOUT)
    def find_bosses(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.find_features:find_hole_patterns"), _GEOMETRY_TIMEOUT)
    def find_hole_patterns(self, object_name: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.align_check:align_check"), _GEOMETRY_TIMEOUT)
    def align_check(
        self, object_a: str, object_b: str, axis: str = "Z", mode: str = "flush"
    ) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.resolve:resolve"), _GEOMETRY_TIMEOUT)
    def resolve(self, object_name: str, selector: str, label: str = "") -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.suggest_view_layout:suggest_view_layout"), _GEOMETRY_TIMEOUT)
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
        extents: list[float] | None = None,
        centroid: list[float] | None = None,
    ) -> str:
        raise NotImplementedError

    @_op(_tool(f"{_T}.script:script"), _SHORT_TIMEOUT)
    def script(self, save_to: str = "") -> str:
        raise NotImplementedError

    @_op(_op_search_library, _SHORT_TIMEOUT)
    def search_library(self, query: str = "") -> str:
        raise NotImplementedError

    @_op(_op_load_part, _load_part_budget)
    def load_part(self, name: str, params: str = "") -> str:
        raise NotImplementedError


class InProcessSession(WorkerSession):
    """WorkerSession-compatible session that skips the worker subprocess.

    Fallback for MCP hosts that block subprocess creation, where the spawn'd
    worker never signals ready (#143: Codex desktop on Windows). The Session
    and all OCC/VTK work live in the server process, so this mode is degraded
    by design:

    - no crash containment — an OCCT segfault kills the whole MCP server;
    - no operation timeouts — a runaway execute() blocks the server (the
      in-Session SIGALRM guard still applies on Unix main threads, but not
      on Windows, which is exactly where this mode is needed);
    - OCC/TBB threads share the process with the MCP event loop;
    - platform render helpers still spawn subprocesses where required
      (macOS VTK guard, Linux xvfb) and may also fail under a host that
      blocks spawning — Windows renders in-process and is unaffected.

    Enabled via --in-process / BUILD123D_IN_PROCESS=1.
    """

    def _start_worker(self) -> None:
        self._session, self._library_index = _build_session(
            self._library_path,
            self._exec_timeout,
            self._allow_all_imports,
            self._extra_allowed_imports,
            self._no_sandbox,
        )

    def _kill_worker(self) -> None:
        pass

    def reset(self) -> str:
        # The base method short-circuits via self._proc.is_alive(); there is
        # no process here, so always dispatch the real reset.
        return self._call("reset", {}, _SHORT_TIMEOUT)

    def _do_call(self, op: str, args: dict, timeout: int) -> Any:
        # Same error contract as the worker path: tool exceptions surface as
        # RuntimeError("TypeName: message"), mirroring worker_main's error
        # envelope. (Session.execute() handles ExecutionTimeout internally
        # and returns an error string, so no special-casing is needed here.)
        # Inherits the base _call's lock, so concurrent requests can't run the
        # one shared Session/OCC kernel re-entrantly. (#322)
        try:
            return _dispatch(self._session, op, args, self._library_index)
        except Exception as exc:
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
