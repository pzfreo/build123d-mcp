"""Path-safety coverage for the file-writing and file-reading MCP tools.

Writes (issue #180): export() and render_view(save_to=...) already routed
writes through ``safe_output_path``; script(), render_drawing(), and
save_drawing_annotations() wrote to caller-provided paths directly. The
write tests below assert the same central rejection plus a happy-path write.

Reads (issue #188): import_cad_file(), render_drawing(svg_path=...),
inspect_drawing(svg_path=...), and lint_drawing(svg_path=...) (plus their
.dims.json sidecar reads) read caller-provided paths directly. The read
tests assert ``safe_input_path`` rejects traversal, outside-root, and
symlink-escape paths, with a happy-path read from a temp root.
"""

import json
import os
import sys

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.import_step import import_cad_file
from build123d_mcp.tools.inspect_drawing import inspect_drawing
from build123d_mcp.tools.lint_drawing import lint_drawing
from build123d_mcp.tools.render_drawing import render_drawing
from build123d_mcp.tools.save_drawing_annotations import save_drawing_annotations
from build123d_mcp.tools.script import script

_OUTSIDE_ROOT_PATH = (
    r"C:\Windows\System32\drivers\etc\hosts" if sys.platform == "win32" else "/etc/passwd"
)
_TRAVERSAL = "../../etc/passwd"
_VALID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm">\n'
    '  <rect x="5" y="5" width="40" height="40" fill="green"/>\n'
    "</svg>"
)


@pytest.fixture
def session():
    return Session()


# --- script(save_to=...) ---------------------------------------------------


def test_script_save_to_path_traversal_rejected(session):
    session.execute("from build123d import *\nresult = Box(5, 5, 5)")
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        script(session, save_to=_TRAVERSAL + ".py")


def test_script_save_to_outside_roots_rejected(session):
    session.execute("from build123d import *\nresult = Box(5, 5, 5)")
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        script(session, save_to=_OUTSIDE_ROOT_PATH)


def test_script_save_to_tmp_allowed(session, tmp_path):
    session.execute("from build123d import *\nresult = Box(5, 5, 5)")
    target = tmp_path / "out.py"
    result = json.loads(script(session, save_to=str(target)))
    assert os.path.exists(target)
    assert "script_path" in result


# --- render_drawing(save_to=...) -------------------------------------------


def test_render_drawing_save_to_path_traversal_rejected(tmp_path):
    svg = tmp_path / "in.svg"
    svg.write_text(_VALID_SVG)
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        render_drawing(str(svg), width=120, save_to=_TRAVERSAL + ".png")


def test_render_drawing_save_to_outside_roots_rejected(tmp_path):
    svg = tmp_path / "in.svg"
    svg.write_text(_VALID_SVG)
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        render_drawing(str(svg), width=120, save_to=_OUTSIDE_ROOT_PATH)


def test_render_drawing_save_to_tmp_allowed(tmp_path):
    svg = tmp_path / "in.svg"
    svg.write_text(_VALID_SVG)
    out = tmp_path / "out.png"
    result = render_drawing(str(svg), width=120, save_to=str(out))
    assert out.exists()
    assert result.get("error") is None


# --- save_drawing_annotations(svg_path -> sidecar) -------------------------


def test_save_drawing_annotations_path_traversal_rejected(session):
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        save_drawing_annotations(session, _TRAVERSAL + ".svg")


def test_save_drawing_annotations_outside_roots_rejected(session):
    # /etc/passwd.svg derives sidecar /etc/passwd.dims.json — outside the roots.
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        save_drawing_annotations(session, _OUTSIDE_ROOT_PATH + ".svg")


def test_save_drawing_annotations_tmp_allowed(session, tmp_path):
    svg_path = str(tmp_path / "drawing.svg")
    result = save_drawing_annotations(session, svg_path)
    assert (tmp_path / "drawing.dims.json").exists()
    assert "annotation" in result


