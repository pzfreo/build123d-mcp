import json
import os
import sys

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.diff import diff_snapshot
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.export import export_file
from build123d_mcp.tools.health_check import health_check
from build123d_mcp.tools.interference import interference
from build123d_mcp.tools.list_objects import list_objects
from build123d_mcp.tools.measure import measure
from build123d_mcp.tools.render import render_view
from build123d_mcp.tools.session_state import session_state

# A path guaranteed to resolve outside any allowed write root (cwd, tempdir, /tmp)
# on each platform. /etc/passwd on POSIX; on Windows we use the system hosts file,
# which is always present and lives under C:\Windows so realpath stays under C:\Windows.
_OUTSIDE_ROOT_PATH = (
    r"C:\Windows\System32\drivers\etc\hosts" if sys.platform == "win32" else "/etc/passwd"
)


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


@pytest.fixture
def fast_session():
    """Session with a 1-second exec timeout for testing timeout behaviour."""
    s = Session(exec_timeout=1)
    s.execute("from build123d import *")
    return s


# --- execute ---


def test_execute_persists_state(session):
    execute_code(session, "x = 42")
    result = execute_code(session, "print(x)")
    assert "42" in result


def test_execute_captures_stdout(session):
    result = execute_code(session, "print('hello')")
    assert "hello" in result


def test_execute_error_returns_message(session):
    result = execute_code(session, "raise ValueError('bad input')")
    assert "ValueError" in result
    assert "bad input" in result


def test_execute_error_appends_hint_for_known_pattern(session):
    result = execute_code(session, "import os")
    assert "Hint:" in result
    assert "Import blocked" in result or "not allowed" in result.lower()


def test_execute_success_has_no_hint(session):
    result = execute_code(session, "x = 42")
    assert "Hint:" not in result


# --- var summary ---


def test_execute_var_summary_shows_new_scalar(session):
    result = execute_code(session, "FV_Y = 35.0")
    assert "# vars:" in result
    assert "FV_Y=35.0" in result


def test_execute_var_summary_distinct_for_different_values(session):
    r1 = execute_code(session, "FV_Y = 35.0")
    r2 = execute_code(session, "FV_Y = 47.0")
    assert r1 != r2
    assert "FV_Y=47.0" in r2


def test_execute_var_summary_unchanged_var_not_repeated(session):
    execute_code(session, "x = 1")
    result = execute_code(session, "x = 1")
    assert "x=" not in result


def test_execute_var_summary_multiple_vars(session):
    result = execute_code(session, "SCALE = 2.0\nPAGE_W = 297.0")
    assert "SCALE=2.0" in result
    assert "PAGE_W=297.0" in result


def test_execute_var_summary_skips_shapes(session):
    result = execute_code(session, "result = Box(1, 1, 1)")
    # shapes are reported in the shape diagnostic, not the var summary line
    var_line = next((l for l in result.splitlines() if l.startswith("# vars:")), "")
    assert "Box" not in var_line


def test_execute_var_summary_skips_private_names(session):
    result = execute_code(session, "_hidden = 99")
    assert "_hidden=" not in result


def test_execute_var_summary_absent_on_error(session):
    result = execute_code(session, "bad syntax !!!")
    assert "# vars:" not in result


def test_execute_session_objects_appended_on_success(session):
    result = execute_code(session, "from build123d import *\nshow(Box(1, 1, 1), 'cube')")
    assert "Session objects:" in result
    assert "cube" in result


def test_execute_session_objects_none_on_empty(session):
    result = execute_code(session, "x = 42")
    assert "Session objects: (none)" in result


def test_execute_session_objects_absent_on_error(session):
    result = execute_code(session, "bad syntax !!!")
    assert "Session objects:" not in result


def test_execute_var_summary_tuple_of_scalars_included(session):
    result = execute_code(session, "pt = (1.0, 2.0, 3.0)")
    assert "pt=(1.0, 2.0, 3.0)" in result


def test_execute_var_summary_tuple_of_shapes_excluded(session):
    result = execute_code(session, "from build123d import *\nt = (Box(1,1,1), Box(2,2,2))")
    var_line = next((l for l in result.splitlines() if l.startswith("# vars:")), "")
    assert "t=" not in var_line


def test_execute_var_summary_long_repr_truncated(session):
    result = execute_code(session, "s = 'x' * 100")
    var_line = next((l for l in result.splitlines() if l.startswith("# vars:")), "")
    assert "..." in var_line
    assert len(var_line) < 200


def test_execute_creates_shape(session):
    execute_code(session, "result = Box(10, 10, 10)")
    assert session.current_shape is not None


def test_execute_detects_buildpart(session):
    execute_code(session, "with BuildPart() as p:\n    Box(10, 10, 10)")
    assert session.current_shape is not None


# --- execute() diagnostics: deltas + anomaly warnings ---


def test_execute_first_shape_shows_absolutes_no_warnings(session):
    """First shape ever: no previous to diff against, so no delta and no warnings."""
    out = execute_code(session, "result = Box(10, 10, 5)")
    assert "current_shape" in out
    assert "500" in out  # volume
    assert "Warning:" not in out
    # No delta markers
    assert "(+" not in out and "(-" not in out


def test_execute_real_cut_shows_deltas(session):
    """A successful boolean cut shows the volume and topology deltas inline."""
    execute_code(session, "result = Box(10, 10, 5)")
    out = execute_code(session, "result = result - Cylinder(2, 5)")
    # Volume dropped, faces increased — delta markers should appear
    assert "(-" in out  # volume decreased
    assert "(+" in out  # faces increased
    assert "%" in out  # percentage delta on volume
    assert "Warning:" not in out


def test_execute_warns_on_boolean_noop(session):
    """A boolean that produces no change (cylinder placed far away) triggers the
    no-op warning so the LLM can't silently sail past a failed cut."""
    execute_code(session, "result = Box(10, 10, 5)")
    out = execute_code(
        session,
        "result = result - Cylinder(2, 5).move(Location((100, 100, 100)))",
    )
    assert "Warning:" in out
    assert "boolean may have missed" in out


def test_execute_warns_on_degenerate(session):
    """Intersection that produces an empty result triggers the degenerate warning."""
    execute_code(session, "result = Box(10, 10, 5)")
    out = execute_code(
        session,
        "result = result & Cylinder(1, 1).move(Location((100, 100, 100)))",
    )
    assert "Warning:" in out
    assert "volume" in out and "0" in out
    assert "degenerate" in out


def test_show_outranks_leftover_2d_object(session):
    """A show()-registered solid must not be displaced by a leftover 2D object
    created in the same execute() — the false 'volume ≈ 0' alarm from #236."""
    out = execute_code(
        session,
        "peg = Cylinder(2, 8)\nsk = Rectangle(1.3, 4.8)\nshow(peg, 'peg')",
    )
    assert session.current_shape is session.objects["peg"]
    assert "degenerate" not in out


def test_show_outranks_stale_result_variable(session):
    """show() in a later call wins over a `result` variable left from an
    earlier call (#236)."""
    execute_code(session, "result = Box(10, 10, 5)")
    execute_code(session, "show(Cylinder(3, 10), 'cyl')")
    assert session.current_shape is session.objects["cyl"]


def test_variable_scan_resumes_after_show_call(session):
    """The explicit-registration override applies only to the call that made
    it — the next execute()'s variable scan works as before."""
    execute_code(session, "show(Box(10, 10, 5), 'base')")
    execute_code(session, "cyl = Cylinder(3, 10)")
    assert session.current_shape is session.namespace["cyl"]


def test_2d_shape_gets_note_not_degenerate_warning(session):
    """A live sketch as current shape is reported as 2D geometry, not as a
    failed boolean (#236)."""
    execute_code(session, "show(Box(10, 10, 5), 'base')")
    out = execute_code(session, "sk = Rectangle(5, 5)")
    assert "degenerate" not in out
    assert "no solid volume" in out


def test_execute_move_does_not_warn(session):
    """A pure translation changes bbox center, not size/topology/volume — must
    not trigger the no-op warning."""
    execute_code(session, "result = Box(10, 10, 5)")
    out = execute_code(session, "result = result.move(Location((50, 0, 0)))")
    assert "Warning:" not in out


def test_execute_no_warnings_when_shape_unchanged(session):
    """If execute doesn't change current_shape (e.g. just printing), no
    diagnostic block at all — no warnings, no deltas."""
    execute_code(session, "result = Box(10, 10, 5)")
    out = execute_code(session, "print('inspecting')")
    assert "current_shape" not in out
    assert "Warning:" not in out
    assert "inspecting" in out


# --- security ---


def test_show_objects_persist_across_execute_calls(session):
    """Objects registered with show() in one execute call are accessible in later calls."""
    execute_code(session, "show(Box(10, 10, 10), 'box1')")
    execute_code(session, "show(Cylinder(5, 10), 'cyl1')")
    assert "box1" in session.objects
    assert "cyl1" in session.objects
    out = render_view(session, "iso", "box1")
    assert out["png"][:8] == PNG_MAGIC


def test_import_os_blocked(session):
    result = execute_code(session, "import os")
    assert "SecurityError" in result or "not allowed" in result.lower()


def test_import_subprocess_blocked(session):
    result = execute_code(session, "import subprocess")
    assert "not allowed" in result.lower()


def test_from_os_import_blocked(session):
    result = execute_code(session, "from os import getcwd")
    assert "not allowed" in result.lower()


def test_import_socket_blocked(session):
    result = execute_code(session, "import socket")
    assert "not allowed" in result.lower()


