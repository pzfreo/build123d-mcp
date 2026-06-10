"""suggest_view_layout — auto-calculate safe view positions for an engineering drawing.

Third-angle projection layout:

    [plan ]  [      ]
    [front]  [ side ] [ iso ]
                      [title block (bottom-right)]

All dimensions in mm (page / world space).  Scale is the drawing scale factor
(e.g. 2.0 for 2:1).  VIEW_X / VIEW_Y are the page coordinates of the part
centroid after .locate(Location((VIEW_X, VIEW_Y, 0))).
"""

from __future__ import annotations

import json
import math
from typing import Any

# Minimum clearance between any two view outlines (mm).
_GAP = 12.0

# Camera/up conventions per named view.
_CAMERAS: dict[str, dict[str, Any]] = {
    "front": {"camera": (0, -100, 0), "up": (0, 0, 1)},
    "plan": {"camera": (0, 0, 100), "up": (0, 1, 0)},
    "side": {"camera": (100, 0, 0), "up": (0, 0, 1)},
    "iso": {"camera": (80, 80, 80), "up": (0, 0, 1)},
}

# Page size catalogue for fit suggestions.
_PAGE_SIZES = [
    (297.0, 210.0, "A4"),
    (420.0, 297.0, "A3"),
    (594.0, 420.0, "A2"),
]


def _page_half_extents(
    view: str, x: float, y: float, z: float, scale: float
) -> tuple[float, float]:
    """Return (half_w, half_h) on the page for *view* at *scale*."""
    sx, sy, sz = x * scale / 2, y * scale / 2, z * scale / 2
    if view == "front":
        return sx, sz
    if view == "plan":
        return sx, sy
    if view == "side":
        return sy, sz
    if view == "iso":
        # APPROXIMATE: uses 75% of the 3-D bounding-box diagonal as the half-extent.
        # This is a heuristic — actual projected size depends on part shape and the
        # isometric camera angle. For elongated or complex parts it may be 20–40% off.
        # Treat the iso position as a starting point and verify with render_view.
        half = math.sqrt(x**2 + y**2 + z**2) * scale / 2 * 0.75
        return half, half
    return sx, sz  # fallback


def _layout(
    x_size: float,
    y_size: float,
    z_size: float,
    scale: float,
    views: list[str],
    margin: float,
    title_block_w: float,
    title_block_h: float,
) -> dict[str, tuple[float, float]]:
    """Compute {view_name: (VIEW_X, VIEW_Y)} for the requested views."""
    g = _GAP

    # Half-extents for each named view
    hw = {v: _page_half_extents(v, x_size, y_size, z_size, scale) for v in views}

    # Anchor: front view sits above the title-block strip.
    # Its left edge touches the left margin.
    fhw, fhh = hw.get("front", (x_size * scale / 2, z_size * scale / 2))
    fv_x = margin + fhw
    fv_y = margin + title_block_h + g + fhh

    pos: dict[str, tuple[float, float]] = {}

    if "front" in views:
        pos["front"] = (fv_x, fv_y)

    # Plan view: directly above front (same X column, third-angle)
    phw, phh = hw.get("plan", (x_size * scale / 2, y_size * scale / 2))
    pv_x = fv_x
    pv_y = fv_y + fhh + g + phh
    if "plan" in views:
        pos["plan"] = (pv_x, pv_y)

    # Side view: to the right of front (same Y row)
    shw, shh = hw.get("side", (y_size * scale / 2, z_size * scale / 2))
    sv_x = fv_x + fhw + g + shw
    sv_y = fv_y
    if "side" in views:
        pos["side"] = (sv_x, sv_y)

    # Iso view: upper-right quadrant
    ihw, ihh = hw.get("iso", (0.0, 0.0))
    iv_x = (sv_x + shw + g + ihw) if "side" in views else (fv_x + fhw + g + ihw)
    iv_y = pv_y if "plan" in views else (fv_y + fhh + g + ihh)
    if "iso" in views:
        pos["iso"] = (iv_x, iv_y)

    return pos


def _check_fits(
    pos: dict[str, tuple[float, float]],
    hw: dict[str, tuple[float, float]],
    page_w: float,
    page_h: float,
    margin: float,
    title_block_w: float,
    title_block_h: float,
) -> list[str]:
    warnings: list[str] = []
    tb_left = page_w - margin - title_block_w
    tb_top = margin + title_block_h

    for vname, (vx, vy) in pos.items():
        vhw, vhh = hw[vname]
        left, right = vx - vhw, vx + vhw
        bottom, top = vy - vhh, vy + vhh

        if left < margin:
            warnings.append(f"'{vname}' left edge ({left:.1f}) is inside left margin ({margin})")
        if right > page_w - margin:
            warnings.append(
                f"'{vname}' right edge ({right:.1f}) exceeds right margin ({page_w - margin:.1f})"
            )
        if bottom < margin:
            warnings.append(
                f"'{vname}' bottom edge ({bottom:.1f}) is inside bottom margin ({margin})"
            )
        if top > page_h - margin:
            warnings.append(
                f"'{vname}' top edge ({top:.1f}) exceeds top margin ({page_h - margin:.1f})"
            )

        # Overlap with title block (bottom-right rectangle)
        if right > tb_left and bottom < tb_top:
            warnings.append(
                f"'{vname}' bbox [x={left:.1f}–{right:.1f}, y={bottom:.1f}–{top:.1f}] "
                f"overlaps title block [x={tb_left:.1f}–{page_w - margin:.1f}, "
                f"y={margin:.1f}–{tb_top:.1f}]"
            )

    return warnings


