"""Tests for the #108 pass-1 drawing-side tools:
view_axes, lint_drawing, render_drawing, inspect_drawing(svg_path=...).
"""

import json
import os

import pytest

from build123d_mcp.session import Session


@pytest.fixture
def session():
    return Session()


# ---------------------------------------------------------------------------
# view_axes
# ---------------------------------------------------------------------------


class TestViewAxes:
    def test_top_view_identity_mapping(self):
        from build123d_mcp.tools.view_axes import view_axes

        result = json.loads(view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0)))
        assert result["world_X"][0] == "page_X"
        assert result["world_X"][1] == 1.0
        assert result["world_Y"][0] == "page_Y"
        assert result["world_Y"][1] == 1.0

    def test_bottom_view_flips_world_x(self):
        """The bottom-view axis swap that the gramel shank drawing hit."""
        from build123d_mcp.tools.view_axes import view_axes

        result = json.loads(view_axes((0, 0, -100), (0, 1, 0), (0, 0, 0)))
        assert result["world_X"][0] == "page_X"
        assert result["world_X"][1] == -1.0

    def test_look_at_offset_zero_at_origin(self):
        from build123d_mcp.tools.view_axes import view_axes

        result = json.loads(view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0)))
        assert "look_at_offset" in result
        assert result["look_at_offset"]["page_X"] == 0.0
        assert result["look_at_offset"]["page_Y"] == 0.0

    def test_look_at_offset_front_view_z_centroid(self):
        """Front view with part centroid at z=-4.65: page_Y offset should be -4.65."""
        from build123d_mcp.tools.view_axes import view_axes

        # camera on -Y axis, up=+Z → world_Z maps to page_Y
        result = json.loads(view_axes((0, -100, 0), (0, 0, 1), (0, 0, -4.65)))
        assert result["look_at_offset"]["page_Y"] == -4.65

    def test_look_at_offset_top_view_y_centroid(self):
        """Top view (camera +Z, up +Y): world_Y → page_Y. look_at y=3.0 offset captured."""
        from build123d_mcp.tools.view_axes import view_axes

        # Camera on +Z axis, look_at Y=3 → world_Y maps to page_Y → offset = 3.0
        result = json.loads(view_axes((0, 0, 100), (0, 1, 0), (0, 3.0, 0)))
        assert result["look_at_offset"]["page_Y"] == 3.0
        assert result["look_at_offset"]["page_X"] == 0.0

    def test_helper_snippet_present_and_non_empty(self):
        from build123d_mcp.tools.view_axes import view_axes

        result = json.loads(view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0)))
        assert "helper_snippet" in result
        assert "VIEW_X" in result["helper_snippet"]
        assert "SCALE" in result["helper_snippet"]

    def test_helper_snippet_includes_offset_when_nonzero(self):
        """When look_at has a non-zero component, the snippet incorporates the offset."""
        from build123d_mcp.tools.view_axes import view_axes

        # Top view: world_X → page_X, world_Y → page_Y, look_at x=3.0
        result = json.loads(view_axes((0, 0, 100), (0, 1, 0), (3.0, 0, 0)))
        assert "3.0" in result["helper_snippet"]

    def test_helper_snippet_clean_when_offset_zero(self):
        """No offset terms when look_at is at origin."""
        from build123d_mcp.tools.view_axes import view_axes

        result = json.loads(view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0)))
        # Clean form: def X(x): return VIEW_X + x * SCALE  (no subtraction)
        snippet = result["helper_snippet"]
        assert "- 0" not in snippet
        assert "+ 0" not in snippet


# ---------------------------------------------------------------------------
# lint_drawing (session mode)
# ---------------------------------------------------------------------------


