"""Run a read-only geometry computation out-of-process when the shape is large (#360).

A native OCC analysis — ``BRepCheck`` validity, ``BRepMesh`` tessellation, a boolean —
on a big B-rep is un-interruptible and can outlast the op timeout; the parent then
SIGKILLs the whole worker (the same session-destroying class as #357/#358). This
generalises the bounded-subprocess pattern that ``locate``/``shape_compare`` already
use into one helper the read-only tools (measure/validate/cross_sections/clearance)
share instead of copy-pasting.

Size-gated: for a small shape the native call is fast and a STEP round-trip would
dominate, so only shapes at/above ``_FACE_GATE`` faces pay the isolation. On a host
that blocks child-process creation (#143 / InProcessSession) it runs in-process — such
hosts run no worker op-timeout, so there is nothing to SIGKILL.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

from build123d_mcp.tools._budget import op_budget

# Face count at/above which a read-only op is isolated out-of-process. A heuristic:
# the field failures (#360) were ~1200-face imported solids, while a typical modelled
# part is well under this, so common measure()-after-every-boolean calls keep the fast
# in-worker path. Tunable — face count is the cheapest proxy for native-analysis cost.
_FACE_GATE = 800
# The subprocess is bounded by the op budget left minus a margin, so the native call
# is always killed before the parent SIGKILLs the worker (op_budget == the parent
# watchdog for these ops — _export_budget). Below _MIN_S there isn't enough left for a
# safe round-trip.
_MARGIN_S = 15
_MIN_S = 10


def _face_count(shape) -> int:
    return len(shape.faces())


def _is_large(shapes) -> bool:
    """True if any shape is complex enough to be worth isolating. A shape we can't
    cheaply size (raises) is treated as large — err toward the safe (bounded) path."""
    for shp in shapes:
        try:
            if _face_count(shp) >= _FACE_GATE:
                return True
        except Exception:  # noqa: BLE001 - unsizable shape → isolate to be safe
            return True
    return False


def _budget_error(op: str, faces: int, budget: int) -> str:
    return (
        f"Error: {op}() exceeded the {budget}s time budget on this large shape "
        f"(~{faces} faces) and was stopped without killing the session. Simplify the "
        f"geometry, or raise the limit with --exec-timeout N / BUILD123D_EXEC_TIMEOUT=N."
    )


def run_bounded_shape_op(
    session,
    op: str,
    shape_map: dict,
    params: dict,
    in_process: Callable[[], str],
    *,
    budget: int | None = None,
) -> str:
    """Run read-only op ``op`` on the given shapes, isolating large ones out-of-process.

    ``shape_map`` maps a label to a build123d shape to serialise (single-shape ops use
    ``{"": shape}``; clearance uses ``{"a": a, "b": b}``). ``params`` are the scalar
    arguments (density, axis, …) forwarded to the subprocess. ``in_process`` runs the
    same computation in-worker and is used for the fast path (small shape), the #143
    no-subprocess host, and when the budget is too tight for a safe round-trip.

    ``budget`` is the caller's parent watchdog (seconds); the subprocess is bounded at
    ``budget - _MARGIN_S`` so it's killed before that watchdog SIGKILLs the worker.
    Defaults to ``op_budget(session)`` (correct for the measure/validate/… TOOLS, whose
    watchdog is ``_export_budget == op_budget``). An in-namespace primitive called inside
    ``execute()`` runs under the SMALLER ``exec_timeout`` watchdog, so it passes that.
    """
    if not _is_large(shape_map.values()):
        return in_process()

    from build123d_mcp.tools.export import _write_step

    if budget is None:
        budget = op_budget(session)
    t0 = time.monotonic()
    faces = max((_face_count(s) for s in shape_map.values()), default=0)
    work = tempfile.mkdtemp(prefix="b123d_shapeop_")
    steps = {label: os.path.join(work, f"{i}.step") for i, label in enumerate(shape_map)}
    manifest_path = os.path.join(work, "manifest.json")
    out_json = os.path.join(work, "out.json")
    try:
        try:
            for label, shp in shape_map.items():
                _write_step(shp, steps[label])
        except Exception as exc:  # noqa: BLE001 - couldn't serialise → surface, don't risk in-worker
            return f"Error: could not serialise the shape to run {op}() safely: {exc}"

        remaining = budget - (time.monotonic() - t0) - _MARGIN_S
        if remaining < _MIN_S:
            return _budget_error(op, faces, budget)

        with open(manifest_path, "w") as f:
            json.dump({"op": op, "params": params, "shapes": steps}, f)

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build123d_mcp._shape_op_subprocess",
                    manifest_path,
                    out_json,
                ],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return _budget_error(op, faces, budget)
        except OSError:
            # Host blocks child-process creation (#143 / InProcessSession): no
            # subprocess and no worker op-timeout to kill, so run in-process.
            return in_process()

        if proc.returncode != 0 or not os.path.exists(out_json):
            return f"Error: {op}() subprocess failed: " + (proc.stderr or "")[-300:]
        try:
            with open(out_json) as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            return f"Error: {op}() produced an unreadable result: {exc}"
        if "error" in payload:
            return f"Error: {op}() failed: {payload['error']}"
        return payload["result"]
    finally:
        for p in (*steps.values(), manifest_path, out_json):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass
