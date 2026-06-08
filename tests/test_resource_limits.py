"""Resource-limit guards for model-supplied file inputs (issue #189).

A model-callable server should reject obviously excessive inputs *before* the
expensive parse/import/rasterise, rather than letting the work run until the
exec timeout kills the worker and destroys session state. These tests prove:

  * render_drawing() rejects an extreme raster width (huge bitmap allocation)
    and an oversized SVG file.
  * import_cad_file(), inspect_drawing(), and lint_drawing() reject oversized
    inputs via the shared ``check_input_size`` preflight.
  * inspect_drawing() and lint_drawing() reject an XML entity-expansion
    ("billion laughs") SVG via the hardened defusedxml parser — stdlib
    ElementTree expands such entities and is memory-exhaustible.

Size limits are patched small (instead of writing multi-MB files) so the tests
stay fast; the real defaults are generous.
"""

import json

import pytest

import build123d_mcp.tools._paths as _paths
from build123d_mcp.session import Session
from build123d_mcp.tools.import_step import import_cad_file
from build123d_mcp.tools.inspect_drawing import inspect_drawing
from build123d_mcp.tools.lint_drawing import lint_drawing
from build123d_mcp.tools.render_drawing import render_drawing

_VALID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm">\n'
    '  <rect x="5" y="5" width="40" height="40" fill="green"/>\n'
    "</svg>"
)

# Billion-laughs payload: a tiny file whose entities expand exponentially. The
# hardened parser must reject it at the first entity definition, never expand.
_BOMB_SVG = (
    '<?xml version="1.0"?>\n'
    "<!DOCTYPE lolz [\n"
    ' <!ENTITY lol "lol">\n'
    ' <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
    ' <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
    "]>\n"
    '<svg xmlns="http://www.w3.org/2000/svg"><text>&lol2;</text></svg>'
)


@pytest.fixture
def session():
    return Session()


def _write(tmp_path, name, text) -> str:
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# --- raster width ----------------------------------------------------------


def test_render_drawing_rejects_extreme_width(tmp_path):
    svg = _write(tmp_path, "drawing.svg", _VALID_SVG)
    with pytest.raises(ValueError, match="raster width"):
        render_drawing(svg, width=_paths.MAX_RASTER_WIDTH + 1)


def test_render_drawing_allows_normal_width(tmp_path):
    # A sane width still rasterises (guards against an over-tight limit).
    result = render_drawing(_write(tmp_path, "drawing.svg", _VALID_SVG), width=200)
    assert result.get("error") is None
    assert result["size_bytes"] > 0


# --- file size preflight ---------------------------------------------------


def test_render_drawing_rejects_oversized_svg(tmp_path, monkeypatch):
    monkeypatch.setattr(_paths, "MAX_SVG_BYTES", 10)
    with pytest.raises(ValueError, match="exceeding"):
        render_drawing(_write(tmp_path, "drawing.svg", _VALID_SVG))


def test_inspect_drawing_rejects_oversized_svg(session, tmp_path, monkeypatch):
    monkeypatch.setattr(_paths, "MAX_SVG_BYTES", 10)
    with pytest.raises(ValueError, match="exceeding"):
        inspect_drawing(session, svg_path=_write(tmp_path, "drawing.svg", _VALID_SVG))


def test_lint_drawing_rejects_oversized_svg(session, tmp_path, monkeypatch):
    monkeypatch.setattr(_paths, "MAX_SVG_BYTES", 10)
    with pytest.raises(ValueError, match="exceeding"):
        lint_drawing(session, svg_path=_write(tmp_path, "drawing.svg", _VALID_SVG))


def test_import_cad_file_rejects_oversized_file(session, tmp_path, monkeypatch):
    # Size is checked before the OCC import, so the file need not be valid CAD.
    monkeypatch.setattr(_paths, "MAX_CAD_BYTES", 10)
    big = tmp_path / "big.stl"
    big.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="exceeding"):
        import_cad_file(session, str(big))


# --- XML entity-expansion (billion laughs) ---------------------------------


def test_inspect_drawing_rejects_xml_bomb(session, tmp_path):
    result = json.loads(inspect_drawing(session, svg_path=_write(tmp_path, "bomb.svg", _BOMB_SVG)))
    assert "XML hardening" in result.get("error", "")


def test_lint_drawing_rejects_xml_bomb(session, tmp_path):
    violations = json.loads(
        lint_drawing(session, svg_path=_write(tmp_path, "bomb.svg", _BOMB_SVG))
    )["violations"]
    assert any(v["check"] == "svg_parse" for v in violations)


def test_lint_drawing_accepts_normal_svg(session, tmp_path):
    # The hardened parser must not false-positive on an ordinary SVG.
    violations = json.loads(lint_drawing(session, svg_path=_write(tmp_path, "ok.svg", _VALID_SVG)))[
        "violations"
    ]
    assert not any(v["check"] == "svg_parse" for v in violations)