def suggest_view_layout(
    session,
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
    """Compute safe VIEW_X / VIEW_Y positions for a multi-view engineering drawing.

    Returns JSON with per-view positions, camera/up/look_at values, and any
    warnings (out-of-bounds, title-block overlap).  If the layout does not fit,
    an alternative scale or page size is suggested.

    If *extents* ([x_size, y_size, z_size] in mm) is given, the layout is
    computed from those numbers directly and no in-session object is needed —
    *centroid* (default [0, 0, 0]) supplies the look_at origin.  This keeps the
    layout math usable when the part failed to import or lives outside the
    session (#229).

    ACCURACY NOTES
    --------------
    Front, plan, and side views: estimates are exact for orthographic projection.
    The bounding-box half-extents map directly to page extents when look_at is at
    the part centroid, so the positions are reliable for any convex or concave part.

    Iso view: position is approximate. The tool uses 75% of the 3-D bounding-box
    diagonal as the page half-extent. For elongated or complex parts this can be
    20–40% off.  Treat the iso VIEW_X / VIEW_Y as a starting point and verify with
    render_view() — adjust manually if the iso overlaps a neighbour.
    """
    if views is None:
        views = ["front", "plan", "side", "iso"]

    unknown = [v for v in views if v not in _CAMERAS]
    if unknown:
        return json.dumps({"error": f"Unknown view(s): {unknown}. Choose from {list(_CAMERAS)}"})

    if extents is not None:
        if len(extents) != 3 or any(e <= 0 for e in extents):
            return json.dumps(
                {"error": f"extents must be three positive sizes [x, y, z], got {extents}"}
            )
        if centroid is not None and len(centroid) != 3:
            return json.dumps({"error": f"centroid must be [x, y, z], got {centroid}"})
        x_size, y_size, z_size = (float(e) for e in extents)
        cx, cy, cz = (float(c) for c in centroid) if centroid else (0.0, 0.0, 0.0)
    else:
        # Resolve shape: named object first, then current_shape as fallback.
        shape = session.objects.get(object_name) or session.current_shape
        if shape is None:
            return json.dumps(
                {
                    "error": f"Object '{object_name}' not found. Run show() first, "
                    "or pass extents=[x, y, z] to lay out without a session object."
                }
            )

        try:
            bb = shape.bounding_box()
            x_size = bb.max.X - bb.min.X
            y_size = bb.max.Y - bb.min.Y
            z_size = bb.max.Z - bb.min.Z
            cx = (bb.min.X + bb.max.X) / 2
            cy = (bb.min.Y + bb.max.Y) / 2
            cz = (bb.min.Z + bb.max.Z) / 2
        except Exception as exc:
            return json.dumps({"error": f"Could not measure '{object_name}': {exc}"})

    hw = {v: _page_half_extents(v, x_size, y_size, z_size, scale) for v in views}
    pos = _layout(x_size, y_size, z_size, scale, views, margin, title_block_w, title_block_h)
    warnings = _check_fits(pos, hw, page_w, page_h, margin, title_block_w, title_block_h)

    # If the layout doesn't fit, suggest an alternative.
    suggestion: dict[str, Any] | None = None
    if warnings:
        # Try progressively smaller scales and larger pages.
        for try_scale in [scale * 0.75, scale * 0.5]:
            for pw, ph, pname in _PAGE_SIZES:
                if pw < page_w and ph < page_h:
                    continue  # don't suggest smaller pages
                try_pos = _layout(
                    x_size, y_size, z_size, try_scale, views, margin, title_block_w, title_block_h
                )
                try_hw = {
                    v: _page_half_extents(v, x_size, y_size, z_size, try_scale) for v in views
                }
                if not _check_fits(try_pos, try_hw, pw, ph, margin, title_block_w, title_block_h):
                    suggestion = {
                        "page_w": pw,
                        "page_h": ph,
                        "scale": round(try_scale, 4),
                        "page_size": pname,
                    }
                    break
            if suggestion:
                break

    # Build output.
    out_views: dict[str, Any] = {}
    for vname in views:
        vx, vy = pos[vname]
        cam = _CAMERAS[vname]
        # look_at in scaled space (for project_to_viewport); iso uses unscaled world coords.
        if vname == "iso":
            look_at = [round(cx, 4), round(cy, 4), round(cz, 4)]
        else:
            look_at = [round(cx * scale, 4), round(cy * scale, 4), round(cz * scale, 4)]
        out_views[vname] = {
            "VIEW_X": round(vx, 2),
            "VIEW_Y": round(vy, 2),
            "half_w": round(hw[vname][0], 2),
            "half_h": round(hw[vname][1], 2),
            "look_at": look_at,
            "camera": list(cam["camera"]),
            "up": list(cam["up"]),
        }

    result: dict[str, Any] = {
        "views": out_views,
        "page_w": page_w,
        "page_h": page_h,
        "scale": scale,
        "part_size": {
            "x": round(x_size, 3),
            "y": round(y_size, 3),
            "z": round(z_size, 3),
            "centroid": [round(cx, 3), round(cy, 3), round(cz, 3)],
        },
        "warnings": warnings,
    }
    if suggestion:
        result["suggestion"] = suggestion
    elif result["warnings"]:
        result["warnings"].append(
            "No automatic fit found — try a smaller scale or a larger page size (A3/A2) manually."
        )

    return json.dumps(result, indent=2)