# ===========================================================================
# Reads (issue #188) — import_cad_file / render_drawing / inspect_drawing /
# lint_drawing route svg_path / path through safe_input_path.
# ===========================================================================

_READ_REJECT = "outside the allowed read roots"
_skip_symlink = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)


def _write_valid_svg(tmp_path) -> str:
    svg = tmp_path / "drawing.svg"
    svg.write_text(_VALID_SVG)
    return str(svg)


# --- import_cad_file(path=...) ---------------------------------------------


def test_import_cad_file_path_traversal_rejected(session):
    with pytest.raises(ValueError, match=_READ_REJECT):
        import_cad_file(session, _TRAVERSAL + ".step")


def test_import_cad_file_outside_roots_rejected(session):
    # Outside-root path is rejected by the root policy before the
    # not-found / extension checks the tool used to reach first.
    with pytest.raises(ValueError, match=_READ_REJECT):
        import_cad_file(session, _OUTSIDE_ROOT_PATH + ".step")


@_skip_symlink
def test_import_cad_file_symlink_escape_rejected(session, tmp_path):
    # A symlink inside an allowed root pointing outside it must be rejected:
    # realpath resolves the link target, which lands outside the roots.
    link = tmp_path / "escape.step"
    os.symlink(_OUTSIDE_ROOT_PATH, link)
    with pytest.raises(ValueError, match=_READ_REJECT):
        import_cad_file(session, str(link))


def test_import_cad_file_tmp_allowed(session, tmp_path):
    from build123d import Box, export_stl

    stl = tmp_path / "box.stl"
    export_stl(Box(5, 5, 5), str(stl))
    result = json.loads(import_cad_file(session, str(stl)))
    assert result["imported"] == "box"
    assert result["format"] == "stl"


# --- render_drawing(svg_path=...) ------------------------------------------


def test_render_drawing_svg_path_traversal_rejected():
    with pytest.raises(ValueError, match=_READ_REJECT):
        render_drawing(_TRAVERSAL + ".svg")


def test_render_drawing_svg_path_outside_roots_rejected():
    with pytest.raises(ValueError, match=_READ_REJECT):
        render_drawing(_OUTSIDE_ROOT_PATH + ".svg")


def test_render_drawing_svg_path_tmp_allowed(tmp_path):
    result = render_drawing(_write_valid_svg(tmp_path), width=120)
    assert result.get("error") is None
    assert result["size_bytes"] > 0


# --- inspect_drawing(svg_path=...) -----------------------------------------


def test_inspect_drawing_svg_path_traversal_rejected(session):
    with pytest.raises(ValueError, match=_READ_REJECT):
        inspect_drawing(session, svg_path=_TRAVERSAL + ".svg")


def test_inspect_drawing_svg_path_outside_roots_rejected(session):
    with pytest.raises(ValueError, match=_READ_REJECT):
        inspect_drawing(session, svg_path=_OUTSIDE_ROOT_PATH + ".svg")


def test_inspect_drawing_svg_path_tmp_allowed(session, tmp_path):
    result = json.loads(inspect_drawing(session, svg_path=_write_valid_svg(tmp_path)))
    assert result["mode"] == "svg"


# --- lint_drawing(svg_path=...) --------------------------------------------


def test_lint_drawing_svg_path_traversal_rejected(session):
    with pytest.raises(ValueError, match=_READ_REJECT):
        lint_drawing(session, svg_path=_TRAVERSAL + ".svg")


def test_lint_drawing_svg_path_outside_roots_rejected(session):
    with pytest.raises(ValueError, match=_READ_REJECT):
        lint_drawing(session, svg_path=_OUTSIDE_ROOT_PATH + ".svg")


def test_lint_drawing_svg_path_tmp_allowed(session, tmp_path):
    result = json.loads(lint_drawing(session, svg_path=_write_valid_svg(tmp_path)))
    assert "violations" in result
