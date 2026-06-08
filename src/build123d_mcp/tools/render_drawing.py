"""render_drawing — rasterise an existing SVG file to PNG.

This complements render_view (which takes build123d shapes from the session)
by accepting an SVG path written outside the sandbox — typically by a
short Python script that does the ExportSVG call directly. The LLM can
then inspect the rendered PNG inline.

resvg-py is already a runtime dep (used by render_view's 2D path).
"""
import os
import re

# Build123d emits SVG with physical units (mm/cm/in) on width/height attributes.
# resvg-py needs unitless pixel-equivalent values; this regex strips the unit.
_UNIT_RE = re.compile(r'(width|height)="([\d.]+)(mm|cm|in)"')


def render_drawing(svg_path: str, width: int = 0, save_to: str = "") -> dict:
    """Rasterise an SVG file at svg_path to PNG.

    Args:
        svg_path: path to an SVG file on disk.
        width: output pixel width (default 1200). The PNG height is set by
            the SVG aspect ratio.
        save_to: optional path to write the PNG to. If empty, the PNG bytes
            are returned in the result for an in-memory delivery.

    Returns dict with at least {png: bytes} and {png_path: str} if save_to.
    On error returns {error: str} so the caller can surface it.
    """
    if not os.path.isfile(svg_path):
        return {"error": f"SVG file not found: {svg_path}"}

    try:
        import resvg_py
    except ImportError:
        return {"error": "resvg-py is not installed in the server runtime."}

    try:
        with open(svg_path, "r", encoding="utf-8") as f:
            svg = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"Could not read {svg_path}: {e}"}

    svg = _UNIT_RE.sub(r'\1="\2"', svg, count=2)
    out_width = width if width > 0 else 1200

    try:
        png = bytes(resvg_py.svg_to_bytes(
            svg_string=svg, width=out_width, background="#ffffff",
        ))
    except Exception as e:
        return {"error": f"resvg failed: {type(e).__name__}: {e}"}

    result: dict = {"png": png, "size_bytes": len(png), "width": out_width}
    if save_to:
        # Route through the central path policy (rejects traversal / writes
        # outside the allowed roots) before opening, like render_view(save_to=...).
        from build123d_mcp.tools._paths import safe_output_path
        abs_path = safe_output_path(save_to)
        try:
            with open(abs_path, "wb") as f:
                f.write(png)
            result["png_path"] = abs_path
        except OSError as e:
            result["error"] = f"Could not write {abs_path}: {e}"
    return result
