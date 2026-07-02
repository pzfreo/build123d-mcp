"""
Outcome-focused tests: verify what the server actually produces,
not just that functions return without error.
"""

import asyncio
import base64
import json
import os
import struct
import sys

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.export import export_file
from build123d_mcp.tools.measure import measure
from build123d_mcp.tools.render import render_view

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


# ---------------------------------------------------------------------------
# Multi-step workflow: does state build up correctly across execute calls?
# ---------------------------------------------------------------------------


def test_incremental_construction_extends_geometry(session):
    """Second execute can reference and extend geometry from the first."""
    execute_code(session, "b = Box(20, 20, 20)")
    execute_code(session, "result = b + Cylinder(5, 30)")
    data = json.loads(measure(session))
    assert data["bbox"]["zsize"] > 20  # cylinder taller than box


def test_boolean_subtraction_removes_material(session):
    """Cutting a cylinder from a box reduces volume."""
    execute_code(session, "box = Box(10, 10, 10)")
    full_volume = session.current_shape.volume
    execute_code(session, "result = box - Cylinder(3, 12)")
    assert session.current_shape.volume < full_volume


def test_create_measure_export_round_trip(session, tmp_path, monkeypatch):
    """Create a known shape, verify its dimensions, then export it."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(30, 20, 10)")
    data = json.loads(measure(session))
    assert abs(data["bbox"]["xsize"] - 30) < 0.01
    assert abs(data["bbox"]["ysize"] - 20) < 0.01
    assert abs(data["bbox"]["zsize"] - 10) < 0.01

    export_file(session, "out", "step")
    # A real STEP file for a simple box is several kilobytes
    assert os.path.getsize("out.step") > 1000


def test_multi_format_export_produces_both_files(session, tmp_path, monkeypatch):
    """Exporting step,stl in one call writes both files with real content."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(20, 20, 20)")
    result = export_file(session, "part", "step,stl")
    step_size = os.path.getsize("part.step")
    stl_size = os.path.getsize("part.stl")
    # STEP files are text-based and larger; STL binary is compact but non-zero
    assert step_size > 1000
    assert stl_size > 0
    # Both paths reported in the return message
    assert ".step" in result
    assert ".stl" in result


