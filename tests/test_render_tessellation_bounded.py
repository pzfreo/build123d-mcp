"""render_view tessellates out-of-process, hard-bounded, so it can't kill the session.

OCC ``BRepMesh`` is un-interruptible and can run for minutes on a complex part; in
the worker that would blow the op-timeout and SIGKILL the whole session. The
worker is a daemon (so ``multiprocessing`` isolation is unavailable), so
tessellation runs in a real ``subprocess.run`` bounded by ``_TESS_BUDGET_S``. On
timeout it returns a clean error and the worker survives.
"""

import subprocess
import sys

import pytest
from build123d import Box

from build123d_mcp.tools import render
from build123d_mcp.tools.render import _tessellate_shapes_bounded


def test_tessellate_bounded_returns_mesh():
    """A normal shape tessellates via the subprocess and comes back as a mesh."""
    meshes, failed = _tessellate_shapes_bounded(
        [("box", Box(10, 10, 10), None)], render._QUALITY["standard"]
    )
    assert failed == []
    assert "box" in meshes
    verts, tris = meshes["box"]
    assert len(verts) > 0 and len(tris) > 0


def test_tessellate_bounded_timeout_is_graceful(monkeypatch):
    """A tessellation that overruns the budget raises a clean RuntimeError (and the
    subprocess is killed) — it does NOT propagate as a hang / worker SIGKILL."""

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else "tessellate", timeout=render._TESS_BUDGET_S
        )

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(RuntimeError, match="tessellation budget"):
        _tessellate_shapes_bounded([("box", Box(10, 10, 10), None)], render._QUALITY["standard"])


def test_tessellate_bounded_reports_subprocess_failure(monkeypatch):
    """A non-zero subprocess exit surfaces a clear error, not a silent empty render."""

    class _Proc:
        returncode = 1
        stderr = "boom in the tessellation worker"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="Tessellation subprocess failed"):
        _tessellate_shapes_bounded([("box", Box(10, 10, 10), None)], render._QUALITY["standard"])


def test_tessellate_falls_back_in_process_when_subprocess_blocked(monkeypatch):
    """On a host that blocks child-process creation (#143 / InProcessSession),
    subprocess.run raises OSError — render must fall back to in-process
    tessellation, not break. The shape still comes back as a mesh."""

    def _blocked(*a, **k):
        raise PermissionError("child process creation is not permitted")

    monkeypatch.setattr(subprocess, "run", _blocked)
    meshes, failed = _tessellate_shapes_bounded(
        [("box", Box(10, 10, 10), None)], render._QUALITY["standard"]
    )
    assert failed == []
    assert "box" in meshes and len(meshes["box"][1]) > 0


def test_tessellate_bounded_unreadable_pickle_is_clean_error(monkeypatch, tmp_path):
    """A subprocess that exits 0 but leaves a truncated/garbage pickle surfaces a
    clean actionable error, not a raw UnpicklingError."""

    class _Proc:
        returncode = 0
        stderr = ""

    def _run(cmd, *a, **k):
        # cmd[4] is the out_pkl path the worker should write; leave garbage there.
        with open(cmd[4], "wb") as f:
            f.write(b"\x80\x04not-a-valid-pickle")
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="unreadable result"):
        _tessellate_shapes_bounded([("box", Box(10, 10, 10), None)], render._QUALITY["standard"])


# --------------------------------------------------------------------------- #
# VTK render is likewise bounded out-of-process (#357): the daemon worker can't
# spawn a multiprocessing child, so isolation must go through subprocess.run —
# the old daemon-check fallback silently ran VTK unbounded in-process, letting a
# hung render blow the op-timeout and SIGKILL the whole session.
# --------------------------------------------------------------------------- #

from build123d_mcp.tools.render import _vtk_render_subprocess


def _box_shape_data():
    meshes, _ = _tessellate_shapes_bounded(
        [("box", Box(10, 10, 10), None)], render._QUALITY["standard"]
    )
    return [("box", meshes["box"][0], meshes["box"][1], None)]


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="_vtk_render_subprocess is the macOS-only render path; Linux/Windows render in-process",
)
def test_vtk_render_subprocess_returns_png():
    """A normal render goes through the real subprocess and returns valid PNG bytes."""
    png = _vtk_render_subprocess(_box_shape_data(), "iso", "", None, 0.0, 0.0, None)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 100


def test_vtk_render_timeout_is_graceful(monkeypatch):
    """A VTK render that overruns its budget raises a clean RuntimeError (child
    killed) — it does NOT hang until the parent SIGKILLs the session."""
    shape_data = _box_shape_data()  # tessellate for real BEFORE mocking subprocess.run

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="vtk", timeout=render._VTK_BUDGET_S)

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(RuntimeError, match="VTK rendering exceeded"):
        _vtk_render_subprocess(shape_data, "iso", "", None, 0.0, 0.0, None)


def test_vtk_render_reports_subprocess_failure(monkeypatch):
    """A non-zero VTK subprocess exit surfaces a clear error, not a silent blank."""
    shape_data = _box_shape_data()

    class _Proc:
        returncode = 1
        stderr = "boom in the vtk worker"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="VTK render subprocess failed"):
        _vtk_render_subprocess(shape_data, "iso", "", None, 0.0, 0.0, None)


def test_vtk_render_falls_back_in_process_when_subprocess_blocked(monkeypatch):
    """On a host that blocks child-process creation (#143 / InProcessSession),
    subprocess.run raises OSError — VTK must fall back to in-process rendering
    (the genuine degraded-host case), NOT the old always-on daemon fallback."""
    shape_data = _box_shape_data()

    def _blocked(*args, **kwargs):
        raise OSError("child processes disabled")

    monkeypatch.setattr(subprocess, "run", _blocked)
    monkeypatch.setattr(render, "_vtk_render_tesselated", lambda *a, **k: b"IN_PROCESS_PNG")
    out = _vtk_render_subprocess(shape_data, "iso", "", None, 0.0, 0.0, None)
    assert out == b"IN_PROCESS_PNG"


def test_render_budget_timeout_does_not_trigger_svg_fallback(monkeypatch):
    """A budget timeout surfaces cleanly — it must NOT run the unbounded SVG (HLR)
    fallback, which after both stages are near their limits could push the op past
    the parent watchdog and SIGKILL the session (#357)."""
    from build123d_mcp.session import Session
    from build123d_mcp.tools.render import _RenderBudgetExceeded, render_view

    s = Session()
    s.execute("from build123d import *")
    s.execute("show(Box(10, 10, 10), 'b')\n")

    def _budget_timeout(*a, **k):
        raise _RenderBudgetExceeded("VTK rendering exceeded the 60s budget — too complex")

    svg_called = []
    monkeypatch.setattr(render, "_do_render_png", _budget_timeout)
    monkeypatch.setattr(
        render, "_do_render_svg", lambda *a, **k: (svg_called.append(True), b"<svg/>")[1]
    )

    with pytest.raises(_RenderBudgetExceeded, match="budget"):
        render_view(s)
    assert svg_called == []  # the unbounded SVG fallback must not have run
