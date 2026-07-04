"""Out-of-process VTK render worker for render_view on macOS (see tools/render.py).

Run as ``python -m build123d_mcp._vtk_render_subprocess_worker <in.pkl> <out.png>``.
``in.pkl`` holds the pickled render inputs (already-tessellated ``shape_data`` plus
view parameters); the rendered PNG bytes are written to ``out.png``.

Why a separate program (not ``multiprocessing``): the worker runs as a daemon, and
daemon processes cannot spawn ``multiprocessing`` children — so the only way to
bound VTK's macOS Cocoa render (which can freeze the window server / hang on first
offscreen-context creation) is a real subprocess the parent can hard-kill via
``subprocess.run(timeout=...)``. Mirrors ``_tessellate_subprocess`` (#357/#308).
"""

import pickle
import sys


def main(in_path: str, out_path: str) -> None:
    from build123d_mcp.tools.render import _vtk_render_tesselated

    with open(in_path, "rb") as f:
        args = pickle.load(f)

    png = _vtk_render_tesselated(
        args["shape_data"],
        args["direction"],
        args["clip_plane"],
        args["clip_at"],
        args["azimuth"],
        args["elevation"],
        args["labels"],
    )
    with open(out_path, "wb") as f:
        f.write(png)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