def test_multi_format_export_named_object(session, tmp_path, monkeypatch):
    """Multi-format export targets the named object, not current_shape."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(5, 5, 5)\nshow(Box(40, 40, 40), 'big')")
    export_file(session, "big", "step,stl", "big")
    # The big box STEP file is distinctly larger than a 5mm box would produce
    assert os.path.getsize("big.step") > 1000
    assert os.path.getsize("big.stl") > 0


def test_reset_discards_previous_geometry(session):
    """After reset, old geometry is gone and new geometry starts from scratch."""
    execute_code(session, "result = Box(100, 100, 100)")
    session.reset()
    session.execute("from build123d import *")
    execute_code(session, "result = Box(5, 5, 5)")
    data = json.loads(measure(session))
    assert abs(data["bbox"]["xsize"] - 5) < 0.01


def test_render_changes_when_model_changes(session):
    """Rendering a different shape produces a different image.

    Note: the camera autofits to the bounds, so a uniform scale change
    (Box(10) → Box(50)) renders identically. This test uses shapes that
    differ in proportion, which is what actually changes the rendered pixels.
    """
    execute_code(session, "result = Box(10, 10, 10)")
    png_cube = render_view(session, "iso")["png"]
    execute_code(session, "result = Box(10, 30, 50)")
    png_slab = render_view(session, "iso")["png"]
    assert png_cube[:8] == PNG_MAGIC
    assert png_slab[:8] == PNG_MAGIC
    assert png_cube != png_slab


def test_error_in_execute_preserves_current_shape(session):
    """A failed execute does not wipe the current shape."""
    execute_code(session, "result = Box(10, 10, 10)")
    shape_before = session.current_shape
    execute_code(session, "this_will_fail(")  # SyntaxError
    assert session.current_shape is shape_before


# ---------------------------------------------------------------------------
# Security: injection resistance
# ---------------------------------------------------------------------------


def test_shell_injection_attempt_blocked(session):
    """A prompt-injection payload trying to run a shell command is rejected."""
    execute_code(session, "result = Box(10, 10, 10)")
    shape_before = session.current_shape
    result = execute_code(session, "import subprocess; subprocess.run(['id'])")
    assert "not allowed" in result.lower() or "SecurityError" in result
    # Geometry is intact
    assert session.current_shape is shape_before


def test_filesystem_read_attempt_blocked(session):
    """Attempting to read a file via open() is rejected."""
    result = execute_code(session, "data = open('/etc/passwd').read()")
    assert "not allowed" in result.lower() or "Error" in result


def test_network_access_attempt_blocked(session):
    """Attempting to open a network socket is rejected."""
    result = execute_code(session, "import socket; socket.create_connection(('1.1.1.1', 80))")
    assert "not allowed" in result.lower() or "SecurityError" in result


def test_normal_workflow_unaffected_by_security(session):
    """Security hardening does not break a legitimate build123d session."""
    execute_code(session, "import math\nresult = Cylinder(math.pi, 20)")
    assert session.current_shape is not None
    data = json.loads(measure(session))
    assert data["bbox"]["zsize"] > 0


def test_dunder_name_allowed(session):
    """type(x).__name__ is a common debugging pattern and must be allowed."""
    result = execute_code(
        session, "from build123d import Box\nb = Box(1,1,1)\nname = type(b).__name__"
    )
    assert "Error" not in result
    assert session.namespace.get("name") == "Box"


def test_dunder_doc_allowed(session):
    """Reading __doc__ for API discovery must be allowed."""
    result = execute_code(session, "from build123d import Box\ndoc = Box.__doc__")
    assert "Error" not in result


def test_dunder_subclasses_still_blocked(session):
    """__subclasses__ traversal must remain blocked."""
    result = execute_code(session, "x = object.__subclasses__()")
    assert "not allowed" in result.lower() or "Error" in result


def test_current_shape_name_in_diagnostic(session):
    """When current_shape is a named show() object, its name appears in the diagnostic."""
    result = execute_code(session, "show(Box(10, 10, 10), 'mybox')")
    assert 'current_shape ("mybox")' in result or "mybox" in result


def test_builtins_import_restriction_independent_of_ast(session):
    """The builtins __import__ restriction provides a second layer: even if a
    future change widened the AST allowlist, the namespace-level filter still
    blocks non-allowlisted imports at runtime."""
    # Bypass AST by calling __import__ through builtins dict directly
    restricted_import = session.namespace["__builtins__"]["__import__"]
    with pytest.raises(ImportError, match="not allowed"):
        restricted_import("os")


# ---------------------------------------------------------------------------
# Session snapshots
# ---------------------------------------------------------------------------


def test_snapshot_restores_geometry_after_bad_experiment(session):
    """save_snapshot / restore_snapshot recovers known-good geometry."""
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("good")
    good_vol = json.loads(measure(session))["volume"]

    # Simulate an experiment that produces wrong geometry
    execute_code(session, "result = Box(1, 1, 1)")
    assert json.loads(measure(session))["volume"] < good_vol

    session.restore_snapshot("good")
    assert abs(json.loads(measure(session))["volume"] - good_vol) < 0.1


def test_snapshot_objects_registry_survives_round_trip(session):
    """Named objects are captured in the snapshot and restored correctly."""
    execute_code(session, "show(Box(60, 40, 8), 'frame')\nshow(Cylinder(5, 50), 'axle')")
    session.save_snapshot("assembly_v1")

    # Overwrite both objects
    execute_code(session, "show(Box(1, 1, 1), 'frame')\nshow(Box(1, 1, 1), 'axle')")
    session.restore_snapshot("assembly_v1")

    frame_bb = json.loads(measure(session, "frame"))["bbox"]
    axle_bb = json.loads(measure(session, "axle"))["bbox"]
    assert abs(frame_bb["xsize"] - 60) < 0.1
    assert abs(axle_bb["zsize"] - 50) < 0.1


def test_namespace_not_restored_by_snapshot(session):
    """Python variables set after a snapshot are still accessible after restore.
    This confirms the documented behaviour: snapshot saves geometry only."""
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("s1")
    execute_code(session, "extra_var = 123")
    session.restore_snapshot("s1")
    # extra_var is still in scope even though it was created after the snapshot
    assert session.namespace.get("extra_var") == 123


# ---------------------------------------------------------------------------
# Multi-object session: show(), per-object measure/export/render
# ---------------------------------------------------------------------------


def test_named_objects_have_independent_bounding_boxes(session):
    """show() isolates shapes: each named object reports its own dimensions."""
    execute_code(session, "show(Box(5, 5, 5), 'small')\nshow(Box(40, 40, 40), 'large')")
    small = json.loads(measure(session, "small"))["bbox"]
    large = json.loads(measure(session, "large"))["bbox"]
    assert abs(small["xsize"] - 5) < 0.01
    assert abs(large["xsize"] - 40) < 0.01


def test_assembly_render_differs_from_single_part_render(session):
    """Rendering all registered objects produces a different image than one part alone."""
    execute_code(session, "show(Box(10, 10, 10), 'box')\nshow(Cylinder(3, 30), 'cyl')")
    png_all = render_view(session, "iso")["png"]
    png_one = render_view(session, "iso", objects="box")["png"]
    assert png_all[:8] == PNG_MAGIC
    assert png_one[:8] == PNG_MAGIC
    assert png_all != png_one


def test_export_named_object_independent_of_current_shape(session, tmp_path, monkeypatch):
    """Exporting a named object writes that shape, not current_shape."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(5, 5, 5)\nshow(Box(50, 50, 50), 'big')")
    export_file(session, "big", "step", "big")
    # The big box STEP file should be larger than a tiny box would produce
    assert os.path.getsize("big.step") > 1000