class TestLintDrawingSession:
    def _run(self, session):
        from build123d_mcp.tools.lint_drawing import lint_drawing

        return json.loads(lint_drawing(session))

    def test_empty_session_no_violations(self, session):
        assert self._run(session)["violations"] == []

    def test_flags_label_divergence(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
w = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="35")  # label wrong: real is 20
annotate(w, "wrong_dim")
""")
        out = self._run(session)
        assert any(v["check"] == "label_vs_measured" for v in out["violations"])
        v = next(v for v in out["violations"] if v["check"] == "label_vs_measured")
        assert v["object"] == "wrong_dim"
        assert v["severity"] == "error"

    def test_clean_session_no_violations(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
w = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "good_dim")
""")
        assert self._run(session)["violations"] == []

    def test_flags_annotation_overlap(self, session):
        # Two dims at the same offset on the same segment will collide.
        session.execute("""
from build123d import *
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
a = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
b = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(a, "dim_a")
annotate(b, "dim_b")
""")
        out = self._run(session)
        assert any(v["check"] == "annotation_overlap" for v in out["violations"])

    def test_surfaces_geometry_precise_interference(self, session):
        # find_interferences checks are now exposed through the MCP tool: a
        # stacked dim whose extension line spears a neighbour's label.
        session.execute("""
from build123d import *
from build123d_drafting import place_dims
draft = Draft(font_size=2.5, decimal_precision=1)
dims = place_dims([
    ((-18, -10, 0), (18, -10, 0), "below", "36"),
    ((-18, -10, 0), (0, -10, 0), "below", "18"),
], draft, base_distance=6)
for i, d in enumerate(dims):
    annotate(d, f"dim_{i}")
""")
        checks = {v["check"] for v in self._run(session)["violations"]}
        assert "line_pierces_label" in checks

    def test_clean_leader_no_false_violation(self, session):
        # The Leader check is delegated to the helpers via the stored label_bbox.
        # A correctly-built Leader (line stops before the label) must not flag.
        session.execute("""
from build123d import *
from build123d_drafting import Leader
draft = Draft(font_size=2.5, decimal_precision=1)
ld = Leader((0, 0, 0), (20, 12, 0), "Ø5 H7", draft)
annotate(ld, "callout")
""")
        assert self._run(session)["violations"] == []

    def test_no_false_overlap_for_separated_dims(self, session):
        # Dims stacked at distinct offsets must not be flagged.
        session.execute("""
from build123d import *
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
inner = Dimension((-10, 0, 0), (10, 0, 0), "above",  8, draft, label="20")
outer = Dimension((-10, 0, 0), (10, 0, 0), "above", 18, draft, label="20")
annotate(inner, "inner_dim")
annotate(outer, "outer_dim")
""")
        out = self._run(session)
        overlap_violations = [v for v in out["violations"] if v["check"] == "annotation_overlap"]
        assert overlap_violations == []

    def test_flags_annotation_out_of_bounds(self, session):
        session.execute("""
from build123d import *
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
# Dim placed far outside a tiny 50x50 mm page
d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(d, "offpage_dim")
set_page(50, 50, margin=5)
""")
        out = self._run(session)
        assert any(v["check"] == "annotation_out_of_bounds" for v in out["violations"])

    def test_no_out_of_bounds_when_within_page(self, session):
        session.execute("""
from build123d import *
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
# Dim centred at (100, 50) — well within A4 landscape page (5..292, 5..205)
d = Dimension((90, 50, 0), (110, 50, 0), "above", 8, draft, label="20")
annotate(d, "dim")
set_page(297, 210, margin=5)
""")
        out = self._run(session)
        bounds_violations = [
            v for v in out["violations"] if v["check"] == "annotation_out_of_bounds"
        ]
        assert bounds_violations == []

    def test_no_page_bounds_check_without_set_page(self, session):
        # Without set_page(), out-of-bounds check must not fire.
        session.execute("""
from build123d import *
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(d, "dim")
""")
        out = self._run(session)
        bounds_violations = [
            v for v in out["violations"] if v["check"] == "annotation_out_of_bounds"
        ]
        assert bounds_violations == []

    def test_set_page_resets_on_session_reset(self, session):
        session.execute("set_page(297, 210)")
        assert session.drawing_page is not None
        session.reset()
        assert session.drawing_page is None

    # --- drawing_scale (#147): N:1 drawings without false label_vs_measured ---

    def _build_scaled(self, session, label):
        # 20 mm of geometry; the label carries whatever string we pass.
        session.execute(f"""
from build123d import *
from build123d_drafting import Dimension
draft = Draft(font_size=2.5, decimal_precision=1)
w = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="{label}")
annotate(w, "scaled_dim")
""")

    def _run_scaled(self, session, drawing_scale):
        from build123d_mcp.tools.lint_drawing import lint_drawing

        return json.loads(lint_drawing(session, drawing_scale=drawing_scale))

    def test_scaled_dim_with_real_label_is_clean(self, session):
        # 20 mm drawn at 2:1 represents a real 10 mm feature; label "10" is correct.
        self._build_scaled(session, "10")
        out = self._run_scaled(session, 2.0)
        assert not any(v["check"] == "label_vs_measured" for v in out["violations"])

    def test_same_drawing_flagged_at_1to1(self, session):
        # Without the scale (default 1.0), label "10" vs measured 20 is flagged —
        # proves the scale param is what clears it, not a loosened check.
        self._build_scaled(session, "10")
        out = self._run_scaled(session, 1.0)
        assert any(v["check"] == "label_vs_measured" for v in out["violations"])

    def test_scaled_dim_with_unscaled_label_flagged(self, session):
        # Labelling the drawn 20 mm instead of the real 10 mm is still caught at 2:1.
        self._build_scaled(session, "20")
        out = self._run_scaled(session, 2.0)
        assert any(v["check"] == "label_vs_measured" for v in out["violations"])


