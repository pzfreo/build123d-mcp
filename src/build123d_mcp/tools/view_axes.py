"""view_axes — analytic world→page axis mapping for a project_to_viewport call.

Pure Python math — no build123d import. This is intentional: view_axes is
often called before any execute() so the OCC kernel has not been loaded yet.
Importing build123d_drafting here would trigger the OCC cold-start and breach
the 10-second SHORT_TIMEOUT even though none of the math needs OCC.
"""

import json
import math


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub3(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale3(s, v):
    return (s * v[0], s * v[1], s * v[2])


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm3(v):
    mag = math.sqrt(_dot3(v, v))
    return (v[0] / mag, v[1] / mag, v[2] / mag) if mag > 1e-10 else (0.0, 0.0, 0.0)


def _helper_expr(param: str, view_var: str, offset: float, sign: float) -> str:
    """Build a compact coordinate-helper expression string."""
    if offset == 0.0:
        inner = param
    elif offset < 0:
        inner = f"({param} + {-offset})"
    else:
        inner = f"({param} - {offset})"
    return f"{view_var} + {inner} * SCALE" if sign == 1.0 else f"{view_var} - {inner} * SCALE"


def view_axes(
    viewport_origin: tuple,
    viewport_up: tuple = (0.0, 1.0, 0.0),
    look_at: tuple = (0.0, 0.0, 0.0),
) -> str:
    """Return the world→page axis mapping plus the look_at offset and helper snippet.

    project_to_viewport centres the projected compound at look_at. When that
    compound is .locate()-d at (VIEW_X, VIEW_Y), the look_at point maps to
    exactly (VIEW_X, VIEW_Y). The correct coordinate helper must subtract the
    look_at component along each active world axis — omitting this term shifts
    every annotation by look_at_component × SCALE mm.

    Returns JSON with four fields:
      world_X/Y/Z : [page_axis, sign]   — existing axis mapping
      look_at_offset : {page_X, page_Y} — look_at world component projected onto
                                          each page axis (multiply by SCALE to get
                                          the page-space offset to subtract)
      helper_snippet : str              — ready-to-paste Python coordinate helpers
                                          (replace VIEW_X/VIEW_Y/SCALE with your values)

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

    axis_map = {}
    la_world = {"world_X": la[0], "world_Y": la[1], "world_Z": la[2]}
    for name, world_v in [
        ("world_X", (1.0, 0.0, 0.0)),
        ("world_Y", (0.0, 1.0, 0.0)),
        ("world_Z", (0.0, 0.0, 1.0)),
    ]:
        px = _dot3(world_v, page_x)
        py = _dot3(world_v, page_y)
        if abs(px) < 1e-9 and abs(py) < 1e-9:
            axis_map[name] = ("depth", 0.0)
        elif abs(px) >= abs(py):
            axis_map[name] = ("page_X", float(round(px / abs(px), 1)))
        else:
            axis_map[name] = ("page_Y", float(round(py / abs(py), 1)))

    # look_at offset: for each page axis, the look_at world component from the dominant
    # world axis (highest projection magnitude). A slightly tilted view can map two world
    # axes to the same page axis; use the one with the larger |dot product|, not the first.
    # Correct helper formula: page_coord = VIEW + (world - offset) * SCALE * sign
    _world_vecs = {
        "world_X": (1.0, 0.0, 0.0),
        "world_Y": (0.0, 1.0, 0.0),
        "world_Z": (0.0, 0.0, 1.0),
    }
    la_offset: dict[str, float] = {"page_X": 0.0, "page_Y": 0.0}
    _best_mag: dict[str, float] = {"page_X": 0.0, "page_Y": 0.0}
    for world_name, (page_axis, _sign) in axis_map.items():
        if page_axis not in _best_mag:
            continue
        proj_vec = page_x if page_axis == "page_X" else page_y
        mag = abs(_dot3(_world_vecs[world_name], proj_vec))
        if mag > _best_mag[page_axis]:
            _best_mag[page_axis] = mag
            la_offset[page_axis] = round(la_world[world_name], 6)

    # Ready-to-paste helper snippet — one function per active (non-depth) axis.
    _param = {"world_X": "x", "world_Y": "y", "world_Z": "z"}
    _view = {"page_X": "VIEW_X", "page_Y": "VIEW_Y"}
    lines = ["# Coordinate helpers (replace VIEW_X/VIEW_Y/SCALE with your values):"]
    for world_name in ("world_X", "world_Y", "world_Z"):
        page_axis, sign = axis_map[world_name]
        if page_axis == "depth":
            continue
        param = _param[world_name]
        expr = _helper_expr(param, _view[page_axis], la_world[world_name], sign)
        lines.append(f"# def {param.upper()}({param}): return {expr}")

    return json.dumps(
        {
            **{k: [v[0], round(v[1], 6)] for k, v in axis_map.items()},
            "look_at_offset": la_offset,
            "helper_snippet": "\n".join(lines),
        },
        indent=2,
    )
