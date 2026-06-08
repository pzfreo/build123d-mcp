"""Path-safety coverage for the remaining file-writing MCP tools (issue #180).

export() and render_view(save_to=...) already route writes through
``safe_output_path``; script(), render_drawing(), and
save_drawing_annotations() wrote to caller-provided paths directly. These
tests mirror the export_file / render_view traversal + outside-root tests and
assert the same central rejection, plus a happy-path write to a temp root.
"""
import json
import os
import sys

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.render_drawing import render_drawing
from build123d_mcp.tools.save_drawing_annotations import save_drawing_annotations
from build123d_mcp.tools.script import script

_OUTSIDE_ROOT_PATH = (
    r"C:\Windows\System32\drivers\etc\hosts"
    if sys.platform == "win32"
    else "/etc/passwd"
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