# ---------------------------------------------------------------------------
# lint_drawing (SVG mode)
# ---------------------------------------------------------------------------


class TestLintDrawingSvg:
    def test_flags_native_text(self, tmp_path):
        # build123d never emits <text> (it renders glyph paths), so any <text>
        # means native SVG text that won't DXF-export — flagged regardless of fill.
        from build123d_mcp.tools.lint_drawing import lint_drawing

        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">
  <g id="dims" fill="none">
    <text id="bad_label" x="10" y="20">40</text>
  </g>
</svg>"""
        p = tmp_path / "bad.svg"
        p.write_text(svg)
        out = json.loads(lint_drawing(None, str(p)))
        assert any(v["check"] == "native_svg_text" for v in out["violations"])

    def test_clean_svg_no_violations(self, tmp_path):
        # A real build123d export uses <path> glyphs, not <text> — clean.
        from build123d_mcp.tools.lint_drawing import lint_drawing

        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">
  <g id="dims" fill="black">
    <path id="glyph" d="M10,10 L20,10 L20,20 Z"/>
  </g>
</svg>"""
        p = tmp_path / "good.svg"
        p.write_text(svg)
        out = json.loads(lint_drawing(None, str(p)))
        assert out["violations"] == []

    def test_missing_file_returns_error(self, tmp_path):
        from build123d_mcp.tools.lint_drawing import lint_drawing

        out = json.loads(lint_drawing(None, str(tmp_path / "does_not_exist.svg")))
        assert any(v["check"] == "svg_parse" for v in out["violations"])


# ---------------------------------------------------------------------------
# inspect_drawing(svg_path=...)
# ---------------------------------------------------------------------------


class TestInspectDrawingSvg:
    def test_reports_page_layers_text(self, tmp_path):
        from build123d_mcp.tools.inspect_drawing import inspect_drawing

        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" viewBox="0 0 297 210">
  <g id="part" fill="black">
    <path d="M10,10 L100,10"/>
  </g>
  <g id="dims" fill="blue">
    <text id="w_label" x="50" y="40">40</text>
  </g>
</svg>"""
        p = tmp_path / "sheet.svg"
        p.write_text(svg)
        out = json.loads(inspect_drawing(None, "", str(p)))
        assert out["mode"] == "svg"
        assert out["page"]["width"] == 297.0
        assert out["page"]["height"] == 210.0
        layer_ids = [g["id"] for g in out["layers"]]
        assert "part" in layer_ids
        assert "dims" in layer_ids
        text_ids = [t["id"] for t in out["text"]]
        assert "w_label" in text_ids
        assert out["counts"]["text"] == 1
        assert out["counts"]["path"] == 1

    def test_missing_file_returns_error(self, tmp_path):
        from build123d_mcp.tools.inspect_drawing import inspect_drawing

        out = json.loads(inspect_drawing(None, "", str(tmp_path / "missing.svg")))
        assert "error" in out


# ---------------------------------------------------------------------------
# render_drawing
# ---------------------------------------------------------------------------


class TestRenderDrawing:
    def test_rasterises_simple_svg(self, tmp_path):
        from build123d_mcp.tools.render_drawing import render_drawing

        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50">
  <rect x="10" y="10" width="80" height="30" fill="blue"/>
</svg>"""
        p = tmp_path / "tile.svg"
        p.write_text(svg)
        result = render_drawing(str(p), width=400)
        assert "error" not in result
        assert "png" in result
        # PNG magic bytes
        assert result["png"][:8] == b"\x89PNG\r\n\x1a\n"
        assert result["width"] == 400

    def test_missing_file_error(self, tmp_path):
        from build123d_mcp.tools.render_drawing import render_drawing

        result = render_drawing(str(tmp_path / "missing.svg"))
        assert "error" in result

    def test_save_to_writes_file(self, tmp_path):
        from build123d_mcp.tools.render_drawing import render_drawing

        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm">
  <circle cx="25" cy="25" r="10" fill="red"/>