def test_import_pathlib_blocked(session):
    result = execute_code(session, "import pathlib")
    assert "not allowed" in result.lower()


def test_eval_call_blocked(session):
    result = execute_code(session, "eval('1+1')")
    assert "not allowed" in result.lower()


def test_exec_call_blocked(session):
    result = execute_code(session, "exec('x=1')")
    assert "not allowed" in result.lower()


def test_open_call_blocked(session):
    result = execute_code(session, "open('/etc/passwd')")
    assert "not allowed" in result.lower()


def test_open_removed_from_builtins(session):
    # open is blocked by builtins restriction even if AST check were bypassed
    assert "open" not in session.namespace.get("__builtins__", {})


# --- --allow-imports flag (granular allowlist extension) ---


def test_extra_allowed_import_permits_specified_module(monkeypatch):
    """Modules added via --allow-imports become importable (using a stdlib
    module that's normally blocked, so the test doesn't depend on optional
    third-party packages being installed)."""
    import build123d_mcp.security as _sec

    monkeypatch.setattr(_sec, "EXTRA_ALLOWED_IMPORTS", set(_sec.EXTRA_ALLOWED_IMPORTS))
    _sec.EXTRA_ALLOWED_IMPORTS.add("os")
    s = Session()
    result = s.execute("import os")
    assert "not allowed" not in result.lower()
    assert "Error" not in result


def test_extra_allowed_import_permits_submodules(monkeypatch):
    """Allowing a root module also permits its submodules (e.g. allowing
    'os' lets 'os.path' through)."""
    import build123d_mcp.security as _sec

    monkeypatch.setattr(_sec, "EXTRA_ALLOWED_IMPORTS", set(_sec.EXTRA_ALLOWED_IMPORTS))
    _sec.EXTRA_ALLOWED_IMPORTS.add("os")
    s = Session()
    result = s.execute("import os.path")
    assert "not allowed" not in result.lower()
    assert "Error" not in result


def test_unspecified_module_still_blocked_when_extras_used(monkeypatch):
    """Adding 'foo' to extras must NOT allow 'bar'."""
    import build123d_mcp.security as _sec

    monkeypatch.setattr(_sec, "EXTRA_ALLOWED_IMPORTS", set(_sec.EXTRA_ALLOWED_IMPORTS))
    _sec.EXTRA_ALLOWED_IMPORTS.add("os")
    s = Session()
    result = s.execute("import subprocess")
    assert "not allowed" in result.lower()


def test_extras_message_lists_added_modules(monkeypatch):
    """Error message for blocked imports should include user-added extras
    in the 'Permitted' list so the LLM sees the current state."""
    import build123d_mcp.security as _sec

    monkeypatch.setattr(_sec, "EXTRA_ALLOWED_IMPORTS", set(_sec.EXTRA_ALLOWED_IMPORTS))
    _sec.EXTRA_ALLOWED_IMPORTS.add("os")
    s = Session()
    result = s.execute("import subprocess")
    assert "'os'" in result


def test_pythonpath_package_importable_when_allowlisted(monkeypatch, tmp_path):
    """A package on PYTHONPATH (not installed) becomes importable when its
    root name is added to the allowlist via --allow-imports / EXTRA_ALLOWED_IMPORTS.
    Regression for issue #150: the runtime __import__ wrapper should honour
    EXTRA_ALLOWED_IMPORTS and let _original_import resolve the package from
    sys.path (which includes PYTHONPATH)."""
    import build123d_mcp.security as _sec

    # Create a minimal package in a temp directory (not installed).
    pkg_dir = tmp_path / "my_test_parts"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("VALUE = 42\n")

    monkeypatch.setattr(_sec, "EXTRA_ALLOWED_IMPORTS", set(_sec.EXTRA_ALLOWED_IMPORTS))
    _sec.EXTRA_ALLOWED_IMPORTS.add("my_test_parts")
    # Add the tmp_path to sys.path so Python can find the package.
    monkeypatch.syspath_prepend(str(tmp_path))

    s = Session()
    result = s.execute(
        "from my_test_parts import VALUE\nassert VALUE == 42, f'expected 42, got {VALUE}'"
    )
    assert "Error" not in result, f"PYTHONPATH package import failed: {result}"
    assert "not allowed" not in result.lower()


def test_blocked_import_error_message_mentions_allow_imports():
    """The Layer-1 AST error message for a blocked module should tell the user
    how to add it to the allowlist via --allow-imports / BUILD123D_ALLOW_IMPORTS."""
    s = Session()
    result = s.execute("import subprocess")
    assert "--allow-imports" in result or "BUILD123D_ALLOW_IMPORTS" in result, (
        "error message should mention how to add the module to the allowlist"
    )


def test_runtime_blocked_import_message_mentions_allow_imports():
    """The Layer-2 _safe_import error message should also mention --allow-imports.
    Exercises _safe_import directly, bypassing the AST check (Layer 1)."""
    from build123d_mcp.security import make_restricted_builtins

    builtins = make_restricted_builtins()
    safe_import = builtins["__import__"]
    with pytest.raises(ImportError, match="--allow-imports|BUILD123D_ALLOW_IMPORTS"):
        safe_import("subprocess")


def test_dir_allowed(session):
    result = execute_code(session, "attrs = dir([])")
    assert "Error" not in result


def test_dir_shows_methods(session):
    result = execute_code(session, "names = dir([])\nassert 'append' in names")
    assert "Error" not in result


def test_inspect_import_allowed(session):
    result = execute_code(session, "import inspect")
    assert "not allowed" not in result.lower()
    assert "Error" not in result


def test_inspect_signature_works(session):
    result = execute_code(
        session, "import inspect\nfrom build123d import Box\nsig = str(inspect.signature(Box))"
    )
    assert "Error" not in result


def test_import_math_allowed(session):
    result = execute_code(session, "import math\nx = math.pi")
    assert "Error" not in result


def test_import_build123d_allowed(session):
    result = execute_code(session, "from build123d import *\nresult = Box(1, 1, 1)")
    assert "Error" not in result


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Session.execute uses SIGALRM for timeout, which is POSIX-only; in-process timeout protection is not available on Windows (the WorkerSession parent-side timeout still works in production).",
)
def test_execution_timeout(fast_session):
    result = execute_code(fast_session, "while True: pass")
    assert "timeout" in result.lower() or "time limit" in result.lower()


def test_worker_execution_timeout():
    """The production timeout path (WorkerSession's parent-side multiprocessing
    poll) works on every platform, including Windows where the in-process
    SIGALRM guard is a no-op. A `while True: pass` must return a timeout error,
    not hang the test."""
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=2)
    try:
        result = ws.execute("while True: pass")
        assert "timeout" in result.lower() or "time limit" in result.lower()
    finally:
        ws._kill_worker()


def test_blocked_import_does_not_corrupt_shape(session):
    execute_code(session, "result = Box(10, 10, 10)")
    shape_before = session.current_shape
    execute_code(session, "import os")
    assert session.current_shape is shape_before


# --- OCP import allowlist ---


def test_ocp_safe_module_allowed(session):
    result = execute_code(session, "from OCP.BRepGProp import BRepGProp")
    assert "not allowed" not in result.lower() and "Error" not in result


def test_ocp_topology_modules_allowed(session):
    result = execute_code(
        session,
        (
            "from OCP.TopExp import TopExp_Explorer\n"
            "from OCP.TopAbs import TopAbs_FACE\n"
            "from OCP.TopoDS import TopoDS"
        ),
    )
    assert "not allowed" not in result.lower() and "Error" not in result


def test_ocp_gp_module_allowed(session):
    result = execute_code(session, "from OCP.gp import gp_Pnt\np = gp_Pnt(1.0, 2.0, 3.0)")
    assert "Error" not in result


def test_ocp_step_control_blocked(session):
    result = execute_code(session, "from OCP.STEPControl import STEPControl_Reader")
    assert "not allowed" in result.lower() or "blocked" in result.lower()


def test_ocp_iges_control_blocked(session):
    result = execute_code(session, "import OCP.IGESControl")
    assert "not allowed" in result.lower() or "blocked" in result.lower()


def test_ocp_osd_blocked(session):
    result = execute_code(session, "from OCP.OSD import OSD_File")
    assert "not allowed" in result.lower() or "blocked" in result.lower()


def test_ocp_brep_gprop_computes_volume(session):
    # Full round-trip: build shape, extract .wrapped, compute volume via OCP directly
    code = (
        "from build123d import Box\n"
        "from OCP.BRepGProp import BRepGProp\n"
        "from OCP.GProp import GProp_GProps\n"
        "b = Box(10, 10, 10)\n"
        "props = GProp_GProps()\n"
        "BRepGProp.VolumeProperties_s(b.wrapped, props)\n"
        "result_volume = props.Mass()\n"
    )
    result = execute_code(session, code)
    assert "Error" not in result
    assert abs(session.namespace.get("result_volume", 0) - 1000) < 1


def test_runtime_error_does_not_update_current_shape(session):
    execute_code(session, "result = Box(10, 10, 10)")
    shape_before = session.current_shape
    execute_code(session, "bad = Box(5, 5, 5); raise ValueError('oops')")
    assert session.current_shape is shape_before


def test_runtime_error_preserves_prior_namespace(session):
    """Variables from previous successful executes survive a later failing execute."""
    execute_code(session, "leaf_a = Box(10, 10, 5)\nleaf_b = Box(8, 8, 3)")
    execute_code(session, "leaf_a.volume  # this doesn't fail\nraise AttributeError('boom')")
    assert "leaf_a" in session.namespace
    assert "leaf_b" in session.namespace


