"""recover() heals an invalid solid, or fails without touching the geometry.

The contract: recover() either returns a faithful valid solid (re-registered) or
it fails and leaves the original shape exactly as it was — it never hands back a
distorted solid that merely happens to be watertight, and never replaces a
registered object on failure or timeout.

These tests construct invalid single solids directly. The heal runs out-of-
process and gates the written-and-reimported STEP (the artifact a scorer sees),
so a defect the STEP round-trip itself normalises — e.g. a reversed face — is
correctly reported "already valid". A defect that survives serialisation and
needs the defeature ladder (an unorientable BSpline from a malformed import) is
validated end-to-end against benchmark fixture 217 in the cadgenbench-build123d
harness — it heals via defeature at a 0.64 mm bbox shift; a second fixture (240)
whose defeature can't finish in budget is killed and FAILs untouched, session
intact. These unit tests pin the constructible contracts: fail-untouched on an
open shell, the serialised-artifact verdict, the no-op, the single-solid guard,
and the resolver error.
"""

import json

import pytest
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
from OCP.TopoDS import TopoDS, TopoDS_Shell

from build123d import Box, Solid
from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.recover import recover
from build123d_mcp.tools.validate import _gate_report


def _solid_from_faces(faces, reverse_first=False):
    builder = BRep_Builder()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for i, f in enumerate(faces):
        fw = f.wrapped
        if reverse_first and i == 0:
            fw = TopoDS.Face_s(fw.Reversed())
        builder.Add(shell, fw)
    return Solid(BRepBuilderAPI_MakeSolid(shell).Solid())


def reversed_face_solid():
    """A unit box with one face reversed: BRepCheck-invalid, healed by ShapeFix
    (re-orients the face). The bbox is identical to a clean box, so a faithful
    heal must pass the bbox guard even though the broken signed volume is wrong."""
    return _solid_from_faces(Box(10, 10, 10).faces(), reverse_first=True)


def open_solid():
    """A box missing one face: not watertight, and no heal can invent the face,
    so recover must FAIL and leave the geometry untouched."""
    return _solid_from_faces(Box(10, 10, 10).faces()[:5])


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def test_recover_gates_the_serialized_artifact(session):
    """recover judges the written-and-reimported STEP (what a scorer sees), not the
    in-memory shape. A reversed-face solid is BRepCheck-invalid in memory but the
    STEP round-trip normalises its orientation, so the artifact is valid — recover
    reports 'already valid' and leaves the object untouched (no needless heal)."""
    session.objects["part"] = reversed_face_solid()
    before = session.objects["part"]
    assert _gate_report(before)["passes_gate"] is False  # invalid in memory

    out = recover(session, "part")
    assert "already valid" in out
    assert session.objects["part"] is before  # untouched — nothing to heal


def test_recover_fail_leaves_geometry_untouched(session):
    """An unhealable invalid solid (open shell) FAILs and leaves the registered
    object as the very same instance — never replaced on failure."""
    session.objects["part"] = open_solid()
    before = session.objects["part"]
    assert _gate_report(before)["passes_gate"] is False  # precondition

    out = recover(session, "part")
    assert out.startswith("Recovery: FAIL")
    payload = json.loads(out.split("\n", 1)[1])
    assert payload["status"] == "failed"
    assert session.objects["part"] is before  # geometry untouched


def test_recover_already_valid_is_noop(session):
    """A solid that already passes the gate is returned as-is, untouched."""
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    before = session.objects["part"]
    out = recover(session, "part")
    assert "already valid" in out
    assert session.objects["part"] is before


def test_recover_refuses_multi_solid(session):
    """recover heals one body; a multi-solid shape is refused, not silently
    operated on, and the geometry is untouched."""
    execute_code(session, "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'two')")
    before = session.objects["two"]
    out = recover(session, "two")
    payload = json.loads(out)
    assert payload["n_solids"] == 2
    assert "single solid" in payload["error"]
    assert session.objects["two"] is before


def test_recover_unknown_object_errors(session):
    """An unknown object name returns the resolver's error, not a crash."""
    out = recover(session, "nope")
    assert "Unknown object" in out