</svg>"""
        src = tmp_path / "circle.svg"
        src.write_text(svg)
        out_path = tmp_path / "out.png"
        result = render_drawing(str(src), width=200, save_to=str(out_path))
        assert out_path.exists()
        # png_path is the policy-resolved realpath (may differ from str(out_path)
        # under a symlinked TMPDIR, e.g. macOS /var -> /private/var).
        assert os.path.samefile(result["png_path"], out_path)
        assert out_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# End-to-end through WorkerSession — proves IPC routing for all four tools
# ---------------------------------------------------------------------------


class TestDrawingToolsViaWorker:
    def test_view_axes_through_worker(self):
        from build123d_mcp.worker import WorkerSession

        ws = WorkerSession(exec_timeout=30)
        try:
            result = json.loads(ws.view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0)))
            assert result["world_X"][0] == "page_X"
        finally:
            ws._kill_worker()

    def test_lint_drawing_through_worker(self):
        from build123d_mcp.worker import WorkerSession

        ws = WorkerSession(exec_timeout=30)
        try:
            result = json.loads(ws.lint_drawing())
            assert "violations" in result
        finally:
            ws._kill_worker()

    def test_lint_drawing_scale_through_worker(self):
        # #147: drawing_scale must survive the IPC round-trip (client -> dispatch
        # -> tool). A 20 mm dim labelled "10" is clean at 2:1, flagged at 1:1.
        from build123d_mcp.worker import WorkerSession

        ws = WorkerSession(exec_timeout=30)
        try:
            ws.execute(
                "from build123d import *\n"
                "from build123d_drafting import Dimension\n"
                "draft = Draft(font_size=2.5, decimal_precision=1)\n"
                "w = Dimension((-10,0,0),(10,0,0),'above',8,draft,label='10')\n"
                "annotate(w, 'd')\n"
            )
            scaled = json.loads(ws.lint_drawing(drawing_scale=2.0))
            assert not any(v["check"] == "label_vs_measured" for v in scaled["violations"])
            unscaled = json.loads(ws.lint_drawing())
            assert any(v["check"] == "label_vs_measured" for v in unscaled["violations"])
        finally:
            ws._kill_worker()

    def test_render_drawing_through_worker(self, tmp_path):
        from build123d_mcp.worker import WorkerSession

        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm">
  <rect x="5" y="5" width="40" height="40" fill="green"/>
</svg>"""
        p = tmp_path / "g.svg"
        p.write_text(svg)
        ws = WorkerSession(exec_timeout=30)
        try:
            result = ws.render_drawing(str(p), width=200)
            assert "error" not in result
            assert result["png"][:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            ws._kill_worker()


# ---------------------------------------------------------------------------
# lint_drawing SVG mode — sidecar annotation checks (#118)
# ---------------------------------------------------------------------------


class TestLintDrawingSvgSidecar:
    def _write_svg(self, path):
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"/>')

    def test_sidecar_label_mismatch_flagged(self, tmp_path):
        import json as _json

        from build123d_mcp.tools.lint_drawing import lint_drawing

        svg = tmp_path / "drawing.svg"
        self._write_svg(svg)
        sidecar = tmp_path / "drawing.dims.json"
        # 30 mm path labelled "99" — clear axis-swap mismatch
        sidecar.write_text(
            _json.dumps(
                {
                    "axis_swap_bug": {
                        "type": "ExtensionLine",
                        "label_str": "99",
                        "measured_length": 30.0,
                    }
                }
            )
        )
        out = json.loads(lint_drawing(None, str(svg)))
        assert any(v["check"] == "label_vs_measured" for v in out["violations"])
        assert any("99" in v["message"] for v in out["violations"])

    def test_sidecar_correct_label_no_violation(self, tmp_path):
        import json as _json

        from build123d_mcp.tools.lint_drawing import lint_drawing

        svg = tmp_path / "drawing.svg"
        self._write_svg(svg)
        sidecar = tmp_path / "drawing.dims.json"
        sidecar.write_text(
            _json.dumps(
                {"width": {"type": "ExtensionLine", "label_str": "20", "measured_length": 20.0}}
            )
        )
        out = json.loads(lint_drawing(None, str(svg)))
        label_violations = [v for v in out["violations"] if v["check"] == "label_vs_measured"]
        assert label_violations == []

    def test_no_sidecar_no_annotation_violations(self, tmp_path):
        from build123d_mcp.tools.lint_drawing import lint_drawing

        svg = tmp_path / "drawing.svg"
        self._write_svg(svg)
        out = json.loads(lint_drawing(None, str(svg)))
        annotation_violations = [v for v in out["violations"] if v["check"] == "label_vs_measured"]
        assert annotation_violations == []
