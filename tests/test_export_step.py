"""export() writes STEP resiliently.

build123d 0.11.0's high-level ``export_step`` (the ``STEPCAFControl_Writer`` path)
raises ``RuntimeError: Failed to write STEP file`` on many imported-STEP-derived
solids that 0.10.0 wrote fine — it hit ~38% of editing-fixture benchmark runs.
``_write_step`` first retries through a ``Compound`` wrapper (which keeps the CAF
path working, so names/colours survive), then falls back to the basic
``STEPControl_Writer`` (geometry only). These tests pin the happy path, the
forced fallback (high-level writer made to raise, so it runs on any version), and
the real reimported-solid regression.
"""

import pytest
from build123d import import_step

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.export import export_file


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def test_export_step_writes_valid_solid(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10) - Cylinder(3, 12), 'part')")
    export_file(session, "out", "step", object_name="part")
    out = tmp_path / "out.step"
    assert out.exists()
    assert len(import_step(str(out)).solids()) == 1


def test_export_step_falls_back_when_high_level_writer_fails(session, tmp_path, monkeypatch):
    """When build123d's export_step raises (the 0.11.0 regression), export still
    produces a valid, reimportable STEP via the raw STEPControl_Writer."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10) - Cylinder(3, 12), 'part')")
    want_vol = session.objects["part"].volume

    def boom(*a, **k):
        raise RuntimeError("Failed to write STEP file")

    monkeypatch.setattr("build123d.export_step", boom)

    export_file(session, "out", "step", object_name="part")
    out = tmp_path / "out.step"
    assert out.exists()
    reimported = import_step(str(out))
    assert len(reimported.solids()) == 1
    # geometry round-trips: the fallback writer preserved the solid (volume), it
    # only drops CAF labels/colours.
    assert reimported.volume == pytest.approx(want_vol, rel=1e-3)


def test_export_step_fallback_preserves_all_solids(session, tmp_path, monkeypatch):
    """The raw-writer fallback must carry EVERY solid of a multi-solid compound,
    not silently drop bodies — the main silent-degradation risk of the fallback."""
    monkeypatch.chdir(tmp_path)
    execute_code(
        session,
        "show(Compound(children=[Box(10, 10, 10), Pos(20, 0, 0) * Box(10, 10, 10), "
        "Pos(40, 0, 0) * Box(10, 10, 10)]), 'asm')",
    )

    def boom(*a, **k):
        raise RuntimeError("Failed to write STEP file")

    monkeypatch.setattr("build123d.export_step", boom)

    export_file(session, "out", "step", object_name="asm")
    reimported = import_step(str(tmp_path / "out.step"))
    assert len(reimported.solids()) == 3  # all three bodies survived the fallback


def test_export_step_raises_clearly_when_both_writers_fail(session, tmp_path, monkeypatch):
    """If even the raw writer can't write, surface a clear combined error rather
    than a bare OCC failure."""
    monkeypatch.chdir(tmp_path)
    execute_code(session, "show(Box(10, 10, 10), 'part')")

    def boom(*a, **k):
        raise RuntimeError("Failed to write STEP file")

    monkeypatch.setattr("build123d.export_step", boom)

    class _Dead:
        def __init__(self, *a, **k): ...
        def Transfer(self, *a, **k): ...
        def Write(self, *a, **k):
            from OCP.IFSelect import IFSelect_ReturnStatus

            return IFSelect_ReturnStatus.IFSelect_RetFail

    monkeypatch.setattr("OCP.STEPControl.STEPControl_Writer", _Dead)
    with pytest.raises(RuntimeError, match="all failed"):
        export_file(session, "out", "step", object_name="part")


def test_write_step_handles_reimported_solid(tmp_path):
    """gumyr/build123d#1356: on build123d 0.11 ``export_step`` raises on a solid
    that came straight from ``import_step``; ``_write_step`` must still write a
    valid STEP (via the Compound-wrap retry) and must NOT mutate the caller's
    shape. Cross-version: on 0.10 the primary path already works. No monkeypatch
    — this exercises the real regression where the installed version has it.
    """
    from build123d import Box, export_step, import_step

    from build123d_mcp.tools.export import _write_step

    seed = tmp_path / "seed.step"
    export_step(Box(10, 10, 10), str(seed))
    s = import_step(str(seed))
    parent_before = s.parent

    out = tmp_path / "out.step"
    _write_step(s, str(out))  # must not raise on either build123d version

    assert s.parent is parent_before  # the fallback must not reparent the shape
    back = import_step(str(out))
    assert len(back.solids()) == 1
    assert back.volume == pytest.approx(1000.0, rel=1e-3)
