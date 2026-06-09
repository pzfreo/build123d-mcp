"""Unit tests for the render result-marshalling helpers (tools/_marshal.py).

render_view's marshalling is covered end-to-end by
test_outcomes.py::test_mcp_render_returns_image_and_file_path; render_drawing's
marshalled output (ImageContent + [SEND:] marker + "Rasterised …" message) had
no test, so these lock it after the #183 part-B extraction.
"""

import base64
import os

from build123d_mcp.tools._marshal import marshal_render_drawing

_PNG = b"\x89PNG\r\n\x1a\nfake-bytes"


def test_marshal_render_drawing_error_branch():
    out = marshal_render_drawing({"error": "boom"}, "d.svg", "")
    assert len(out) == 1
    assert out[0].type == "text"
    assert out[0].text == "render_drawing error: boom"


def test_marshal_render_drawing_png_to_tempfile():
    out = marshal_render_drawing(
        {"png": _PNG, "size_bytes": len(_PNG), "width": 120}, "drawing.svg", ""
    )
    assert out[0].type == "image"
    assert out[0].mimeType == "image/png"
    assert out[0].data == base64.b64encode(_PNG).decode()

    assert out[1].type == "text" and out[1].text.startswith("[SEND: ")
    sent = out[1].text[len("[SEND: ") : -1]
    assert os.path.basename(sent).startswith("build123d_drawing_")
    assert os.path.isfile(sent)
    os.unlink(sent)

    assert out[2].text == f"Rasterised drawing.svg to PNG ({len(_PNG)} bytes, width=120px)."


def test_marshal_render_drawing_uses_save_to_path():
    # When save_to is set and the worker wrote the file, deliver that path
    # directly (no tempfile) — preserves the `if save_to and png_path` branch.
    out = marshal_render_drawing(
        {"png": _PNG, "png_path": "/tmp/out.png", "size_bytes": 9, "width": 50},
        "drawing.svg",
        "/tmp/out.png",
    )
    assert out[1].text == "[SEND: /tmp/out.png]"