def test_export_assembly_step_contains_all_parts(session, tmp_path, monkeypatch):
    """object_name='*' exports a compound of all named shapes; STEP is larger than any single part."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'box')\nshow(Cylinder(5, 20), 'cyl')")
    export_file(session, "box", "step", "box")
    export_file(session, "cyl", "step", "cyl")
    export_file(session, "assembly", "step", "*")
    box_size = os.path.getsize("box.step")
    cyl_size = os.path.getsize("cyl.step")
    asm_size = os.path.getsize("assembly.step")
    assert asm_size > box_size
    assert asm_size > cyl_size


def test_export_assembly_stl_is_valid_binary(session, tmp_path, monkeypatch):
    """object_name='*' produces a valid binary STL whose triangle count matches the header."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'a')\nshow(Box(5, 5, 5), 'b')")
    export_file(session, "assembly", "stl", "*")
    with open("assembly.stl", "rb") as f:
        data = f.read()
    # Binary STL: 80-byte header + 4-byte count + count * 50 bytes
    tri_count = struct.unpack_from("<I", data, 80)[0]
    assert tri_count > 0
    assert len(data) == 84 + tri_count * 50


def test_export_stl_avoids_mesher_for_complex_shape(session, tmp_path, monkeypatch):
    """STL export of a boolean-subtracted shape uses tessellate(), not Mesher, so it doesn't
    raise '3mf mesh is invalid' for shapes that pass OCCT meshing but fail Lib3MF validation."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(20, 20, 20) - Cylinder(5, 22), 'hollow')")
    export_file(session, "hollow", "stl", "hollow")
    with open("hollow.stl", "rb") as f:
        data = f.read()
    tri_count = struct.unpack_from("<I", data, 80)[0]
    assert tri_count > 0


def test_reset_clears_show_registry(session):
    """After reset, previously registered objects are gone."""
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    assert "part" in session.objects
    session.reset()
    assert not session.objects


# ---------------------------------------------------------------------------
# Richer measurements
# ---------------------------------------------------------------------------


def test_volume_detects_missing_boolean(session):
    """volume() exposes the difference between a solid and a hollowed part."""
    execute_code(session, "show(Box(20, 20, 20), 'full')")
    solid_vol = json.loads(measure(session, "full"))["volume"]
    execute_code(session, "show(Box(20, 20, 20) - Cylinder(5, 22), 'hollow')")
    hollow_vol = json.loads(measure(session, "hollow"))["volume"]
    assert hollow_vol < solid_vol
    assert abs(solid_vol - 8000) < 1


def test_area_increases_after_adding_surface(session):
    """Surface area grows when a protrusion is added."""
    execute_code(session, "result = Box(10, 10, 10)")
    base_area = json.loads(measure(session))["area"]
    execute_code(session, "result = Box(10, 10, 10) + Cylinder(2, 5).move(Location((0, 0, 7.5)))")
    new_area = json.loads(measure(session))["area"]
    assert new_area > base_area


def test_clearance_between_assembly_parts(session):
    """Clearance between two registered bodies."""
    from build123d_mcp.tools.measure import clearance as _clearance

    execute_code(
        session,
        "show(Cylinder(4, 30), 'shaft')\nshow(Box(30, 30, 30) - Cylinder(5, 32), 'bore_housing')",
    )
    data = json.loads(_clearance(session, "shaft", "bore_housing"))
    # shaft radius 4, bore radius 5 — clearance should be ~1mm
    assert data["clearance"] >= 0
    assert data["clearance"] < 5


def test_clearance_zero_for_touching_shapes(session):
    """Touching shapes report zero clearance."""
    from build123d_mcp.tools.measure import clearance as _clearance

    execute_code(
        session, "show(Box(10, 10, 10), 'a')\nshow(Box(10, 10, 10).move(Location((10, 0, 0))), 'b')"
    )
    data = json.loads(_clearance(session, "a", "b"))
    assert data["clearance"] < 0.01


# ---------------------------------------------------------------------------
# measure(summary)
# ---------------------------------------------------------------------------


def test_measure_returns_all_fields(session):
    """measure() returns bbox, volume, area, topology, center_of_mass, inertia, face_inventory."""
    execute_code(session, "result = Box(10, 20, 30)")
    data = json.loads(measure(session))
    assert abs(data["volume"] - 6000) < 1
    assert data["topology"]["faces"] == 6
    assert "bbox" in data and abs(data["bbox"]["xsize"] - 10) < 0.01
    assert abs(data["bbox"]["center"]["x"]) < 0.01
    assert "area" in data and data["area"] > 0
    assert "center_of_mass" in data
    assert "inertia" in data
    assert "inertia_units" in data
    assert "face_inventory" in data


# ---------------------------------------------------------------------------
# last_error()
# ---------------------------------------------------------------------------


def test_last_error_captures_type_message_and_line(session):
    """After a failed execute(), last_error() returns the exception type, message,
    and the 1-based line number within the submitted code."""
    from build123d_mcp.tools.last_error import last_error as get_last_error

    session.execute("x = 1\ny = 2\nraise ValueError('boom')\nz = 3")
    data = json.loads(get_last_error(session))
    assert data["type"] == "ValueError"
    assert "boom" in data["message"]
    assert data["line"] == 3
    assert "boom" in data["excerpt"]
    assert "→" in data["excerpt"]


def test_last_error_cleared_after_success(session):
    """A successful execute() clears last_error."""
    from build123d_mcp.tools.last_error import last_error as get_last_error

    session.execute("raise RuntimeError('oops')")
    assert json.loads(get_last_error(session))["type"] == "RuntimeError"
    session.execute("result = Box(5, 5, 5)")
    assert json.loads(get_last_error(session))["error"] is None


def test_last_error_null_before_any_failure(session):
    """last_error() returns {error: null} when no execute() has failed."""
    from build123d_mcp.tools.last_error import last_error as get_last_error

    session.execute("result = Box(1, 1, 1)")
    assert json.loads(get_last_error(session))["error"] is None


# ---------------------------------------------------------------------------
# session_state() namespace variables
# ---------------------------------------------------------------------------


def test_session_state_includes_namespace_variables(session):
    """session_state() variables key summarises Python namespace: scalars, lists, Shapes."""
    from build123d_mcp.tools.session_state import session_state as get_state

    execute_code(session, "width = 40.0\ntags = ['a', 'b', 'c']\nresult = Box(width, 10, 10)")
    data = json.loads(get_state(session))
    assert "variables" in data
    vs = data["variables"]
    assert vs["width"]["type"] == "float" and abs(vs["width"]["value"] - 40.0) < 0.001
    assert vs["tags"]["type"] == "list" and vs["tags"]["length"] == 3
    # result is a Shape — should have volume
    assert "volume" in vs["result"]


# ---------------------------------------------------------------------------
# validate_code()
# ---------------------------------------------------------------------------


def test_validate_code_clean_script_is_ok():
    """Well-formed build123d code returns ok=True with no blocked items."""
    from build123d_mcp.tools.validate_code import validate_code

    result = json.loads(validate_code("from build123d import *\nresult = Box(10, 10, 10)"))
    assert result["ok"] is True
    assert result["syntax"] == "ok"
    assert result["blocked"] == []


def test_validate_code_catches_syntax_error():
    """A syntax error is reported and ok=False."""
    from build123d_mcp.tools.validate_code import validate_code

    result = json.loads(validate_code("def foo(\n  pass"))
    assert result["ok"] is False
    assert "SyntaxError" in result["syntax"]


def test_validate_code_catches_blocked_import():
    """Importing os is flagged as blocked."""
    from build123d_mcp.tools.validate_code import validate_code

    result = json.loads(validate_code("import os\nresult = Box(10,10,10)"))
    assert result["ok"] is False
    assert any("os" in b for b in result["blocked"])


def test_validate_code_warns_no_output():
    """Code with no result assignment or show() call gets a warning."""
    from build123d_mcp.tools.validate_code import validate_code

    result = json.loads(validate_code("from build123d import *\nx = 1 + 2"))
    assert result["ok"] is True  # not blocked, just a warning
    assert any("result" in w or "show" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# shape_compare()
# ---------------------------------------------------------------------------


def test_shape_compare_reports_volume_delta(session):
    """shape_compare() reports the correct volume difference between two named shapes."""
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(session, "show(Box(10, 10, 10), 'small')\nshow(Box(20, 20, 20), 'large')")
    data = json.loads(shape_compare(session, "small", "large"))
    assert abs(data["a"]["volume"] - 1000) < 1
    assert abs(data["b"]["volume"] - 8000) < 1
    assert abs(data["delta"]["volume"] - 7000) < 1


def test_shape_compare_identical_shapes_zero_delta(session):
    """Comparing two identical shapes gives zero volume delta and zero center offset."""
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(session, "show(Box(10,10,10), 'a')\nshow(Box(10,10,10), 'b')")
    data = json.loads(shape_compare(session, "a", "b"))
    assert abs(data["delta"]["volume"]) < 0.001
    assert data["delta"]["center_offset"] < 0.001
    assert data["max_deviation"] < 0.001
    assert data["changed"]["moved_fraction"] == 0.0
    assert data["unchanged_elsewhere"] is True


def test_shape_compare_reports_local_surface_deviation(session):
    """A local boss translation reports a non-zero localized surface deviation."""
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(
        session,
        "base = Box(60, 30, 6)\n"
        "show(base + Pos(0, 0, 7) * Box(8, 8, 8), 'a')\n"
        "show(base + Pos(6, 0, 7) * Box(8, 8, 8), 'b')",
    )
    data = json.loads(shape_compare(session, "a", "b"))
    # Exact boolean magnitude: the boss slid 6mm, so displacement ~6mm, and equal
    # material is added (new position) and removed (old position).
    assert data["magnitude_method"] == "exact_boolean"
    assert data["max_deviation"] == pytest.approx(6.0, abs=0.35)
    assert data["changed"]["added_volume"] > 0 and data["changed"]["removed_volume"] > 0
    assert data["changed"]["bbox"] is not None
    assert data["unchanged_elsewhere"] is True


def test_shape_compare_exact_magnitude_growth(session):
    """A pure boss-height increase: exact boolean reports material ADDED, none removed,
    and the displacement equals the height change — not the inflated vertex-NN distance."""
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(
        session,
        "base = Box(60, 30, 6)\n"
        "show(base + Pos(0, 0, 7) * Box(8, 8, 8), 'a')\n"
        "show(base + Pos(0, 0, 8) * Box(8, 8, 10), 'b')",  # top 11 -> 13 mm: +2 mm
    )
    data = json.loads(shape_compare(session, "a", "b"))
    assert data["magnitude_method"] == "exact_boolean"
    assert data["max_deviation"] == pytest.approx(2.0, abs=0.2)
    assert data["changed"]["added_volume"] == pytest.approx(128.0, rel=0.1)  # 8*8*2
    assert data["changed"]["removed_volume"] == 0.0  # pure growth, nothing removed


def test_shape_compare_flags_change_elsewhere(session):
    """Two distant edits should not be reported as one clean localized change."""
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(
        session,
        "base = Box(80, 20, 6)\n"
        "show(base + Pos(-25, 0, 7) * Box(6, 6, 8), 'a')\n"
        "show(base + Pos(25, 0, 7) * Box(6, 6, 8), 'b')",
    )
    data = json.loads(shape_compare(session, "a", "b"))
    # Two separate bosses removed/added -> regions span the part -> not localized.
    assert data["unchanged_elsewhere"] is False
    assert data["changed"]["added_volume"] > 0 and data["changed"]["removed_volume"] > 0


def test_shape_compare_exact_magnitude_removal(session):
    """Drilling a hole reports material REMOVED, none added (exact boolean, right sign)."""
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(
        session,
        "base = Box(60, 30, 10)\n"
        "show(base, 'a')\n"
        "show(base - Cylinder(4, 10), 'b')",  # Ø8 through-hole removed
    )
    data = json.loads(shape_compare(session, "a", "b"))
    # Volumes are exact; the displacement is a mesh estimate (a cut has ~0 true surface
    # displacement) — the METHOD must say so, not claim 'exact_boolean'.
    assert data["magnitude_method"] == "exact_volume_mesh_displacement"
    assert data["changed"]["removed_volume"] == pytest.approx(502.65, rel=0.1)  # pi*16*10
    assert data["changed"]["added_volume"] == 0.0
    # max_deviation must NOT read 0 ("no change") for a real removal.
    assert data["max_deviation"] > 0.0
    assert any("cut or flush fill" in w for w in data["warnings"])


def test_shape_compare_budget_skip_falls_back_to_mesh(session):
    """With no op budget left, the exact boolean is skipped for the flagged mesh estimate."""
    import time

    from build123d_mcp._shape_compare_subprocess import compare_shapes

    execute_code(
        session,
        "base = Box(60, 30, 6)\n"
        "show(base + Pos(0, 0, 7) * Box(8, 8, 8), 'a')\n"
        "show(base + Pos(0, 0, 8) * Box(8, 8, 10), 'b')",
    )
    r = compare_shapes(session.objects["a"], session.objects["b"], deadline=time.monotonic())
    assert r["magnitude_method"] == "mesh_estimate"
    assert any("skipped" in w for w in r["warnings"])


def test_shape_compare_skips_boolean_on_wide_clip(session):
    """A wide-clip (spread) edit skips the exact boolean because the boolean cost
    scales with ABSOLUTE clip size — gated even on a sub-300mm part (the 206-class
    that a part-size floor wrongly let run 360s)."""
    from build123d_mcp._shape_compare_subprocess import compare_shapes

    execute_code(
        session,
        "base = Box(250, 40, 8)\n"  # diag ~253mm (<300) but the two changes are ~200mm apart
        "show(base + Pos(-100, 0, 9) * Box(8, 8, 8), 'a')\n"
        "show(base + Pos(100, 0, 9) * Box(8, 8, 8), 'b')",
    )
    r = compare_shapes(session.objects["a"], session.objects["b"])
    assert r["magnitude_method"] == "mesh_estimate"
    assert any("spread region" in w for w in r["warnings"])


def test_shape_compare_in_process_never_runs_boolean(session):
    """The in-process path (subprocess-blocked host) has no op-timeout to bound a
    runaway boolean, so it must run mesh-only (allow_exact=False)."""
    from build123d_mcp._shape_compare_subprocess import compare_shapes

    execute_code(
        session,
        "base = Box(60, 30, 6)\n"
        "show(base + Pos(0, 0, 7) * Box(8, 8, 8), 'a')\n"
        "show(base + Pos(0, 0, 8) * Box(8, 8, 10), 'b')",  # a real localized edit
    )
    r = compare_shapes(session.objects["a"], session.objects["b"], allow_exact=False)
    assert r["magnitude_method"] == "mesh_estimate"
    assert any("in-process" in w for w in r["warnings"])


def test_shape_compare_exception_after_boolean_keeps_mesh_estimate(session, monkeypatch):
    """If a post-boolean step raises, the comparison must fall back to the mesh
    estimate (the salvage), not crash — a raise must not lose what a timeout keeps."""
    import build123d_mcp._shape_compare_subprocess as scs
    from build123d_mcp._shape_compare_subprocess import compare_shapes

    execute_code(
        session,
        "base = Box(60, 30, 6)\n"
        "show(base + Pos(0, 0, 7) * Box(8, 8, 8), 'a')\n"
        "show(base + Pos(0, 0, 8) * Box(8, 8, 10), 'b')",
    )

    def _boom(*a, **k):
        raise RuntimeError("displacement blew up")

    monkeypatch.setattr(scs, "_chunk_displacement", _boom)
    r = compare_shapes(session.objects["a"], session.objects["b"])
    assert r["magnitude_method"] == "mesh_estimate"  # not an error, not a crash
    assert r["region_count"] > 0


def test_shape_compare_salvages_mesh_result_on_subprocess_timeout(session, monkeypatch, tmp_path):
    """If the boolean overruns and the subprocess is hard-killed, the driver returns
    the mesh-estimate result the worker persisted before the boolean — not a bare error."""
    import subprocess

    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(
        session, "show(Box(20, 10, 10), 'a')\nshow(Box(20, 10, 10) + Pos(0,0,6)*Box(4,4,4), 'b')"
    )

    def _kill(cmd, *a, **k):
        # cmd[5] is the out_json path; the worker would have persisted the mesh result
        # there before the boolean. Simulate that, then act like a hard timeout kill.
        with open(cmd[5], "w") as f:
            json.dump(
                {"max_deviation": 4.0, "magnitude_method": "mesh_estimate", "region_count": 1}, f
            )
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(subprocess, "run", _kill)
    data = json.loads(shape_compare(session, "a", "b"))
    assert data["surface_deviation"]["magnitude_method"] == "mesh_estimate"
    assert any("timed out" in w for w in data["surface_deviation"]["warnings"])


def test_shape_compare_reexport_noop_is_clean(session, tmp_path):
    """A shape vs a STEP round-trip of ITSELF — same geometry, INDEPENDENTLY
    re-tessellated — must report NO change. This guards the eps-vs-tessellation-
    noise regression: a fixed-mm eps sat below the noise floor and fabricated a
    multi-mm 'change' on identical geometry. Curved geometry (a cylinder wall) is
    used so the two tessellations genuinely differ, unlike two identical Box()es."""
    import os

    from build123d_mcp.tools.export import _write_step
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(session, "show(Cylinder(20, 30) + Box(70, 14, 30), 'orig')")
    rt = os.path.join(tmp_path, "roundtrip.step")
    _write_step(session.objects["orig"], rt)
    execute_code(session, f"show(import_step({rt!r}), 'rt')")

    data = json.loads(shape_compare(session, "orig", "rt"))
    # No REAL change: region-filtered max_deviation ~0, no localized region, clean.
    assert data["max_deviation"] < 1.0
    assert data["changed"]["moved_fraction"] == 0.0
    assert data["unchanged_elsewhere"] is True


def test_shape_compare_falls_back_in_process_when_subprocess_blocked(session, monkeypatch):
    """If child process creation is blocked, surface compare still runs in-process."""
    import subprocess

    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(session, "show(Box(10,10,10), 'a')\nshow(Box(10,10,10), 'b')")

    def _blocked(*a, **k):
        raise PermissionError("child process creation not permitted")

    monkeypatch.setattr(subprocess, "run", _blocked)
    data = json.loads(shape_compare(session, "a", "b"))
    assert data["max_deviation"] < 0.001
    assert "error" not in data["surface_deviation"]


def test_shape_compare_in_process_tessellation_failure_is_clean_error(session, monkeypatch):
    """On a subprocess-blocked host, an un-tessellatable shape (the build123d-0.11
    'NbNodes' quirk) must return a structured JSON error, not raise out of the tool."""
    import subprocess

    import build123d_mcp._shape_compare_subprocess as scs
    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(
        session, "show(Box(10,10,10), 'a')\nshow(Box(10,10,10) + Pos(0,0,6)*Box(4,4,4), 'b')"
    )

    def _blocked(*a, **k):
        raise PermissionError("child process creation not permitted")

    def _boom(*a, **k):
        raise AttributeError("'NoneType' object has no attribute 'NbNodes'")

    monkeypatch.setattr(subprocess, "run", _blocked)
    monkeypatch.setattr(scs, "_tessellate_points", _boom)
    data = json.loads(shape_compare(session, "a", "b"))  # must NOT raise
    assert "error" in data["surface_deviation"]
    assert "NbNodes" in data["surface_deviation"]["error"]


def test_shape_compare_timeout_is_clean_error(session, monkeypatch):
    import subprocess

    from build123d_mcp.tools.shape_compare import shape_compare

    execute_code(session, "show(Box(10,10,10), 'a')\nshow(Box(20,20,20), 'b')")

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="shape_compare", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    data = json.loads(shape_compare(session, "a", "b"))
    assert "time budget" in data["surface_deviation"]["error"]
    assert data["delta"]["volume"] > 0


# ---------------------------------------------------------------------------
# repair_hints()
# ---------------------------------------------------------------------------


def test_repair_hints_matches_nonetype_error():
    """NoneType attribute error gets the .part hint."""
    from build123d_mcp.tools.repair_hints import repair_hints

    result = json.loads(repair_hints("AttributeError: 'NoneType' has no attribute 'volume'"))
    assert any(".part" in h or "None" in h for h in result["hints"])


def test_repair_hints_matches_cq_idiom():
    """CadQuery-style error text gets the API mismatch hint."""
    from build123d_mcp.tools.repair_hints import repair_hints

    result = json.loads(repair_hints("NameError: cq is not defined"))
    assert any("CadQuery" in h or "build123d" in h for h in result["hints"])


def test_repair_hints_fallback_for_unknown_error():
    """Unrecognised error text returns the generic fallback hint."""
    from build123d_mcp.tools.repair_hints import repair_hints

    result = json.loads(repair_hints("some totally unknown error xyz"))
    assert len(result["hints"]) == 1
    assert "last_error" in result["hints"][0]


# ---------------------------------------------------------------------------
# Rendering quality and clip plane
# ---------------------------------------------------------------------------


def test_high_quality_render_differs_from_standard(session):
    """High quality tessellation produces a different image than standard."""
    execute_code(session, "result = Cylinder(5, 20)")
    png_std = render_view(session, "iso", quality="standard")["png"]
    png_hi = render_view(session, "iso", quality="high")["png"]
    assert png_std[:8] == PNG_MAGIC
    assert png_hi[:8] == PNG_MAGIC
    assert png_std != png_hi


def test_clip_plane_produces_different_image_than_unclipped(session):
    """A clipped render exposes internal geometry and differs from the unclipped view."""
    execute_code(session, "result = Cylinder(8, 30)")
    png_full = render_view(session, "iso")["png"]
    png_clip = render_view(session, "iso", clip_plane="y")["png"]
    assert png_full[:8] == PNG_MAGIC
    assert png_clip[:8] == PNG_MAGIC
    assert png_full != png_clip


# ---------------------------------------------------------------------------
# MCP protocol round-trip: test through the actual stdio transport
# ---------------------------------------------------------------------------
# Skipped on Windows: each round-trip cold-imports build123d in a freshly
# spawned worker (~60-90s on Windows runners), and pytest-timeout's "thread"
# method (the only one available on Windows) cannot kill a hung asyncio.run,
# so a stuck call blocks the entire test session indefinitely.

_skip_mcp_on_win = pytest.mark.skipif(
    sys.platform == "win32",
    reason="MCP-stdio round-trip too slow on Windows; covered by Linux/macOS jobs",
)


async def _mcp_session(coro, cwd=None):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command="uv",
        args=["run", "build123d-mcp"],
        cwd=cwd or SERVER_DIR,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            return await coro(mcp)


@_skip_mcp_on_win
def test_mcp_lists_all_tools():
    async def run(mcp):
        result = await mcp.list_tools()
        return {t.name for t in result.tools}

    names = asyncio.run(_mcp_session(run))
    assert names == {
        "execute",
        "render_view",
        "measure",
        "validate",
        "locate_gate_defects",
        "design_audit",
        "verify_spec",
        "export",
        "reset",
        "save_snapshot",
        "restore_snapshot",
        "diff_snapshot",
        "search_library",
        "load_part",
        "workflow_hints",
        "session_state",
        "health_check",
        "version",
        "last_error",
        "shape_compare",
        "repair_hints",
        "import_cad_file",
        "cross_sections",
        "clearance",
        "inspect_drawing",
        "view_axes",
        "lint_drawing",
        "render_drawing",
        "save_drawing_annotations",
        "align_check",
        "find_holes",
        "find_bosses",
        "find_countersinks",
        "find_hole_patterns",
        "resolve",
        "script",
        "install_skill",
        "suggest_view_layout",
        "analyze_printability",
    }


@_skip_mcp_on_win
def test_mcp_execute_and_measure_round_trip():
    async def run(mcp):
        await mcp.call_tool(
            "execute",
            {"code": "from build123d import *\nresult = Box(10, 20, 30)"},
        )
        result = await mcp.call_tool("measure", {})
        return result.content[0].text

    data = json.loads(asyncio.run(_mcp_session(run)))
    assert abs(data["bbox"]["xsize"] - 10) < 0.01
    assert abs(data["bbox"]["ysize"] - 20) < 0.01
    assert abs(data["bbox"]["zsize"] - 30) < 0.01


@_skip_mcp_on_win
def test_mcp_render_returns_image_and_file_path():
    async def run(mcp):
        await mcp.call_tool(
            "execute",
            {"code": "from build123d import *\nresult = Box(10, 10, 10)"},
        )
        result = await mcp.call_tool("render_view", {"direction": "iso"})
        img = result.content[0]
        path_item = result.content[1]
        return img.type, img.data, img.mimeType, path_item.type, path_item.text

    img_type, img_data, mime, path_type, path_text = asyncio.run(_mcp_session(run))
    # ImageContent with base64 PNG
    assert img_type == "image"
    assert mime == "image/png"
    png_bytes = base64.b64decode(img_data)
    assert png_bytes[:8] == PNG_MAGIC
    # TextContent with file path for [SEND:] delivery
    assert path_type == "text"
    path = path_text.removeprefix("[SEND: ").removesuffix("]")
    assert path.endswith(".png")
    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read(8) == PNG_MAGIC


@_skip_mcp_on_win
def test_mcp_reset_clears_state():
    async def run(mcp):
        await mcp.call_tool(
            "execute",
            {"code": "from build123d import *\nresult = Box(10, 10, 10)"},
        )
        await mcp.call_tool("reset", {})
        # measure should now fail — no shape
        result = await mcp.call_tool("measure", {})
        return result.content[0].text

    text = asyncio.run(_mcp_session(run))
    assert "No shape" in text


@_skip_mcp_on_win
def test_mcp_injection_attempt_returns_error_not_executes():
    """A shell-injection payload through the MCP wire returns an error and does
    not produce side-effects (geometry is still None at the start of the session)."""

    async def run(mcp):
        result = await mcp.call_tool(
            "execute",
            {"code": "import subprocess; subprocess.run(['id'], capture_output=True)"},
        )
        return result.content[0].text

    text = asyncio.run(_mcp_session(run))
    assert "not allowed" in text.lower() or "SecurityError" in text


@_skip_mcp_on_win
def test_mcp_snapshot_save_and_restore():
    """save_snapshot / restore_snapshot round-trip through the MCP wire restores geometry."""

    async def run(mcp):
        await mcp.call_tool(
            "execute", {"code": "from build123d import *\nresult = Box(10, 10, 10)"}
        )
        await mcp.call_tool("save_snapshot", {"name": "v1"})
        await mcp.call_tool("execute", {"code": "result = Box(99, 99, 99)"})
        await mcp.call_tool("restore_snapshot", {"name": "v1"})
        result = await mcp.call_tool("measure", {})
        return result.content[0].text

    data = json.loads(asyncio.run(_mcp_session(run)))
    assert abs(data["bbox"]["xsize"] - 10) < 0.01


@_skip_mcp_on_win
def test_mcp_multi_format_export(tmp_path):
    """export with format='step,stl' reports both paths over the MCP wire."""

    async def run(mcp):
        await mcp.call_tool(
            "execute", {"code": "from build123d import *\nresult = Box(10, 10, 10)"}
        )
        result = await mcp.call_tool("export", {"filename": "mcp_test_out", "format": "step,stl"})
        return result.content[0].text

    text = asyncio.run(_mcp_session(run, cwd=str(tmp_path)))
    assert ".step" in text
    assert ".stl" in text


@_skip_mcp_on_win
def test_mcp_volume_and_clearance():
    """volume and clearance round-trip through the MCP wire."""

    async def run(mcp):
        await mcp.call_tool(
            "execute",
            {
                "code": (
                    "from build123d import *\n"
                    "show(Box(10, 10, 10), 'a')\n"
                    "show(Box(10, 10, 10).move(Location((15, 0, 0))), 'b')"
                )
            },
        )
        r_vol = await mcp.call_tool("measure", {"object_name": "a"})
        r_cl = await mcp.call_tool("clearance", {"object_a": "a", "object_b": "b"})
        return r_vol.content[0].text, r_cl.content[0].text

    vol_json, cl_json = asyncio.run(_mcp_session(run))
    assert abs(json.loads(vol_json)["volume"] - 1000) < 1
    assert abs(json.loads(cl_json)["clearance"] - 5) < 0.1


@_skip_mcp_on_win
def test_mcp_show_and_measure_named_object():
    """show() + per-object measure round-trip through the MCP wire."""

    async def run(mcp):
        await mcp.call_tool(
            "execute",
            {
                "code": "from build123d import *\nshow(Box(40, 5, 5), 'wide')\nshow(Box(5, 5, 40), 'tall')"
            },
        )
        r_wide = await mcp.call_tool("measure", {"object_name": "wide"})
        r_tall = await mcp.call_tool("measure", {"object_name": "tall"})
        return r_wide.content[0].text, r_tall.content[0].text

    wide_json, tall_json = asyncio.run(_mcp_session(run))
    wide = json.loads(wide_json)
    tall = json.loads(tall_json)
    assert abs(wide["bbox"]["xsize"] - 40) < 0.01
    assert abs(tall["bbox"]["zsize"] - 40) < 0.01
