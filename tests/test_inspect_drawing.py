"""Tests for inspect_drawing tool and annotate() session helper."""
import json
import pytest

from build123d_mcp.session import Session


@pytest.fixture
def session():
    return Session()


# ---------------------------------------------------------------------------
# annotate() helper
# ---------------------------------------------------------------------------

class TestAnnotate:
    def test_annotate_registers_shape(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        assert "width" in session.objects

    def test_annotate_stores_metadata(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        ann = session.drawing_annotations.get("width")
        assert ann is not None
        assert ann["label_str"] == "20"
        assert abs(ann["measured_length"] - 20.0) < 0.01

    def test_annotate_leader_stores_tip_elbow(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import leader
draft = Draft(font_size=2.5, decimal_precision=1)
lea = leader((5, 5, 0), (20, 12, 0), "Ra 1.6", draft)
annotate(lea, "ra_mark")
""")
        ann = session.drawing_annotations.get("ra_mark")
        assert ann is not None
        assert ann["label_str"] == "Ra 1.6"
        assert "tip" in ann
        assert "elbow" in ann

    def test_annotate_clears_on_reset(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        session.reset()
        assert session.drawing_annotations == {}

    def test_annotate_rolled_back_on_error(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        # Now execute bad code that should roll back
        session.execute("raise ValueError('intentional error')")
        # annotation from first execute must still be there (only the bad exec rolls back)
        assert "width" in session.drawing_annotations

    # -----------------------------------------------------------------------
    # Vanilla build123d primitives — annotate() must extract measured_length
    # from .dimension and accept an explicit label= kwarg (issue #107).
    # The old behaviour was: empty metadata block, label and length lost.
    # -----------------------------------------------------------------------

    def test_annotate_vanilla_extension_line_captures_dimension(self, session):
        session.execute("""
from build123d import ExtensionLine, Draft
draft = Draft(font_size=2.5, decimal_precision=1, arrow_length=1.0, line_width=0.1)
w = ExtensionLine(border=[(-20, 10, 0), (20, 10, 0)], offset=6, draft=draft, label="40")
annotate(w, "width_dim", label="40")
""")
        ann = session.drawing_annotations.get("width_dim")
        assert ann is not None
        assert ann["label_str"] == "40"
        assert abs(ann["measured_length"] - 40.0) < 0.01
        assert ann["type"] == "ExtensionLine"

    def test_annotate_vanilla_dimension_line_captures_dimension(self, session):
        session.execute("""
from build123d import DimensionLine, Draft
draft = Draft(font_size=2.5, decimal_precision=1, arrow_length=1.0, line_width=0.1)
d = DimensionLine(path=[(0, 0, 0), (25, 0, 0)], draft=draft, label="25")
annotate(d, "len_dim", label="25")
""")
        ann = session.drawing_annotations.get("len_dim")
        assert ann is not None
        assert ann["label_str"] == "25"
        assert abs(ann["measured_length"] - 25.0) < 0.01
        assert ann["type"] == "DimensionLine"

    def test_annotate_vanilla_extension_line_without_label_has_no_label_str(self, session):
        """Without an explicit label= kwarg, label_str is absent.
        build123d doesn't expose the constructor label after construction, so
        we can't distinguish a correctly-labelled dim from an axis-swap bug.
        Leaving label_str absent means lint skips the check rather than
        producing a false negative."""
        session.execute("""
from build123d import ExtensionLine, Draft
draft = Draft(font_size=2.5, decimal_precision=1, arrow_length=1.0, line_width=0.1)
w = ExtensionLine(border=[(-15, 0, 0), (15, 0, 0)], offset=5, draft=draft)
annotate(w, "no_label_dim")
""")
        ann = session.drawing_annotations.get("no_label_dim")
        assert ann is not None
        assert "measured_length" in ann
        assert abs(ann["measured_length"] - 30.0) < 0.01
        assert "label_str" not in ann

    def test_annotate_vanilla_el_with_wrong_label_fires_lint(self, session):
        """Regression for #119: annotate(el, name, label="99") on a 30mm path
        must produce a lint violation, not a false negative."""
        session.execute("""
from build123d import ExtensionLine, Draft
draft = Draft(font_size=2.5, decimal_precision=1, arrow_length=1.0, line_width=0.1)
bad = ExtensionLine(border=[(-15, -30, 0), (15, -30, 0)], offset=-6, draft=draft, label="99")
annotate(bad, "axis_swap_bug", label="99")
""")
        from build123d_mcp.tools.lint_drawing import lint_drawing
        import json
        out = json.loads(lint_drawing(session))
        assert any(v["check"] == "label_vs_measured" for v in out["violations"]), (
            "lint must flag 99 vs 30mm mismatch when label= is passed explicitly"
        )

    def test_annotate_vanilla_el_without_label_no_false_negative(self, session):
        """Regression for #119: annotate(el, name) without label= on a 30mm
        path labelled '99' must NOT produce a false-clean lint result."""
        session.execute("""
from build123d import ExtensionLine, Draft
draft = Draft(font_size=2.5, decimal_precision=1, arrow_length=1.0, line_width=0.1)
bad = ExtensionLine(border=[(-15, -30, 0), (15, -30, 0)], offset=-6, draft=draft, label="99")
annotate(bad, "axis_swap_bug")   # no label= kwarg
""")
        ann = session.drawing_annotations.get("axis_swap_bug")
        # label_str must be absent — lint cannot fire a false negative
        assert "label_str" not in ann

    def test_annotate_label_kwarg_ignored_when_result_has_label_str(self, session):
        """For DimResult (which already exposes label_str), an explicit
        label= kwarg should not overwrite the helper's value."""
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width", label="WRONG")
""")
        ann = session.drawing_annotations.get("width")
        assert ann is not None
        assert ann["label_str"] == "20"


# ---------------------------------------------------------------------------
# inspect_drawing tool
# ---------------------------------------------------------------------------

class TestInspectDrawing:
    def _run(self, session, objects=""):
        from build123d_mcp.tools.inspect_drawing import inspect_drawing
        return json.loads(inspect_drawing(session, objects))

    def test_empty_session_returns_error(self, session):
        result = self._run(session)
        assert "error" in result

    def test_unknown_object_returns_error(self, session):
        session.execute("from build123d import *; show(Box(10,10,10), 'box')")
        result = self._run(session, "nonexistent")
        assert "error" in result

    def test_reports_bbox_for_plain_shape(self, session):
        session.execute("""
from build123d import *
plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))
visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible)), "part_view")
""")
        result = self._run(session)
        assert "part_view" in result["objects"]
        bb = result["objects"]["part_view"]["bbox"]
        assert bb is not None
        assert "min_x" in bb and "max_x" in bb

    def test_annotation_included_for_annotated_dim(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        result = self._run(session)
        ann = result["objects"]["width"]["annotation"]
        assert ann is not None
        assert ann["label_str"] == "20"
        assert abs(ann["measured_length"] - 20.0) < 0.01

    def test_annotation_null_for_plain_show(self, session):
        session.execute("""
