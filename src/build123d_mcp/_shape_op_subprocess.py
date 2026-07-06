"""Bounded out-of-process runner for the read-only geometry tools (#360).

Run as ``python -m build123d_mcp._shape_op_subprocess <manifest.json> <out.json>``.

``manifest`` is ``{"op": <name>, "params": {...}, "shapes": {label: step_path}}``.
Imports each STEP and runs the SAME pure computation the tool uses in-worker, then
writes ``{"result": <str>}`` (or ``{"error": <str>}`` on any failure) to ``out.json``.
Mirrors ``_locate_subprocess``: STEP in, JSON out, every error captured to structured
output so the parent never sees a bare crash. The parent bounds it with
``subprocess.run(timeout=...)`` so an un-interruptible native call is killed cleanly
instead of SIGKILLing the worker.

``validate`` is NOT here: it keeps its B-rep checks in the worker and isolates only its
mesh stitch (via ``validate._run_mesh_gate_subprocess``), the same way ``export`` does,
so a subprocess kill degrades to "mesh not verified" without losing the B-rep verdict.
"""

import json
import sys


def _run(op: str, shapes: dict, params: dict) -> str:
    from build123d_mcp.tools.cross_sections import _cross_sections_report
    from build123d_mcp.tools.measure import _clearance_report, _measure_report

    if op == "measure":
        return _measure_report(shapes[""], params["rho"])
    if op == "cross_sections":
        return _cross_sections_report(shapes[""], params["axis"], params["num_slices"])
    if op == "clearance":
        return _clearance_report(shapes["a"], shapes["b"])
    raise ValueError(f"unknown op {op!r}")


def main(manifest_path: str, out_path: str) -> None:
    from build123d import import_step

    with open(manifest_path) as f:
        manifest = json.load(f)
    try:
        # measure/cross_sections are wrapper-insensitive; clearance normalises the shape
        # wrapper itself (measure._surface_distance), so the re-imported shape can be used
        # as-is here regardless of whether STEP round-tripped Solid↔Compound.
        shapes = {label: import_step(path) for label, path in manifest["shapes"].items()}
        payload = {"result": _run(manifest["op"], shapes, manifest.get("params", {}))}
    except Exception as exc:  # noqa: BLE001 - any failure → structured error, not a crash
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    with open(out_path, "w") as f:
        json.dump(payload, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
