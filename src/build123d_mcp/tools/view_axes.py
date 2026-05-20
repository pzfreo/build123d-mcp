"""view_axes — analytic world→page axis mapping for a project_to_viewport call.

Pure Python math — no build123d import. This is intentional: view_axes is
often called before any execute() so the OCC kernel has not been loaded yet.
Importing build123d_drafting here would trigger the OCC cold-start and breach
the 10-second SHORT_TIMEOUT even though none of the math needs OCC.
"""
import json
import math


def _dot3(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

def _sub3(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])

def _scale3(s, v):
    return (s*v[0], s*v[1], s*v[2])

def _cross3(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def _norm3(v):
    mag = math.sqrt(_dot3(v, v))
    return (v[0]/mag, v[1]/mag, v[2]/mag) if mag > 1e-10 else (0.0, 0.0, 0.0)


def view_axes(
    viewport_origin: tuple,
    viewport_up: tuple = (0.0, 1.0, 0.0),
    look_at: tuple = (0.0, 0.0, 0.0),
) -> str:
    """Return JSON {world_X, world_Y, world_Z: [page_axis, sign]}.

    Computes the world→page axis mapping analytically. Useful BEFORE calling
    project_to_viewport to confirm which world axis maps to which page axis
    and with what sign — catches bottom-view/side-view axis swaps before they
    show up in the render.

    Returns JSON like {"world_X": ["page_X", -1.0], "world_Y": ["page_Y", 1.0],
    "world_Z": ["depth", 0.0]} — for a bottom-view origin (0,0,-100), world-X
    flips to negative page-X.

    Args:
        viewport_origin: camera position, same arg as project_to_viewport.
        viewport_up: up vector. Defaults to (0,1,0).
        look_at: target point. Defaults to origin (0,0,0).
    """
    vo = tuple(float(x) for x in viewport_origin)
    la = tuple(float(x) for x in look_at)
    vu = tuple(float(x) for x in viewport_up)

    view_dir = _norm3(_sub3(la, vo))
    page_y = _norm3(_sub3(vu, _scale3(_dot3(vu, view_dir), view_dir)))
    page_x = _cross3(view_dir, page_y)

    result = {}
    for name, world_v in [
        ("world_X", (1.0, 0.0, 0.0)),
        ("world_Y", (0.0, 1.0, 0.0)),
        ("world_Z", (0.0, 0.0, 1.0)),
    ]:
        px = _dot3(world_v, page_x)
        py = _dot3(world_v, page_y)
        if abs(px) < 1e-9 and abs(py) < 1e-9:
            result[name] = ("depth", 0.0)
        elif abs(px) >= abs(py):
            result[name] = ("page_X", float(round(px / abs(px), 1)))
        else:
            result[name] = ("page_Y", float(round(py / abs(py), 1)))

    return json.dumps(
        {k: [v[0], round(v[1], 6)] for k, v in result.items()},
        indent=2,
    )