from build123d import *
show(Box(10, 10, 10), "box")
""")
        result = self._run(session)
        assert result["objects"]["box"]["annotation"] is None

    def test_drawing_bbox_covers_all_objects(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
h = dim_linear((20, -10, 0), (20, 10, 0), "right", 8, draft, label="20")
annotate(w, "width")
annotate(h, "height")
""")
        result = self._run(session)
        db = result["drawing_bbox"]
        assert db is not None
        assert db["min_x"] < 0 and db["max_x"] > 0

    def test_objects_filter_works(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
h = dim_linear((0, -10, 0), (0, 10, 0), "right", 8, draft, label="20")
annotate(w, "width")
annotate(h, "height")
""")
        result = self._run(session, "width")
        assert "width" in result["objects"]
        assert "height" not in result["objects"]

    def test_lint_flags_label_divergence(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
# Label says 35 but segment is 20 mm — should be flagged
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="35")
annotate(w, "wrong_dim")
""")
        result = self._run(session)
        assert len(result["lint"]) > 0
        assert any("35" in w or "differs" in w for w in result["lint"])

    def test_lint_clean_for_correct_label(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        result = self._run(session)
        assert result["lint"] == []


# ---------------------------------------------------------------------------
# Regression: inspect_drawing must work through WorkerSession (issue #105).
# The bare-Session tests above can't catch a routing bug because the in-process
# Session has .objects/.drawing_annotations directly; the production server
# uses WorkerSession, a parent-side IPC proxy whose state lives in a subprocess.
# ---------------------------------------------------------------------------

class TestInspectDrawingViaWorker:
    def test_inspect_drawing_through_worker(self):
        from build123d_mcp.worker import WorkerSession

        ws = WorkerSession(exec_timeout=30)
        try:
            ws.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
            payload = json.loads(ws.inspect_drawing())
            assert "error" not in payload, payload
            assert "width" in payload["objects"]
            ann = payload["objects"]["width"]["annotation"]
            assert ann is not None
            assert ann["label_str"] == "20"
        finally:
            ws._kill_worker()


# ---------------------------------------------------------------------------
# annotate() label auto-derivation (#115)
# ---------------------------------------------------------------------------

class TestAnnotateLabelFallback:
    def test_vanilla_extension_line_without_kwarg_has_no_label_str(self, session):
        """Without label= kwarg, label_str is absent — not derived from measured_length.
        Auto-derive was removed because it caused false-negative lint results (#119)."""
        session.execute("""
from build123d import *
from build123d import Draft, ExtensionLine, Mode
draft = Draft(font_size=2.5, decimal_precision=1)
el = ExtensionLine(border=[(-10, 0, 0), (10, 0, 0)], offset=8, draft=draft, label="20", mode=Mode.PRIVATE)
annotate(el, "dim")
""")
        ann = session.drawing_annotations.get("dim")
        assert ann is not None
        assert "label_str" not in ann
        assert abs(ann["measured_length"] - 20.0) < 0.01

    def test_explicit_label_kwarg_wins(self, session):
        session.execute("""
from build123d import *
from build123d import Draft, ExtensionLine, Mode
draft = Draft(font_size=2.5, decimal_precision=1)
el = ExtensionLine(border=[(-10, 0, 0), (10, 0, 0)], offset=8, draft=draft, label="40", mode=Mode.PRIVATE)
annotate(el, "dim", label="40")
""")
        ann = session.drawing_annotations.get("dim")
        assert ann is not None
        assert ann["label_str"] == "40"

    def test_dim_result_label_preserved_exactly(self, session):
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="custom")
annotate(w, "width")
""")
        ann = session.drawing_annotations.get("width")
        assert ann is not None
        assert ann["label_str"] == "custom"


# ---------------------------------------------------------------------------
# save_drawing_annotations / sidecar (#116)
# ---------------------------------------------------------------------------

class TestSaveDrawingAnnotations:
    def _run(self, session, objects=""):
        from build123d_mcp.tools.inspect_drawing import inspect_drawing
        return json.loads(inspect_drawing(session, objects))

    def test_save_writes_dims_json(self, session, tmp_path):
        from build123d_mcp.tools.save_drawing_annotations import save_drawing_annotations
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        svg_path = str(tmp_path / "drawing.svg")
        result = save_drawing_annotations(session, svg_path)
        sidecar = tmp_path / "drawing.dims.json"
        assert sidecar.exists()
        assert "1 annotation" in result

    def test_sidecar_loaded_by_inspect_svg_mode(self, session, tmp_path):
        from build123d_mcp.tools.save_drawing_annotations import save_drawing_annotations
        from build123d_mcp.tools.inspect_drawing import _inspect_svg
        session.execute("""
from build123d import *
from build123d import Draft
from build123d_drafting import dim_linear
draft = Draft(font_size=2.5, decimal_precision=1)
w = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
annotate(w, "width")
""")
        svg_path = str(tmp_path / "drawing.svg")
        # Write a minimal SVG so the file exists
        (tmp_path / "drawing.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"/>'
        )
        save_drawing_annotations(session, svg_path)

        result = json.loads(_inspect_svg(svg_path))
        assert result["mode"] == "svg"
        assert "width" in result["annotations"]
        assert result["annotations"]["width"]["label_str"] == "20"
        assert "loaded from sidecar" in result["annotations_note"]

    def test_no_sidecar_gives_guidance_note(self, tmp_path):
        from build123d_mcp.tools.inspect_drawing import _inspect_svg
        svg_path = tmp_path / "bare.svg"
        svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"/>')
        result = json.loads(_inspect_svg(str(svg_path)))
        assert result["annotations"] == {}
        assert "save_drawing_annotations" in result["annotations_note"]
