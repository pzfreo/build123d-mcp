"""Marshal worker render results into MCP content lists.

``render_view`` and ``render_drawing`` route through the worker and get back a
plain ``dict``; turning that into a ``list[ImageContent | TextContent]`` (with
tempfile fallback for the ``[SEND: …]`` delivery markers) is the only real
business logic left in those tool wrappers. Kept here so ``server.py`` stays
registration-only.
"""

import base64
import os
import tempfile

from mcp.types import ImageContent, TextContent


def marshal_render_view(result: dict) -> list:
    contents: list = []

    # Helper: prefer the user-requested save_to path (in result["<fmt>_path"])
    # over a fresh tempfile. When save_to is set, render_view has already
    # written the file at the requested path; we just need a path to deliver
    # via the [SEND:] marker. When save_to is empty, fall back to a tempfile.
    def _path_for(key: str, suffix: str) -> str:
        saved = result.get(f"{key}_path")
        if saved:
            return saved
        fd, p = tempfile.mkstemp(suffix=suffix, prefix="build123d_")
        os.close(fd)
        with open(p, "wb") as f:
            f.write(result[key])
        return p

    if "png" in result:
        path = _path_for("png", ".png")
        contents.append(
            ImageContent(
                type="image",
                data=base64.b64encode(result["png"]).decode(),
                mimeType="image/png",
            )
        )
        contents.append(TextContent(type="text", text=f"[SEND: {path}]"))

    if "svg" in result:
        path = _path_for("svg", ".svg")
        contents.append(TextContent(type="text", text=f"[SEND: {path}]"))

    # DXF is a CAD interchange format, not an image — emit only the file marker
    # so clients deliver the file without the ImageContent base64 round-trip.
    if "dxf" in result:
        path = _path_for("dxf", ".dxf")
        contents.append(
            TextContent(
                type="text",
                text=f"DXF saved: {path}\n[SEND: {path}]",
            )
        )
    if result.get("fallback"):
        contents.append(TextContent(type="text", text=result["fallback"]))
    if result.get("png_error"):
        contents.append(TextContent(type="text", text=f"PNG render failed: {result['png_error']}"))
    if result.get("png_warnings"):
        for w in result["png_warnings"]:
            contents.append(TextContent(type="text", text=f"Warning: {w}"))
    if result.get("label_warnings"):
        for w in result["label_warnings"]:
            contents.append(TextContent(type="text", text=f"Warning: {w}"))
    if result.get("render_mode"):
        contents.append(
            TextContent(
                type="text",
                text=f"Rendered via {result['render_mode']} pipeline.",
            )
        )
    return contents


def marshal_render_drawing(result: dict, svg_path: str, save_to: str) -> list:
    if "error" in result:
        return [TextContent(type="text", text=f"render_drawing error: {result['error']}")]

    contents: list = []
    if "png" in result:
        if save_to and result.get("png_path"):
            path = result["png_path"]
        else:
            fd, path = tempfile.mkstemp(suffix=".png", prefix="build123d_drawing_")
            os.close(fd)
            with open(path, "wb") as f:
                f.write(result["png"])
        contents.append(
            ImageContent(
                type="image",
                data=base64.b64encode(result["png"]).decode(),
                mimeType="image/png",
            )
        )
        contents.append(TextContent(type="text", text=f"[SEND: {path}]"))
        contents.append(
            TextContent(
                type="text",
                text=f"Rasterised {svg_path} to PNG ({result['size_bytes']} bytes, width={result['width']}px).",
            )
        )
    return contents
