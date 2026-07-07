"""Out-of-process mesh-validity check for the export gate.

Run as ``python -m build123d_mcp._gate_subprocess <step_path>``. Loads the STEP,
runs the exact mesh check (open-edge ladder + non-manifold + face-tessellation)
with NO internal time deadline, and prints the result as a single
``GATE_RESULT:{...}`` line on stdout.

Why a separate process: the dominant cost is OCC ``BRepMesh`` tessellation, an
un-interruptible native call that an in-process wall-clock budget cannot stop —
so on a very large/complex part the in-process gate must SKIP the mesh check to
avoid running past the worker op-timeout (which would kill the session). Running
it here lets the caller bound it with a hard ``subprocess`` timeout: a large
part gets a generous budget and is actually checked, and an over-budget part is
killed cleanly (the worker is never blocked). The marker prefix lets the caller
recover the JSON even though OCC writes progress noise to stdout.
"""

import json
import sys

_MARKER = "GATE_RESULT:"


def main() -> None:
    if len(sys.argv) < 2:
        print(_MARKER + json.dumps({"error": "no step_path"}))
        return
    step_path = sys.argv[1]
    try:
        from build123d import Compound, import_step

        from build123d_mcp.tools.validate import _EXACT_ISOLATED_MAX_TRIS, _mesh_defects_exact

        shp = import_step(step_path)
        solids = shp.solids()
        if not solids:
            shape = shp
        elif len(solids) == 1:
            shape = solids[0]
        else:
            shape = Compound(children=list(solids))
        # deadline=inf: no in-process time bail — the parent's subprocess timeout
        # bounds us, so the triangle ceiling can be much higher than the in-worker
        # checks' (#381) — it's a memory/sanity backstop here, not a time proxy.
        nm, open_edges, untri, nmv, vdefl, ok = _mesh_defects_exact(
            shape, max_triangles=_EXACT_ISOLATED_MAX_TRIS, deadline=float("inf")
        )
        print(
            _MARKER
            + json.dumps(
                {
                    "nm": nm,
                    "open": open_edges,
                    "untri": untri,
                    "nmv": nmv,
                    "vdefl": vdefl,
                    "ok": ok,
                }
            )
        )
    except Exception as exc:  # noqa: BLE001 — report and exit cleanly
        print(_MARKER + json.dumps({"error": f"{type(exc).__name__}: {exc}"}))


if __name__ == "__main__":
    main()
