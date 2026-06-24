"""``locate_gate_defects()`` — report WHERE a solid fails the validity gate.

The validate/export gate says *what* is wrong (``1 non-manifold edge``,
``BRepCheck failed``) but never *where*, so the agent repairs blind — chamfer
here, sew there — burning 50-70 execute() calls per fixture. This returns each
defect with **3D coordinates** (and B-rep face/edge identity), turning a blind
hunt into a targeted fix.

The mesh non-manifold check tessellates (un-interruptible OCC ``BRepMesh``), so —
like the export gate and the render fix — the work runs in a real ``subprocess``
the parent hard-bounds, never blocking/SIGKILLing the worker. On a host that
blocks child-process creation (#143 / InProcessSession) it falls back to running
in-process (those hosts run no worker op-timeout, so there is nothing to kill).
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from build123d_mcp.tools.validate import _resolve_shape

# The locate subprocess is bounded by the time LEFT in the op budget (derived from
# the worker op-timeout = max(60, exec_timeout)) minus a margin, so the un-
# interruptible mesh check is always killed before the parent SIGKILLs the worker.
_LOCATE_MARGIN_S = 15
_LOCATE_MIN_S = 10


def _format(defects: list) -> str:
    if not defects:
        return (
            "No validity defects located — the part passes the structural checks.\n"
            + json.dumps({"count": 0, "defects": []})
        )
    kinds = ", ".join(sorted({d.get("kind", "?") for d in defects}))
    return f"{len(defects)} defect(s) located ({kinds}):\n" + json.dumps(
        {"count": len(defects), "defects": defects}, indent=2
    )


def locate_gate_defects(session, object_name: str = "") -> str:
    """Locate the validity-gate defects on a shape, each with 3D coordinates.

    Returns a defect list: ``brep_invalid_face`` (face index + center + status),
    ``open_edge`` / ``nonmanifold_edge`` (B-rep edge midpoint), and the mesh
    self-touches a CAD scorer rejects — ``mesh_nonmanifold_edge`` and
    ``mesh_nonmanifold_vertex`` (corner-to-corner touch). Empty list means the
    part passes the structural checks. object_name: named object from show()
    (default: current shape).
    """
    t0 = time.monotonic()
    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err

    from build123d_mcp.tools.export import _write_step

    work = tempfile.mkdtemp(prefix="b123d_locate_")
    in_step = os.path.join(work, "in.step")
    out_json = os.path.join(work, "defects.json")
    try:
        try:
            _write_step(shape, in_step)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"could not serialise the shape to locate defects: {exc}"})

        budget = max(60, getattr(session, "exec_timeout", 120))
        remaining = budget - (time.monotonic() - t0) - _LOCATE_MARGIN_S
        if remaining < _LOCATE_MIN_S:
            return json.dumps(
                {
                    "error": "not enough of the op budget left to locate defects safely; retry on a fresh op."
                }
            )

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "build123d_mcp._locate_subprocess", in_step, out_json],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return json.dumps(
                {
                    "error": "defect location exceeded the time budget — the part is too large/complex "
                    "to mesh-check in budget; inspect with measure()/render()."
                }
            )
        except OSError:
            # Host blocks child-process creation (#143 / InProcessSession): no
            # subprocess and no worker op-timeout to kill, so run in-process.
            from build123d_mcp._locate_subprocess import collect_defects

            return _format(collect_defects(shape))

        if proc.returncode != 0 or not os.path.exists(out_json):
            return json.dumps({"error": "defect locator failed: " + (proc.stderr or "")[-300:]})
        try:
            with open(out_json) as f:
                defects = json.load(f)["defects"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            return json.dumps({"error": f"defect locator produced an unreadable result: {exc}"})
        return _format(defects)
    finally:
        for p in (in_step, out_json):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass
