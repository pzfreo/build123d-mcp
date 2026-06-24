"""Out-of-process tessellation worker for render_view (see tools/render.py).

Run as ``python -m build123d_mcp._tessellate_subprocess <manifest.json> <out.pkl>
<linear_deflection> <angular_deflection>``. The manifest is ``[{"name","step"}]``;
each STEP is imported and tessellated, and the result ``{name: (verts, tris)}``
is pickled to ``out.pkl``.

Why a separate program (not ``multiprocessing``): the worker runs as a daemon,
and daemon processes cannot spawn ``multiprocessing`` children — so the only way
to bound OCC's un-interruptible ``BRepMesh`` (which can run for minutes on a
complex part and blow the worker op-timeout, SIGKILLing the session) is a real
subprocess the parent can hard-kill via ``subprocess.run(timeout=...)``. Mirrors
the export mesh gate (``_gate_subprocess``).
"""

import json
import pickle
import sys


def main(
    manifest_path: str, out_path: str, linear_deflection: str, angular_deflection: str
) -> None:
    from build123d import import_step

    lin = float(linear_deflection)
    ang = float(angular_deflection)
    manifest = json.loads(open(manifest_path).read())

    meshes: dict = {}
    failed: list = []
    for item in manifest:
        try:
            shape = import_step(item["step"])
            verts, tris = shape.tessellate(lin, ang)
            meshes[item["name"]] = ([(v.X, v.Y, v.Z) for v in verts], [list(t) for t in tris])
        except Exception as exc:  # noqa: BLE001 - one bad shape shouldn't fail the render
            failed.append(f"{item['name']}: {exc}")

    with open(out_path, "wb") as f:
        pickle.dump({"meshes": meshes, "failed": failed}, f, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
