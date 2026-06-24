"""render_view tessellates out-of-process, hard-bounded, so it can't kill the session.

OCC ``BRepMesh`` is un-interruptible and can run for minutes on a complex part; in
the worker that would blow the op-timeout and SIGKILL the whole session. The
worker is a daemon (so ``multiprocessing`` isolation is unavailable), so
tessellation runs in a real ``subprocess.run`` bounded by ``_TESS_BUDGET_S``. On
timeout it returns a clean error and the worker survives.
"""

import subprocess

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