def test_runtime_error_preserves_partial_namespace(session):
    """Variables defined in the failing execute before the error point also persist."""
    execute_code(session, "x = 1")
    execute_code(session, "new_var = 42\nraise ValueError('oops')")
    assert session.namespace.get("new_var") == 42


def test_show_sets_current_shape(session):
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    assert session.current_shape is not None
    assert session.objects.get("part") is session.current_shape


# --- render_view ---

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_render_view_iso_returns_png(session):
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso")
    assert "png" in out and out["png"][:8] == PNG_MAGIC


def test_render_view_all_directions(session):
    execute_code(session, "result = Box(10, 10, 10)")
    for direction in ("top", "front", "side", "iso"):
        out = render_view(session, direction)
        assert out["png"][:8] == PNG_MAGIC, f"direction '{direction}' did not return valid PNG"


def test_render_view_invalid_direction(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown direction"):
        render_view(session, "diagonal")


def test_render_view_no_shape_raises(session):
    with pytest.raises(ValueError, match="No shape"):
        render_view(session, "iso")


def test_render_view_high_quality_returns_png(session):
    execute_code(session, "result = Cylinder(5, 20)")
    out = render_view(session, "iso", quality="high")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_invalid_quality_raises(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown quality"):
        render_view(session, "iso", quality="ultra")


def test_render_view_clip_plane_returns_png(session):
    execute_code(session, "result = Cylinder(5, 20)")
    for plane in ("x", "y", "z"):
        out = render_view(session, "iso", clip_plane=plane)
        assert out["png"][:8] == PNG_MAGIC, f"clip_plane '{plane}' did not return valid PNG"


def test_render_view_invalid_clip_plane_raises(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown clip_plane"):
        render_view(session, "iso", clip_plane="w")


def test_render_view_svg_format(session):
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", format="svg")
    assert "svg" in out and "png" not in out
    assert b"<svg" in out["svg"]


def test_render_view_both_format(session):
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", format="both")
    assert out["png"][:8] == PNG_MAGIC
    assert b"<svg" in out["svg"]


def test_render_view_invalid_format_raises(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown format"):
        render_view(session, "iso", format="jpeg")


def test_render_view_fallback_to_svg_when_vtk_fails(session, monkeypatch):
    execute_code(session, "result = Box(10, 10, 10)")
    from build123d_mcp.tools import render as render_mod

    def boom(*_a, **_kw):
        raise RuntimeError("simulated GL failure")

    monkeypatch.setattr(render_mod, "_do_render_png", boom)
    out = render_view(session, "iso", format="png")
    assert "png" not in out
    assert b"<svg" in out["svg"]
    assert "fallback" in out and "simulated GL failure" in out["fallback"]
    assert out.get("format") == "svg"


def test_render_view_save_to_both_writes_two_files(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    render_view(session, "iso", save_to="multi", format="both")
    assert (tmp_path / "multi.png").exists()
    assert (tmp_path / "multi.svg").exists()


# --- render_view DXF ---


def test_render_view_dxf_returns_dxf_bytes(session):
    execute_code(session, "result = Box(20, 10, 5)")
    out = render_view(session, "top", format="dxf")
    assert "dxf" in out and "png" not in out and "svg" not in out
    # DXF magic: file starts with "  0\nSECTION\n  2\nHEADER" and contains $ACADVER
    assert b"SECTION" in out["dxf"]
    assert b"$ACADVER" in out["dxf"]


def test_render_view_dxf_contains_named_layers(session):
    """Each named shape should produce visible + hidden layers in the DXF."""
    execute_code(session, "show(Box(20, 10, 5), 'plate')")
    out = render_view(session, "top", format="dxf")
    text = out["dxf"].decode("utf-8", errors="ignore")
    assert "plate_visible" in text
    assert "plate_hidden" in text


def test_render_view_dxf_save_to_writes_dxf_file(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(20, 10, 5)")
    render_view(session, "top", save_to="part", format="dxf")
    assert (tmp_path / "part.dxf").exists()
    assert (tmp_path / "part.dxf").stat().st_size > 0


def test_render_view_dxf_save_to_records_path_in_result(session, tmp_path, monkeypatch):
    """Regression for #91: render_view must record the on-disk path for save_to
    output so the MCP wrapper can use it for [SEND:] markers instead of writing
    a duplicate tempfile. Without this, the LLM gets a /tmp/<random> path back
    even when it asked for a specific location."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(20, 10, 5)")
    out = render_view(session, "top", save_to="part", format="dxf")
    assert "dxf_path" in out
    assert out["dxf_path"].endswith("part.dxf")
    assert os.path.exists(out["dxf_path"])


def test_render_view_png_save_to_records_path_in_result(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(20, 10, 5)")
    out = render_view(session, "iso", save_to="part", format="png")
    assert "png_path" in out
    assert out["png_path"].endswith("part.png")


def test_render_view_no_save_to_no_path_in_result(session):
    """When save_to is empty, no <fmt>_path key is added — the wrapper falls
    back to writing a tempfile."""
    execute_code(session, "result = Box(20, 10, 5)")
    out = render_view(session, "iso", format="png")
    assert "png" in out
    assert "png_path" not in out


def test_render_view_dxf_save_to_strips_dxf_extension(session, tmp_path, monkeypatch):
    """Passing save_to='part.dxf' should write part.dxf (not part.dxf.dxf)."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(20, 10, 5)")
    render_view(session, "top", save_to="part.dxf", format="dxf")
    assert (tmp_path / "part.dxf").exists()
    assert not (tmp_path / "part.dxf.dxf").exists()


def test_render_view_dxf_invalid_format_message_lists_dxf(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="dxf"):
        render_view(session, "iso", format="jpeg")


# --- render_view 2D drafting (Sketch/Compound) ---


def _build_2d_drawing(session):
    """Set up a session with a dimensioned 2D drawing as a named object."""
    execute_code(
        session,
        """
plate = Box(40, 20, 5) - Cylinder(3, 5).move(Location((10, 0, 0)))
visible, _ = plate.project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
draft = Draft(font_size=2.5, decimal_precision=1)
length = ExtensionLine(border=[(-20, -10, 0), (20, -10, 0)], offset=8, draft=draft, label='40')
drawing = Compound(children=list(visible) + [length])
show(drawing, 'top_view')
""",
    )


def test_render_view_2d_returns_png(session):
    """A composed 2D drawing renders to a real PNG via the resvg-py path."""
    _build_2d_drawing(session)
    out = render_view(session, objects="top_view", format="png")
    assert "png" in out
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_2d_dxf_returns_dxf(session):
    """A composed 2D drawing exports to DXF via render_view too."""
    _build_2d_drawing(session)
    out = render_view(session, objects="top_view", format="dxf")
    assert "dxf" in out
    assert b"SECTION" in out["dxf"]


def test_render_view_2d_with_label_objects(session):
    """label_objects=True for a 2D drawing adds a label below the bbox."""
    _build_2d_drawing(session)
    plain = render_view(session, objects="top_view", format="png", label_objects=False)
    labelled = render_view(session, objects="top_view", format="png", label_objects=True)
    # Labelled render is larger because of the added text glyphs
    assert len(labelled["png"]) > len(plain["png"])


def test_render_view_2d_save_to_writes_png(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _build_2d_drawing(session)
    render_view(session, objects="top_view", format="png", save_to="out")
    assert (tmp_path / "out.png").exists()


def test_render_view_2d_multi_object_uses_2d_path(session):
    """Regression for #92 F3: multi-object 2D inputs (e.g. two parts both
    composed via build123d.drafting) must render through the 2D pipeline,
    not fall back to 3D VTK. The detection is: every shape has no solids
    AND lies flat in Z."""
    execute_code(
        session,
        """
plate_a, _ = (Box(20, 20, 5)).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0)), None
visible_a, _ = Box(20, 20, 5).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
visible_b, _ = Box(15, 15, 3).move(Location((25, 0, 0))).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible_a)), 'plate_a')
show(Compound(children=list(visible_b)), 'plate_b')
""",
    )
    out = render_view(session, objects="plate_a,plate_b", format="dxf")
    text = out["dxf"].decode("utf-8", errors="ignore")
    # Multi-object 2D path produces per-object _part layers
    assert "plate_a_part" in text
    assert "plate_b_part" in text


def test_render_view_2d_multi_object_honours_per_object_colour(session):
    """F3: name:color syntax should propagate through the 2D path so
    visualisations of multi-part 2D drawings render in the requested hues."""
    execute_code(
        session,
        """
visible_a, _ = Box(20, 20, 5).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
visible_b, _ = Box(15, 15, 3).move(Location((25, 0, 0))).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible_a)), 'plate_a')
show(Compound(children=list(visible_b)), 'plate_b')
""",
    )
    out = render_view(session, objects="plate_a:red,plate_b:blue", format="svg")
    text = out["svg"].decode("utf-8", errors="ignore")
    # Red ≈ 255,0,0; blue ≈ 0,0,255 — SVG embeds explicit RGB on each layer
    assert "rgb(255,0,0)" in text or "rgb(255, 0, 0)" in text
    assert "rgb(0,0,255)" in text or "rgb(0, 0, 255)" in text


# --- #92 F4: colors= dict for fine-grained per-layer colour ---


def test_render_view_2d_colors_dict_overrides_per_object(session):
    """colors={'plate_a': 'red'} should win over both the default palette
    and any name:color syntax."""
    execute_code(
        session,
        """
visible_a, _ = Box(20, 20, 5).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
visible_b, _ = Box(15, 15, 3).move(Location((25, 0, 0))).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible_a)), 'plate_a')
show(Compound(children=list(visible_b)), 'plate_b')
""",
    )
    out = render_view(
        session,
        objects="plate_a,plate_b",  # no inline colours
        format="svg",
        colors={"plate_a": "red", "plate_b": "blue"},
    )
    text = out["svg"].decode("utf-8", errors="ignore")
    assert "rgb(255,0,0)" in text or "rgb(255, 0, 0)" in text
    assert "rgb(0,0,255)" in text or "rgb(0, 0, 255)" in text


def test_render_view_2d_colors_overrides_inline_namecolor(session):
    """colors dict beats inline name:color when both specify the same object."""
    execute_code(
        session,
        """
visible_a, _ = Box(20, 20, 5).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible_a)), 'plate_a')
""",
    )
    # Inline says red; colors dict says green — green should win
    out = render_view(
        session,
        objects="plate_a:red",
        format="svg",
        colors={"plate_a": "green"},
    )
    text = out["svg"].decode("utf-8", errors="ignore")
    # matplotlib 'green' = rgb(0, 128, 0) in standard colour names
    assert "rgb(0,128,0)" in text or "rgb(0, 128, 0)" in text
    # And red should NOT appear
    assert "rgb(255,0,0)" not in text and "rgb(255, 0, 0)" not in text


def test_render_view_2d_colors_dims_layer(session):
    """colors['_dims'] should change the dimensions/annotations colour."""
    _build_2d_drawing(session)
    out = render_view(session, objects="top_view", format="svg", colors={"_dims": "darkgreen"})
    text = out["svg"].decode("utf-8", errors="ignore")
    # matplotlib 'darkgreen' = rgb(0, 100, 0)
    assert "rgb(0,100,0)" in text or "rgb(0, 100, 0)" in text


# --- #92 F8: explicit mode= parameter + render_mode in result ---


def test_render_view_mode_auto_detects_3d(session):
    """mode='auto' (default) routes a 3D solid to the 3D pipeline."""
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", format="png")
    assert out.get("render_mode") == "3d"


def test_render_view_mode_auto_detects_2d(session):
    """mode='auto' routes a flat 2D Compound to the 2D pipeline."""
    _build_2d_drawing(session)
    out = render_view(session, objects="top_view", format="png")
    assert out.get("render_mode") == "2d"


def test_render_view_mode_2d_forced_errors_on_3d(session):
    """mode='2d' on a 3D solid raises a clear error."""
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="mode='2d'.*3D"):
        render_view(session, "iso", format="png", mode="2d")


def test_render_view_mode_3d_forced_errors_on_2d(session):
    """mode='3d' on a flat 2D Compound raises a clear error."""
    _build_2d_drawing(session)
    with pytest.raises(ValueError, match="mode='3d'.*flat 2D"):
        render_view(session, objects="top_view", format="png", mode="3d")


def test_render_view_mode_invalid_raises(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown mode"):
        render_view(session, "iso", format="png", mode="banana")


def test_render_view_2d_colors_unknown_name_falls_back_to_palette(session):
    """A colors dict that doesn't mention the object should fall back to
    the inline colour or palette — not crash."""
    execute_code(
        session,
        """
visible_a, _ = Box(20, 20, 5).project_to_viewport((0, 0, 100), (0, 1, 0), (0, 0, 0))
show(Compound(children=list(visible_a)), 'plate_a')
""",
    )
    out = render_view(
        session,
        objects="plate_a:red",
        format="svg",
        colors={"_dims": "purple"},  # nothing for 'plate_a'
    )
    text = out["svg"].decode("utf-8", errors="ignore")
    # Inline 'red' should still apply for plate_a
    assert "rgb(255,0,0)" in text or "rgb(255, 0, 0)" in text


# --- export 2D drafting ---


def test_export_2d_to_dxf(session, tmp_path, monkeypatch):
    """A 2D drawing exports cleanly to DXF via the export tool."""
    monkeypatch.chdir(tmp_path)
    _build_2d_drawing(session)
    export_file(session, "drawing", "dxf", object_name="top_view")
    assert (tmp_path / "drawing.dxf").exists()
    assert (tmp_path / "drawing.dxf").stat().st_size > 0
    text = (tmp_path / "drawing.dxf").read_text(errors="ignore")
    assert "SECTION" in text


def test_export_2d_to_svg(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _build_2d_drawing(session)
    export_file(session, "drawing", "svg", object_name="top_view")
    assert (tmp_path / "drawing.svg").exists()
    text = (tmp_path / "drawing.svg").read_text(errors="ignore")
    assert "<svg" in text


def test_export_2d_rejects_step(session, tmp_path, monkeypatch):
    """Trying to export a 2D drawing as STEP gives a clear error pointing at dxf/svg."""
    monkeypatch.chdir(tmp_path)
    _build_2d_drawing(session)
    with pytest.raises(ValueError, match="dxf.*svg|2D"):
        export_file(session, "drawing", "step", object_name="top_view")


def test_export_3d_rejects_dxf(session, tmp_path, monkeypatch):
    """Trying to export a 3D solid as DXF points at render_view(format='dxf') for the projection."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    with pytest.raises(ValueError, match="render_view"):
        export_file(session, "out", "dxf", object_name="cube")


# --- render_view labels ---


def test_render_view_label_objects_renders(session):
    execute_code(session, "show(Box(10, 10, 10), 'bracket')")
    execute_code(session, "show(Cylinder(3, 8).move(Location((20, 0, 0))), 'pin')")
    out = render_view(session, "iso", label_objects=True)
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_label_objects_skips_default_name(session):
    """Auto-named 'shape' (from bare current_shape) should not produce a label,
    but the render must still succeed."""
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", label_objects=True)
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_highlights_face(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    out = render_view(
        session,
        "iso",
        highlights=[{"object": "cube", "type": "face", "index": 0, "label": "top"}],
    )
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_highlights_edge_and_vertex(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    out = render_view(
        session,
        "iso",
        highlights=[
            {"object": "cube", "type": "edge", "index": 2, "label": "e2"},
            {"object": "cube", "type": "vertex", "index": 0, "label": "v0"},
        ],
    )
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_highlight_unknown_object_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    with pytest.raises(ValueError, match="unknown object 'ghost'"):
        render_view(
            session,
            "iso",
            highlights=[{"object": "ghost", "type": "face", "index": 0, "label": "x"}],
        )


def test_render_view_highlight_object_not_in_render_set_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    execute_code(session, "show(Cylinder(3, 5), 'pin')")
    with pytest.raises(ValueError, match="not in the rendered set"):
        render_view(
            session,
            "iso",
            objects="cube",
            highlights=[{"object": "pin", "type": "face", "index": 0, "label": "x"}],
        )


def test_render_view_highlight_index_out_of_range_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    with pytest.raises(ValueError, match="out of range"):
        render_view(
            session,
            "iso",
            highlights=[{"object": "cube", "type": "face", "index": 99, "label": "x"}],
        )


def test_render_view_highlight_invalid_type_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    with pytest.raises(ValueError, match="type must be"):
        render_view(
            session,
            "iso",
            highlights=[{"object": "cube", "type": "wire", "index": 0, "label": "x"}],
        )


def test_render_view_highlight_missing_key_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    with pytest.raises(ValueError, match="missing required key"):
        render_view(
            session,
            "iso",
            highlights=[{"object": "cube", "type": "face", "index": 0}],  # no label
        )


def test_render_view_labels_warn_in_svg(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    out = render_view(session, "iso", format="svg", label_objects=True)
    assert "svg" in out and "png" not in out
    assert "label_warnings" in out
    assert any("PNG" in w for w in out["label_warnings"])


# --- measure ---


def test_measure_bounding_box(session):
    execute_code(session, "result = Box(10, 20, 30)")
    data = json.loads(measure(session))
    assert abs(data["bbox"]["xsize"] - 10) < 0.01
    assert abs(data["bbox"]["ysize"] - 20) < 0.01
    assert abs(data["bbox"]["zsize"] - 30) < 0.01


def test_measure_center_origin(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    center = data["bbox"]["center"]
    assert abs(center["x"]) < 0.01
    assert abs(center["y"]) < 0.01
    assert abs(center["z"]) < 0.01


def test_measure_volume(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    assert abs(data["volume"] - 1000) < 0.1


def test_measure_area(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    assert abs(data["area"] - 600) < 0.1


def test_measure_clearance(session):
    from build123d_mcp.tools.measure import clearance

    execute_code(
        session, "show(Box(10,10,10), 'a')\nshow(Box(10,10,10).move(Location((20,0,0))), 'b')"
    )
    data = json.loads(clearance(session, "a", "b"))
    assert abs(data["clearance"] - 10) < 0.1


def test_clearance_unknown_object_raises(session):
    from build123d_mcp.tools.measure import clearance

    execute_code(session, "show(Box(10,10,10), 'a')")
    with pytest.raises(ValueError, match="Unknown object"):
        clearance(session, "a", "missing")


def test_clearance_apart_status(session):
    """Two boxes with a gap: status=apart, clearance is the gap."""
    from build123d_mcp.tools.measure import clearance

    execute_code(session, "show(Box(10,10,10), 'a')")
    execute_code(session, "show(Box(5,5,5).move(Location((20,0,0))), 'b')")
    data = json.loads(clearance(session, "a", "b"))
    assert data["status"] == "apart"
    assert data["containment"] == "neither"
    assert data["clearance"] > 0
    assert data["intersection_volume"] == 0.0


def test_clearance_touching_status(session):
    """Two boxes sharing a face: status=touching, clearance=0, no overlap."""
    from build123d_mcp.tools.measure import clearance

    execute_code(session, "show(Box(10,10,10), 'a')")
    execute_code(session, "show(Box(10,10,10).move(Location((10,0,0))), 'b')")
    data = json.loads(clearance(session, "a", "b"))
    assert data["status"] == "touching"
    assert data["clearance"] < 1e-6
    assert data["intersection_volume"] < 1e-6


def test_clearance_containing_reports_wall_thickness(session):
    """Pocket-style: cylinder fully inside plate. status=containing,
    clearance is the smallest gap from cylinder surface to plate exterior
    (= wall thickness in the worst direction)."""
    from build123d_mcp.tools.measure import clearance

    execute_code(session, "show(Box(40, 20, 10), 'plate')")
    execute_code(session, "show(Cylinder(3, 5).move(Location((10, 0, 0))), 'pocket')")
    data = json.loads(clearance(session, "pocket", "plate"))
    assert data["status"] == "containing"
    assert data["containment"] == "a_in_b"  # pocket inside plate
    # Wall thickness: pocket z range [-2.5, 2.5], plate z range [-5, 5] → gap 2.5 each side
    assert abs(data["clearance"] - 2.5) < 0.01
    assert data["intersection_volume"] > 0  # pocket overlaps with plate by its own volume
    assert data["a_volume_outside_b"] < 1e-6  # pocket entirely inside plate


def test_clearance_interpenetrating_flags_wall_pierce(session):
    """The wall-piercing scenario: pocket pokes out of plate. The signal that
    matters is a_volume_outside_b > 0 — that's the volume of the pocket
    extending beyond the plate's outer surface."""
    from build123d_mcp.tools.measure import clearance

    execute_code(session, "show(Box(40, 20, 5), 'plate')")
    execute_code(session, "show(Cylinder(3, 10).move(Location((10, 0, 0))), 'pocket')")
    data = json.loads(clearance(session, "pocket", "plate"))
    assert data["status"] == "interpenetrating"
    assert data["containment"] == "neither"
    assert data["clearance"] < 1e-6
    assert data["intersection_volume"] > 0
    assert data["a_volume_outside_b"] > 0  # pocket pierces plate


# --- export ---


def test_export_step(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    result = export_file(session, "out", "step")
    assert os.path.exists("out.step")
    assert os.path.getsize("out.step") > 0
    assert "Exported" in result


def test_export_stl(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    export_file(session, "out", "stl")
    assert os.path.exists("out.stl")
    assert os.path.getsize("out.stl") > 0


def test_export_multi_format(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    result = export_file(session, "out", "step,stl")
    assert os.path.exists("out.step")
    assert os.path.exists("out.stl")
    assert os.path.getsize("out.step") > 0
    assert os.path.getsize("out.stl") > 0
    assert ".step" in result
    assert ".stl" in result


def test_export_echoes_sanity_line_3d(session, tmp_path, monkeypatch):
    """Export result reports volume/bbox/faces of the written solid (#241)."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    result = export_file(session, "out", "step")
    assert "volume 1000" in result
    assert "10×10×10 mm" in result
    assert "6 faces" in result


def test_export_echoes_sanity_line_2d(session, tmp_path, monkeypatch):
    """2D exports report bbox/edge count instead of volume (#241)."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Rectangle(20, 10), 'sk')")
    result = export_file(session, "out", "dxf", object_name="sk")
    assert "2D drawing" in result
    assert "edges" in result


def test_export_invalid_format(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown format"):
        export_file(session, "out", "obj")


def test_export_path_traversal_rejected(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        export_file(session, "../../etc/passwd", "step")


def test_export_outside_roots_rejected(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        export_file(session, _OUTSIDE_ROOT_PATH, "step")


def test_export_to_tmp_allowed(session, tmp_path):
    execute_code(session, "result = Box(10, 10, 10)")
    target = tmp_path / "exported.step"
    result = export_file(session, str(target), "step")
    assert os.path.exists(target)
    assert str(target) in result


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="/tmp does not exist on Windows; tempfile.gettempdir() coverage in test_export_to_tmp_allowed already runs there",
)
def test_export_to_slash_tmp_allowed(session):
    execute_code(session, "result = Box(10, 10, 10)")
    import tempfile as _tempfile

    with _tempfile.NamedTemporaryFile(
        prefix="build123d_mcp_test_", suffix=".step", dir="/tmp", delete=False
    ) as f:
        target = f.name
    try:
        export_file(session, target, "step")
        assert os.path.exists(target)
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_export_step_named_object_carries_label(session, tmp_path, monkeypatch):
    """A single named object exported to STEP should carry its session name as the label."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'bracket')")
    export_file(session, "out", "step", object_name="bracket")
    content = (tmp_path / "out.step").read_text()
    assert "bracket" in content


def test_export_step_star_carries_assembly_and_child_labels(session, tmp_path, monkeypatch):
    """Exporting * should produce a STEP file with the assembly label and each child name."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'bracket')")
    execute_code(session, "show(Cylinder(2, 8).move(Location((20, 0, 0))), 'pin')")
    export_file(session, "out", "step", object_name="*")
    content = (tmp_path / "out.step").read_text()
    assert "bracket" in content
    assert "pin" in content
    assert "assembly" in content


def test_export_step_does_not_mutate_session_shapes(session, tmp_path, monkeypatch):
    """Setting labels for export must not leak back into session.objects."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'bracket')")
    original_label = getattr(session.objects["bracket"], "label", None)
    export_file(session, "out", "step", object_name="bracket")
    assert getattr(session.objects["bracket"], "label", None) == original_label


def test_export_symlink_escape_rejected(session, tmp_path):
    execute_code(session, "result = Box(10, 10, 10)")
    link = tmp_path / "escape.step"
    os.symlink("/etc/passwd", link)
    try:
        with pytest.raises(ValueError, match="outside the allowed write roots"):
            export_file(session, str(link), "step")
    finally:
        if link.is_symlink():
            link.unlink()


# --- reset ---


def test_reset_clears_shape(session):
    execute_code(session, "result = Box(10, 10, 10)")
    assert session.current_shape is not None
    session.reset()
    assert session.current_shape is None


def test_reset_clears_namespace(session):
    execute_code(session, "x = 99")
    session.reset()
    assert "x" not in session.namespace
    assert "show" in session.namespace


# --- multi-object / show() ---


def test_show_registers_object(session):
    execute_code(session, "box = Box(10, 10, 10)")
    session.namespace["show"](session.current_shape, "mybox")
    assert "mybox" in session.objects


def test_show_callable_from_execute(session):
    execute_code(session, "box = Box(10, 10, 10)\nshow(box, 'mybox')")
    assert "mybox" in session.objects


def test_reset_clears_objects(session):
    execute_code(session, "show(Box(5, 5, 5), 'b')")
    assert session.objects
    session.reset()
    assert not session.objects


def test_show_available_after_reset(session):
    session.reset()
    assert "show" in session.namespace


def test_render_view_multiple_objects(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')\nshow(Cylinder(5, 20), 'b')")
    out = render_view(session, "iso")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_named_subset(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')\nshow(Cylinder(5, 20), 'b')")
    out = render_view(session, "iso", "a")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_unknown_object_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')")
    with pytest.raises(ValueError, match="Unknown object"):
        render_view(session, "iso", "missing")


def test_measure_named_object(session):
    execute_code(session, "show(Box(30, 10, 10), 'wide')")
    data = json.loads(measure(session, "wide"))
    assert abs(data["bbox"]["xsize"] - 30) < 0.01


def test_measure_unknown_object_raises(session):
    execute_code(session, "show(Box(5, 5, 5), 'a')")
    with pytest.raises(ValueError, match="Unknown object"):
        measure(session, "missing")


def test_export_named_object(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    result = export_file(session, "out", "step", "part")
    assert os.path.exists("out.step")
    assert "Exported" in result


def test_export_unknown_object_raises(session):
    execute_code(session, "show(Box(5, 5, 5), 'a')")
    with pytest.raises(ValueError, match="Unknown object"):
        export_file(session, "out", "step", "missing")


# --- snapshots ---


def test_save_and_restore_snapshot(session):
    execute_code(session, "result = Box(10, 10, 10)")
    vol_before = session.current_shape.volume
    session.save_snapshot("v1")
    execute_code(session, "result = Box(99, 99, 99)")
    assert session.current_shape.volume != vol_before
    session.restore_snapshot("v1")
    assert abs(session.current_shape.volume - vol_before) < 0.01


def test_snapshot_deep_copies_shape(session):
    execute_code(session, "result = Box(10, 10, 10)")
    shape_ref = session.current_shape
    session.save_snapshot("v1")
    # snapshot must hold a copy, not the original reference
    assert session.snapshots["v1"]["current_shape"] is not shape_ref


def test_snapshot_captures_objects_registry(session):
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    session.save_snapshot("s1")
    execute_code(session, "show(Box(5, 5, 5), 'extra')")
    assert "extra" in session.objects
    session.restore_snapshot("s1")
    assert "extra" not in session.objects
    assert "part" in session.objects


def test_restore_unknown_snapshot_raises(session):
    with pytest.raises(KeyError, match="no_such"):
        session.restore_snapshot("no_such")


def test_reset_clears_snapshots(session):
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("v1")
    session.reset()
    assert not session.snapshots


def test_dispatch_save_snapshot_no_shape_omits_current_shape():
    from build123d_mcp.session import Session
    from build123d_mcp.worker import _dispatch

    s = Session()
    msg = _dispatch(s, "save_snapshot", {"name": "empty"}, None)
    assert "current_shape" not in msg
    assert "none" in msg.lower()


def test_dispatch_restore_snapshot_no_shape_omits_current_shape():
    from build123d_mcp.session import Session
    from build123d_mcp.worker import _dispatch

    s = Session()
    _dispatch(s, "save_snapshot", {"name": "empty"}, None)
    msg = _dispatch(s, "restore_snapshot", {"name": "empty"}, None)
    assert "current_shape" not in msg
    assert "none" in msg.lower()


def test_namespace_preserved_after_restore(session):
    execute_code(session, "x = 42")
    session.save_snapshot("s1")
    execute_code(session, "x = 99")
    session.restore_snapshot("s1")
    # namespace is NOT restored — x stays at 99
    assert session.namespace.get("x") == 99


# --- show() API: shape first, optional name second (issue #11) ---


def test_show_with_explicit_name(session):
    execute_code(session, "box = Box(10, 10, 10)")
    session.namespace["show"](session.current_shape, "mybox")
    assert "mybox" in session.objects


def test_show_with_explicit_name_from_execute(session):
    execute_code(session, "box = Box(10, 10, 10)\nshow(box, 'mybox')")
    assert "mybox" in session.objects


def test_show_without_name_defaults_to_shape(session):
    execute_code(session, "box = Box(10, 10, 10)\nshow(box)")
    assert "shape" in session.objects


# --- render_view per-object color (issue #12) ---


def test_render_view_per_object_color(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')\nshow(Cylinder(5, 20), 'b')")
    out = render_view(session, "iso", "a:blue,b:red")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_mixed_color_and_palette(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')\nshow(Cylinder(5, 20), 'b')")
    out = render_view(session, "iso", "a:green,b")
    assert out["png"][:8] == PNG_MAGIC


# --- interference check (issue #13) ---


def test_interference_overlapping(session):
    execute_code(
        session, "show(Box(10, 10, 10), 'a')\nshow(Box(10, 10, 10).move(Location((5, 0, 0))), 'b')"
    )
    data = json.loads(interference(session, "a", "b"))
    assert data["interferes"] is True
    assert data["volume"] > 0


def test_interference_non_overlapping(session):
    execute_code(
        session, "show(Box(10, 10, 10), 'a')\nshow(Box(10, 10, 10).move(Location((20, 0, 0))), 'b')"
    )
    data = json.loads(interference(session, "a", "b"))
    assert data["interferes"] is False
    assert data["volume"] == 0.0


def test_interference_returns_bounds(session):
    execute_code(
        session, "show(Box(10, 10, 10), 'a')\nshow(Box(10, 10, 10).move(Location((5, 0, 0))), 'b')"
    )
    data = json.loads(interference(session, "a", "b"))
    assert "bounds" in data
    bounds = data["bounds"]
    for key in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"):
        assert key in bounds


def test_interference_unknown_object_raises(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')")
    with pytest.raises(ValueError, match="Unknown object"):
        interference(session, "a", "missing")


# --- measure topology ---


def test_measure_topology_box(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    assert data["topology"]["faces"] == 6
    assert data["topology"]["edges"] == 12
    assert data["topology"]["vertices"] == 8


def test_measure_topology_increases_after_boolean_cut(session):
    execute_code(session, "result = Box(20, 20, 20) - Cylinder(3, 30)")
    data = json.loads(measure(session))
    assert data["topology"]["faces"] > 6


def test_measure_topology_named_object(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    data = json.loads(measure(session, "cube"))
    assert data["topology"]["faces"] == 6


# --- render_view azimuth/elevation (new) ---


def test_render_view_azimuth_returns_png(session):
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", azimuth=45.0)
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_elevation_returns_png(session):
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", elevation=30.0)
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_azimuth_and_elevation_returns_png(session):
    execute_code(session, "result = Cylinder(5, 20)")
    out = render_view(session, "front", azimuth=20.0, elevation=15.0)
    assert out["png"][:8] == PNG_MAGIC


# --- render_view clip_at (new) ---


def test_render_view_clip_at_returns_png(session):
    execute_code(session, "result = Cylinder(5, 20)")
    out = render_view(session, "iso", clip_plane="z", clip_at=5.0)
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_clip_at_negative_returns_png(session):
    execute_code(session, "result = Box(20, 20, 20)")
    out = render_view(session, "iso", clip_plane="x", clip_at=-3.0)
    assert out["png"][:8] == PNG_MAGIC


# --- render_view save_to (new) ---


def test_render_view_save_to_writes_png(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    out = render_view(session, "iso", save_to="out")
    assert out["png"][:8] == PNG_MAGIC
    assert os.path.exists("out.png")
    assert os.path.getsize("out.png") > 0


def test_render_view_save_to_with_extension(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "result = Box(10, 10, 10)")
    render_view(session, "iso", save_to="out.png")
    assert os.path.exists("out.png")


def test_render_view_save_to_path_traversal_rejected(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        render_view(session, "iso", save_to="../../etc/passwd")


def test_render_view_save_to_outside_roots_rejected(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="outside the allowed write roots"):
        render_view(session, "iso", save_to=_OUTSIDE_ROOT_PATH)


def test_render_view_save_to_tmp_allowed(session, tmp_path):
    execute_code(session, "result = Box(10, 10, 10)")
    target = tmp_path / "render.png"
    render_view(session, "iso", save_to=str(target))
    assert os.path.exists(target)


# --- show() feedback (new) ---


def test_show_prints_volume_and_faces(session):
    output = execute_code(session, "show(Box(10, 10, 10), 'cube')")
    assert "cube" in output
    assert "volume" in output.lower() or "mm" in output


def test_show_prints_feedback_without_name(session):
    output = execute_code(session, "show(Box(5, 5, 5))")
    assert "Registered" in output


# --- face() helper ---


def test_face_top_returns_highest_z(session):
    execute_code(session, "from build123d import *\nb = Box(10, 10, 10)\nt = named_face(b, 'top')")
    # Top face center should be at z = +5
    top = session.namespace["t"]
    assert abs(top.center_location.position.Z - 5) < 0.1


def test_face_bottom_returns_lowest_z(session):
    execute_code(
        session, "from build123d import *\nb = Box(10, 10, 10)\nt = named_face(b, 'bottom')"
    )
    bottom = session.namespace["t"]
    assert abs(bottom.center_location.position.Z + 5) < 0.1


def test_face_all_names_work(session):
    code = (
        "from build123d import *\n"
        "b = Box(10, 20, 30)\n"
        "top = named_face(b, 'top')\n"
        "bottom = named_face(b, 'bottom')\n"
        "front = named_face(b, 'front')\n"
        "back = named_face(b, 'back')\n"
        "right = named_face(b, 'right')\n"
        "left = named_face(b, 'left')\n"
    )
    result = execute_code(session, code)
    assert "Error" not in result


def test_face_unknown_name_raises(session):
    result = execute_code(
        session, "from build123d import *\nb = Box(10,10,10)\nnamed_face(b, 'diagonal')"
    )
    assert "Error" in result and "diagonal" in result


def test_face_survives_rollback(session):
    # face() should still be available after a failed execute()
    execute_code(session, "raise ValueError('oops')")
    result = execute_code(
        session, "from build123d import *\nb = Box(5,5,5)\nt = named_face(b, 'top')"
    )
    assert "Error" not in result


def test_face_available_without_import(session):
    # face() is a session built-in — no import needed
    result = execute_code(
        session, "from build123d import Box\nb = Box(10,10,10)\nt = named_face(b, 'top')"
    )
    assert "Error" not in result


# --- list_objects (new) ---


def test_list_objects_empty(session):
    result = list_objects(session)
    assert "No named objects" in result


def test_list_objects_returns_all_shapes(session):
    execute_code(session, "show(Box(10, 10, 10), 'a')\nshow(Cylinder(5, 20), 'b')")
    data = json.loads(list_objects(session))
    names = [item["name"] for item in data]
    assert "a" in names
    assert "b" in names


def test_list_objects_includes_geometry(session):
    execute_code(session, "show(Box(10, 10, 10), 'cube')")
    data = json.loads(list_objects(session))
    cube = next(item for item in data if item["name"] == "cube")
    assert abs(cube["volume"] - 1000) < 0.1
    assert cube["faces"] == 6
    assert cube["edges"] == 12
    assert cube["vertices"] == 8


# --- auto-diagnostics after execute ---


def test_execute_auto_diagnostics_on_new_shape(session):
    result = execute_code(session, "result = Box(10, 10, 10)")
    assert "current_shape" in result
    assert "volume" in result
    assert "mm³" in result


def test_execute_auto_diagnostics_includes_topology(session):
    result = execute_code(session, "result = Box(10, 10, 10)")
    assert "6f" in result


def test_execute_auto_diagnostics_not_shown_without_shape(session):
    result = execute_code(session, "x = 42")
    assert "current_shape" not in result


def test_execute_auto_diagnostics_not_shown_on_error(session):
    result = execute_code(session, "result = Box(10, 10, 10)\nraise ValueError('oops')")
    assert "Error" in result
    assert "current_shape" not in result


def test_execute_auto_diagnostics_not_repeated_when_shape_unchanged(session):
    execute_code(session, "result = Box(10, 10, 10)")
    result = execute_code(session, "x = 99")
    assert "current_shape" not in result


# --- assertion / constraint support ---


def test_assert_passing_does_not_error(session):
    result = execute_code(session, "result = Box(10, 10, 10)\nassert result.volume > 500")
    assert "Error" not in result
    assert "Constraint" not in result


def test_assert_failing_returns_constraint_failed(session):
    result = execute_code(
        session, "result = Box(10, 10, 10)\nassert result.volume > 9999, 'volume too small'"
    )
    assert result.startswith("Constraint failed")
    assert "volume too small" in result


def test_assert_failing_with_no_message(session):
    result = execute_code(session, "assert False")
    assert "Constraint failed" in result


def test_assert_failure_does_not_update_current_shape(session):
    execute_code(session, "result = Box(10, 10, 10)")
    shape_before = session.current_shape
    execute_code(session, "result = Box(5, 5, 5)\nassert False")
    assert session.current_shape is shape_before


# --- diff_snapshot ---


def test_diff_snapshot_vs_current(session):
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("v1")
    execute_code(session, "result = Box(10, 10, 5)")
    result = diff_snapshot(session, "v1")
    assert "v1" in result
    assert "current" in result
    assert "volume" in result


def test_diff_snapshot_shows_volume_delta(session):
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("before")
    execute_code(session, "result = Box(10, 10, 5)")
    result = diff_snapshot(session, "before")
    assert "Δ" in result
    assert "-500" in result or "500" in result


def test_diff_snapshot_two_named_snapshots(session):
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("v1")
    execute_code(session, "result = Box(10, 10, 20)")
    session.save_snapshot("v2")
    result = diff_snapshot(session, "v1", "v2")
    assert "v1" in result
    assert "v2" in result


def test_diff_snapshot_shows_added_object(session):
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("before")
    execute_code(session, "show(Cylinder(5, 20), 'pin')")
    result = diff_snapshot(session, "before")
    assert "+ pin" in result or "pin (added)" in result


def test_diff_snapshot_shows_removed_object(session):
    execute_code(session, "show(Box(10, 10, 10), 'bracket')")
    session.save_snapshot("with_bracket")
    session.objects.clear()
    result = diff_snapshot(session, "with_bracket")
    assert "bracket" in result
    assert "removed" in result


def test_diff_snapshot_unknown_snapshot_a(session):
    result = diff_snapshot(session, "no_such")
    assert "Error" in result


def test_diff_snapshot_unchanged_object_marked(session):
    execute_code(session, "show(Box(10, 10, 10), 'base')")
    session.save_snapshot("s1")
    session.save_snapshot("s2")
    result = diff_snapshot(session, "s1", "s2")
    assert "unchanged" in result or "= base" in result


# --- session_state ---


def test_session_state_empty(session):
    data = json.loads(session_state(session))
    assert data["current_shape"] is None
    assert data["objects"] == {}
    assert data["snapshots"] == []


def test_session_state_with_shape_and_objects(session):
    execute_code(session, "result = Box(10, 10, 10)")
    execute_code(session, "show(Cylinder(5, 20), 'pin')")
    session.save_snapshot("v1")
    data = json.loads(session_state(session))
    assert data["current_shape"]["volume"] > 0
    assert "pin" in data["objects"]
    assert "v1" in data["snapshots"]


def test_session_state_variables_exclude_injected_helpers(session):
    # The injected helpers (show, annotate, named_face, ...) live in the user
    # namespace but are session plumbing, not user variables (issue #219).
    from build123d_mcp.session import _INJECTED

    execute_code(session, "x = 5")
    variables = json.loads(session_state(session))["variables"]
    assert "x" in variables
    assert not (_INJECTED & set(variables))


# --- diff_snapshot JSON mode ---


def test_diff_snapshot_json_format(session):
    execute_code(session, "result = Box(10, 10, 10)")
    session.save_snapshot("s1")
    execute_code(session, "result = Box(20, 20, 20)")
    data = json.loads(diff_snapshot(session, "s1", format="json"))
    assert data["a"]["label"] == "s1"
    assert data["b"]["label"] == "current"
    assert data["b"]["current_shape"]["volume"] > data["a"]["current_shape"]["volume"]


# --- measure extended fields (center_of_mass, inertia, face_inventory) ---


def test_measure_center_of_mass_symmetric_box(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    com = data["center_of_mass"]
    assert abs(com["x"]) < 0.01
    assert abs(com["y"]) < 0.01
    assert abs(com["z"]) < 0.01


def test_measure_center_of_mass_offset_box(session):
    execute_code(session, "result = Box(10, 10, 10).move(Location((5, 5, 5)))")
    data = json.loads(measure(session))
    com = data["center_of_mass"]
    assert abs(com["x"] - 5) < 0.1
    assert abs(com["y"] - 5) < 0.1
    assert abs(com["z"] - 5) < 0.1


def test_measure_inertia_returns_six_components(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    inertia = data["inertia"]
    for key in ("Ixx", "Iyy", "Izz", "Ixy", "Ixz", "Iyz"):
        assert key in inertia
    assert abs(inertia["Ixx"] - inertia["Iyy"]) < 1.0
    assert abs(inertia["Ixx"] - inertia["Izz"]) < 1.0
    assert abs(inertia["Ixy"]) < 1.0


def test_measure_inertia_differs_for_different_shapes(session):
    execute_code(session, "show(Box(10,10,10), 'cube')\nshow(Cylinder(5, 20), 'cyl')")
    cube_data = json.loads(measure(session, "cube"))
    cyl_data = json.loads(measure(session, "cyl"))
    assert abs(cube_data["inertia"]["Ixx"] - cyl_data["inertia"]["Ixx"]) > 1.0


def test_measure_face_inventory_box(session):
    """Six identical 100 mm² planes collapse to one entry with count=6 (#238)."""
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session))
    fi = data["face_inventory"]
    assert isinstance(fi, list)
    assert len(fi) == 1
    assert fi[0]["type"] == "Plane"
    assert abs(fi[0]["area"] - 100) < 1
    assert fi[0]["count"] == 6


def test_condense_inventory_folds_non_analytic_slivers():
    """Thread-fade sliver BSplines fold into one summary line; analytic faces
    are kept verbatim however small; identical entries aggregate (#238)."""
    from build123d_mcp.tools.measure import _condense_inventory

    faces = (
        [{"type": "BSpline", "area": 0.001} for _ in range(23)]
        + [{"type": "BSpline", "area": 22.17} for _ in range(6)]
        + [{"type": "Cylinder", "area": 50.0, "diameter": 6.6, "axis": (0, 0, 1)}]
        + [{"type": "Plane", "area": 0.001}]
    )
    out = _condense_inventory(faces)
    big_bsplines = [f for f in out if f["type"] == "BSpline"]
    assert len(big_bsplines) == 1 and big_bsplines[0]["count"] == 6
    assert [f for f in out if f["type"] == "Cylinder"]
    assert [f for f in out if f["type"] == "Plane"]  # analytic sliver survives
    folded = [f for f in out if f["type"] == "slivers_folded"]
    assert len(folded) == 1 and folded[0]["count"] == 23
    assert folded[0]["total_area"] == pytest.approx(0.023)


def test_measure_density_reports_mass(session):
    """volume mm³ × g/cm³ / 1000 = grams (#237)."""
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session, density=7.85))
    assert data["density_g_cm3"] == 7.85
    assert data["mass_g"] == pytest.approx(7.85, rel=1e-3)
    assert data["inertia_units"] == "g·mm²"


def test_measure_material_preset(session):
    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(measure(session, material="brass"))
    assert data["mass_g"] == pytest.approx(8.5, rel=1e-3)


def test_measure_density_scales_inertia(session):
    """With density the volume inertia (mm⁵) becomes mass inertia (g·mm²)."""
    execute_code(session, "result = Box(10, 10, 10)")
    plain = json.loads(measure(session))
    dense = json.loads(measure(session, density=2.0))
    assert plain["inertia_units"].startswith("mm⁵")
    assert dense["inertia"]["Ixx"] == pytest.approx(plain["inertia"]["Ixx"] * 2.0 / 1000, rel=1e-3)


def test_measure_density_and_material_conflict(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="not both"):
        measure(session, density=1.0, material="steel")


def test_measure_unknown_material_raises(session):
    execute_code(session, "result = Box(10, 10, 10)")
    with pytest.raises(ValueError, match="Unknown material"):
        measure(session, material="unobtainium")


def test_measure_face_inventory_cylinder(session):
    execute_code(session, "result = Cylinder(5, 10)")
    data = json.loads(measure(session))
    fi = data["face_inventory"]
    types = [f["type"] for f in fi]
    assert "Cylinder" in types
    assert "Plane" in types
    cyl_faces = [f for f in fi if f["type"] == "Cylinder"]
    assert len(cyl_faces) == 1
    assert abs(cyl_faces[0]["diameter"] - 10) < 0.1


# --- cross_sections ---


def test_cross_sections_box(session):
    from build123d_mcp.tools.cross_sections import cross_sections

    execute_code(session, "result = Box(10, 10, 10)")
    data = json.loads(cross_sections(session, axis="Z", num_slices=5))
    assert isinstance(data, list)
    assert len(data) == 5
    for s in data:
        assert "position" in s and "area" in s
        assert abs(s["area"] - 100) < 2.0


def test_cross_sections_named_object(session):
    from build123d_mcp.tools.cross_sections import cross_sections

    execute_code(session, "show(Box(20, 10, 10), 'wide')")
    data = json.loads(cross_sections(session, "wide", axis="X", num_slices=4))
    assert len(data) == 4
    for s in data:
        assert abs(s["area"] - 100) < 2.0


# --- import_cad_file ---


def test_import_step_round_trip(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(10, 10, 10)")
    export_file(session, "ref", "step")
    step_path = str(tmp_path / "ref.step")
    data = json.loads(import_cad_file(session, step_path, "reference"))
    assert data["imported"] == "reference"
    assert data["format"] == "step"
    assert abs(data["volume"] - 1000) < 1
    assert data["faces"] == 6
    assert "reference" in session.objects


def test_import_step_default_name_from_filename(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(5, 5, 5)")
    export_file(session, "mypart", "step")
    step_path = str(tmp_path / "mypart.step")
    data = json.loads(import_cad_file(session, step_path))
    assert data["imported"] == "mypart"
    assert "mypart" in session.objects


def test_import_stl_round_trip(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(10, 10, 10)")
    export_file(session, "ref", "stl")
    stl_path = str(tmp_path / "ref.stl")
    data = json.loads(import_cad_file(session, stl_path, "ref_stl"))
    assert data["imported"] == "ref_stl"
    assert data["format"] == "stl"
    # STL is a triangulated mesh face — volume=0 is expected; check bbox instead
    assert data["bbox"]["xsize"] > 0
    assert "ref_stl" in session.objects


def test_import_cad_file_missing_file_raises(session, tmp_path):
    from build123d_mcp.tools.import_step import import_cad_file

    # Missing file under an allowed root: passes the input-path policy check
    # and reaches the not-found check (an outside-root path would be rejected
    # earlier with "outside the allowed read roots" — see test_path_safety.py).
    with pytest.raises(ValueError, match="File not found"):
        import_cad_file(session, str(tmp_path / "missing.step"))


def test_import_cad_file_wrong_extension_raises(session, tmp_path):
    from build123d_mcp.tools.import_step import import_cad_file

    bad = tmp_path / "file.obj"
    bad.write_text("dummy")
    with pytest.raises(ValueError, match="Expected a .step"):
        import_cad_file(session, str(bad))


def test_import_step_becomes_current_shape(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(10, 10, 10)")
    export_file(session, "shape", "step")
    import_cad_file(session, str(tmp_path / "shape.step"), "imported")
    assert session.current_shape is not None
    assert "imported" in session.objects


# --- render_view after import ---


def test_render_view_after_step_import(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(10, 10, 10)")
    export_file(session, str(tmp_path / "ref"), "step")
    session.reset()
    execute_code(session, "from build123d import *")
    import_cad_file(session, str(tmp_path / "ref.step"), "imported")
    out = render_view(session, "iso")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_after_stl_import(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(10, 10, 10)")
    export_file(session, str(tmp_path / "ref"), "stl")
    session.reset()
    execute_code(session, "from build123d import *")
    import_cad_file(session, str(tmp_path / "ref.stl"), "imported")
    out = render_view(session, "iso")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_imported_by_name(session, tmp_path, monkeypatch):
    """Rendering a specific imported object by name avoids Z-fighting with other shapes."""
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "show(Box(10, 10, 10), 'original')")
    export_file(session, str(tmp_path / "ref"), "step")
    import_cad_file(session, str(tmp_path / "ref.step"), "imported")
    out = render_view(session, "iso", objects="imported")
    assert out["png"][:8] == PNG_MAGIC


def test_render_view_stl_import_non_trivial(session, tmp_path, monkeypatch):
    """Regression: STL-imported shells must produce a shaded render, not an all-black image.
    The vtkPolyDataNormals fix ensures consistent face orientation on mesh shells."""
    monkeypatch.chdir(tmp_path)
    from build123d_mcp.tools.import_step import import_cad_file

    execute_code(session, "result = Box(20, 20, 20)")
    export_file(session, str(tmp_path / "box"), "stl")
    session.reset()
    execute_code(session, "from build123d import *")
    import_cad_file(session, str(tmp_path / "box.stl"), "mesh")
    out = render_view(session, "iso")
    assert out["png"][:8] == PNG_MAGIC
    # A properly shaded render is non-trivial. An all-black 800×600 PNG compresses to
    # well under 1 KB; 2000 bytes is a safe floor that catches empty/uniform renders
    # while tolerating platform differences in VTK tessellation complexity.
    assert len(out["png"]) > 2000, "PNG suspiciously small — likely an all-black or empty render"


# --- health_check ---


def test_health_check_returns_json(session):
    data = json.loads(health_check(session))
    assert "ok" in data
    assert "export_step" in data
    assert "export_stl" in data
    assert "render_svg" in data
    assert data["export_step"]["ok"] is True
    assert data["export_stl"]["ok"] is True
    assert data["render_svg"]["ok"] is True


def test_version_returns_package_versions():
    """version_info() reports build123d-mcp and its key deps, keyed by dist name."""
    from build123d_mcp.tools.version import version_info

    info = version_info()
    assert isinstance(info, dict)
    for key in (
        "build123d-mcp",
        "build123d",
        "build123d-drafting-helpers",
        "bd_warehouse",
        "augura",
    ):
        assert key in info, f"version_info() missing key {key!r}"
        assert isinstance(info[key], str) and info[key], f"empty version for {key}"
    # Companion packages are hard deps of this distribution — they must report
    # a real version, otherwise the #240 discoverability promise is broken.
    assert info["bd_warehouse"] != "unknown"
    assert info["augura"] != "unknown"


def test_version_build123d_mcp_matches_metadata():
    """The reported build123d-mcp version matches importlib metadata."""
    from importlib.metadata import version as _pkg_version

    from build123d_mcp.tools.version import version_info

    assert version_info()["build123d-mcp"] == _pkg_version("build123d-mcp")


def test_version_unknown_package_reports_unknown(monkeypatch):
    """A package that isn't installed reports "unknown" rather than raising."""
    import build123d_mcp.tools.version as v

    monkeypatch.setattr(v, "_PACKAGES", ("build123d-mcp", "definitely-not-installed-xyz"))
    info = v.version_info()
    assert info["definitely-not-installed-xyz"] == "unknown"
    assert info["build123d-mcp"] != "unknown"


# ---------------------------------------------------------------------------
# analyze_printability
# ---------------------------------------------------------------------------


def test_analyze_printability_clean_box(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    session.execute("from build123d import *\nshow(Box(10, 10, 5), 'box')")
    result = analyze_printability(session, object_name="box")
    assert "finding" in result
    data = json.loads(result.split("\n\n", 1)[1])
    assert "findings" in data


def test_analyze_printability_uses_current_shape(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    session.execute("from build123d import *\nresult = Box(10, 10, 5)")
    result = analyze_printability(session)
    assert "finding" in result


def test_analyze_printability_detects_overhang(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    # T-shape: thin post with a wide slab on top. The underside of the slab
    # is a horizontal downward face not touching the build plate — a clear overhang.
    session.execute(
        "from build123d import *\n"
        "post = Box(4, 4, 20)\n"
        "slab = Box(20, 20, 4).move(Location((0, 0, 22)))\n"
        "show(post + slab, 'tshape')\n"
    )
    result = analyze_printability(session, object_name="tshape", support_angle=45.0)
    assert "finding" in result
    data = json.loads(result.split("\n\n", 1)[1])
    assert any(f["kind"] == "overhang" for f in data["findings"])


def test_analyze_printability_unknown_object(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    session.execute("from build123d import *\nshow(Box(1, 1, 1), 'a')")
    result = analyze_printability(session, object_name="nonexistent")
    data = json.loads(result)
    assert "error" in data


def test_analyze_printability_no_shape(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    result = analyze_printability(session)
    data = json.loads(result)
    assert "error" in data


def test_analyze_printability_build_volume(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    session.execute("from build123d import *\nshow(Box(10, 10, 5), 'box')")
    result = analyze_printability(session, object_name="box", build_volume="256 256 256")
    assert "finding" in result


def test_analyze_printability_min_feature_threshold(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    # Box with a 0.3 mm-tall boss on top: flagged at the default
    # min_feature=0.5, not flagged when the threshold is lowered below it.
    session.execute(
        "from build123d import *\n"
        "base = Box(10, 10, 5)\n"
        "boss = Box(4, 4, 0.3).move(Location((0, 0, 2.65)))\n"
        "show(base + boss, 'bossed')\n"
    )
    flagged = analyze_printability(session, object_name="bossed")
    data = json.loads(flagged.split("\n\n", 1)[1])
    assert any(f["kind"] == "min_feature" for f in data["findings"])

    clean = analyze_printability(session, object_name="bossed", min_feature=0.2)
    data = json.loads(clean.split("\n\n", 1)[1])
    assert not any(f["kind"] == "min_feature" for f in data["findings"])


def test_analyze_printability_bed_tol_param(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    session.execute("from build123d import *\nshow(Box(10, 10, 5), 'box')")
    result = analyze_printability(session, object_name="box", bed_tol=0.01)
    assert "finding" in result


def test_analyze_printability_bad_build_volume(session):
    from build123d_mcp.tools.analyze_printability import analyze_printability

    session.execute("from build123d import *\nshow(Box(1, 1, 1), 'a')")
    result = analyze_printability(session, object_name="a", build_volume="not valid")
    data = json.loads(result)
    assert "error" in data
