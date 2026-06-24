import json
import os
import subprocess
import sys
import tempfile
import time

from build123d_mcp.tools.diff import _shape_diag
from build123d_mcp.tools.measure import _center_of_mass

_COMPARE_MARGIN_S = 15
_COMPARE_MIN_S = 10
# 0 => the worker auto-scales the move threshold to the mesh deflection. A fixed mm
# eps is unsafe: it sits below the independent-tessellation noise floor on large
# parts and fabricates changed regions on unchanged geometry.
_SURFACE_EPS_MM = 0.0


def _surface_compare_in_process(sa, sb) -> dict:
    from build123d_mcp._shape_compare_subprocess import compare_shapes

    return compare_shapes(sa, sb, _SURFACE_EPS_MM)


def _surface_compare_bounded(session, sa, sb) -> dict:
    from build123d_mcp.tools.export import _write_step

    t0 = time.monotonic()
    work = tempfile.mkdtemp(prefix="b123d_compare_")
    a_step = os.path.join(work, "a.step")
    b_step = os.path.join(work, "b.step")
    out_json = os.path.join(work, "surface.json")
    try:
        try:
            _write_step(sa, a_step)
            _write_step(sb, b_step)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not serialise shapes for surface comparison: {exc}"}

        budget = max(60, getattr(session, "exec_timeout", 120))
        remaining = budget - (time.monotonic() - t0) - _COMPARE_MARGIN_S
        if remaining < _COMPARE_MIN_S:
            return {
                "error": (
                    "not enough of the op budget left to surface-compare safely; "
                    "retry on a fresh op."
                )
            }

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build123d_mcp._shape_compare_subprocess",
                    a_step,
                    b_step,
                    out_json,
                    repr(_SURFACE_EPS_MM),
                    repr(remaining),
                ],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return {
                "error": (
                    "surface comparison exceeded the time budget -- the parts are too "
                    "large/complex to tessellate in budget; use volume/bbox deltas and "
                    "targeted measure()/render_view() checks."
                )
            }
        except OSError:
            # Host blocks child-process creation (#143 / InProcessSession): run
            # in-process because there is no worker op-timeout to protect there.
            return _surface_compare_in_process(sa, sb)

        if proc.returncode != 0 or not os.path.exists(out_json):
            return {"error": "surface comparison subprocess failed: " + (proc.stderr or "")[-300:]}
        try:
            with open(out_json) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            return {"error": f"surface comparison produced an unreadable result: {exc}"}
    finally:
        for p in (a_step, b_step, out_json):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass


def shape_compare(session, object_a: str, object_b: str) -> str:
    if object_a not in session.objects:
        raise ValueError(f"Unknown object '{object_a}'. Registered: {list(session.objects.keys())}")
    if object_b not in session.objects:
        raise ValueError(f"Unknown object '{object_b}'. Registered: {list(session.objects.keys())}")

    sa, sb = session.objects[object_a], session.objects[object_b]
    da, db = _shape_diag(sa), _shape_diag(sb)

    ca, cb = _center_of_mass(sa), _center_of_mass(sb)
    offset = round(
        ((cb["x"] - ca["x"]) ** 2 + (cb["y"] - ca["y"]) ** 2 + (cb["z"] - ca["z"]) ** 2) ** 0.5, 4
    )
    surface = _surface_compare_bounded(session, sa, sb)

    payload = {
        "a": {"name": object_a, **da, "center": ca},
        "b": {"name": object_b, **db, "center": cb},
        "delta": {
            "volume": round(db["volume"] - da["volume"], 4),
            "faces": db["faces"] - da["faces"],
            "edges": db["edges"] - da["edges"],
            "vertices": db["vertices"] - da["vertices"],
            "bbox": [round(db["bbox"][i] - da["bbox"][i], 4) for i in range(3)],
            "center_offset": offset,
        },
        "surface_deviation": surface,
        "max_deviation": surface.get("max_deviation"),
        "magnitude_method": surface.get("magnitude_method"),
        "changed": surface.get("changed"),
        "regions": surface.get("regions"),
        "unchanged_elsewhere": surface.get("unchanged_elsewhere"),
        "warnings": surface.get("warnings"),
        "note": (
            "Compares object_a to object_b, not to a reference answer. max_deviation is the "
            "EXACT surface displacement (boolean-measured) when magnitude_method='exact_boolean'; "
            "changed.added_volume/removed_volume give the exact material added/removed. For editing, "
            "verify the changed region(s), displacement, and add/remove volumes match the request. "
            "IMPORTANT: a TANGENTIAL feature move (sliding a hole) and a sub-resolution edit on a very "
            "large part produce no detected region — 'unchanged' then means 'no change above the "
            "detection floor', NOT a guarantee; cross-check the volume/bbox/center deltas and "
            "find_holes for those."
        ),
    }

    return json.dumps(payload, indent=2)
