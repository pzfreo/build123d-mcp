"""recover() heals an invalid solid, or fails without touching the geometry.

The contract: recover() either returns a faithful valid solid (re-registered) or
it fails and leaves the original shape exactly as it was — it never hands back a
distorted solid that merely happens to be watertight, and never replaces a
registered object on failure or timeout.

A heal is accepted only if it both passes the exact gate on the written-and-
reimported STEP AND preserves the geometry to within a surface-deviation
(Hausdorff) bound — no surface point moved more than max(1 mm, 0.5% of the
diagonal). That bound is what catches a heal that distorts the part (e.g.
defeaturing away an internal bore moves its wall ~12 mm) while a bounding-box or
signed-volume proxy is blind to it.

These tests construct invalid single solids directly. The heal runs out-of-
process and gates the written-and-reimported STEP (the artifact a scorer sees),
so a defect the STEP round-trip itself normalises — e.g. a reversed face — is
correctly reported "already valid". A defect that survives serialisation and
needs the defeature ladder (an unorientable BSpline from a malformed import) is
validated end-to-end against benchmark fixture 217 in the cadgenbench-build123d
harness; a second fixture (240) whose defeature can't finish in budget is killed
and FAILs untouched, session intact. These unit tests pin the constructible
contracts: fail-untouched on an open shell, the serialised-artifact verdict, the
no-op, the single-solid guard, the resolver error, the surface-deviation guard
(rejects a distorting heal, accepts a faithful one), and the budget-decline path.
"""

import json
import os

import pytest
from build123d import Box, Solid
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
from OCP.TopoDS import TopoDS, TopoDS_Shell

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


# ---- the fidelity guard: the property the bbox/volume proxies could not enforce ----


def test_surface_deviation_detects_internal_feature_loss():
    """The guard's reason for being: filling in an internal feature (a Ø16 bore)
    moves a real surface ~12 mm even though the bounding box does not change at
    all. A bbox/volume proxy is blind to this; the surface deviation is not."""
    from build123d import Cylinder

    from build123d_mcp._recover_subprocess import _first_solid, _surface_deviation

    block = _first_solid(Box(40, 40, 40).wrapped)
    bored = _first_solid((Box(40, 40, 40) - Cylinder(radius=8, height=40)).wrapped)

    # bore-fill: a real internal surface vanishes -> large deviation -> would REJECT
    assert _surface_deviation(bored, block, 1.0) > 5.0
    # identical solids -> ~0 -> would ACCEPT
    assert _surface_deviation(block, _first_solid(Box(40, 40, 40).wrapped), 1.0) < 0.05
    # a sub-mm faithful change stays sub-mm -> would ACCEPT
    thin = _first_solid(Box(40, 40, 39.7).wrapped)
    assert _surface_deviation(block, thin, 0.5) < 1.0


def test_recover_subprocess_rejects_distorting_heal(tmp_path, monkeypatch):
    """End-to-end through the subprocess main(): a heal that produces a valid but
    DISTORTED solid (bbox doubled) is refused on the surface-deviation guard, so the
    run reports 'failed' — never 'recovered'. Injected because a real
    un-shapefixable invalid solid that *also* defeatures into a distortion can't be
    built in a few lines (round-2 review's 'minimum to unblock' item 4)."""
    import io
    from contextlib import redirect_stdout

    from build123d import export_step

    import build123d_mcp._recover_subprocess as rs

    in_step = str(tmp_path / "in.step")
    out_step = str(tmp_path / "out.step")
    export_step(Solid(rs._first_solid(Box(10, 10, 10).wrapped)), in_step)

    # Force the heal path: treat the input as invalid, every candidate as valid.
    calls = {"n": 0}

    def fake_gate(_solid):
        calls["n"] += 1
        return calls["n"] > 1  # 1st call = src (invalid); later = candidates (valid)

    monkeypatch.setattr(rs, "_gate_pass", fake_gate)
    # shapefix yields a grossly distorted (but watertight) solid; defeature no-ops.
    monkeypatch.setattr(rs, "_shapefix", lambda _s: rs._first_solid(Box(20, 20, 20).wrapped))
    monkeypatch.setattr(rs, "_defeature", lambda _s: None)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rs.main(in_step, out_step)
    line = next(ln for ln in buf.getvalue().splitlines() if ln.startswith("RECOVER_RESULT:"))
    result = json.loads(line[len("RECOVER_RESULT:") :])
    assert result["status"] == "failed"
    assert any("alters geometry" in a.get("result", "") for a in result["attempts"])


def test_recover_subprocess_accepts_faithful_heal(tmp_path, monkeypatch):
    """The mirror: a heal that reproduces the input faithfully passes the guard and
    is reported 'recovered' with a small deviation and a measured reimport cost."""
    import io
    from contextlib import redirect_stdout

    from build123d import export_step

    import build123d_mcp._recover_subprocess as rs

    in_step = str(tmp_path / "in.step")
    out_step = str(tmp_path / "out.step")
    export_step(Solid(rs._first_solid(Box(10, 10, 10).wrapped)), in_step)

    calls = {"n": 0}

    def fake_gate(_solid):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(rs, "_gate_pass", fake_gate)
    monkeypatch.setattr(rs, "_shapefix", lambda _s: rs._first_solid(Box(10, 10, 10).wrapped))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rs.main(in_step, out_step)
    line = next(ln for ln in buf.getvalue().splitlines() if ln.startswith("RECOVER_RESULT:"))
    result = json.loads(line[len("RECOVER_RESULT:") :])
    assert result["status"] == "recovered"
    assert result["hausdorff"] <= result["eps"]
    assert "reimport_secs" in result


def test_recover_declines_load_when_budget_too_small(session, monkeypatch):
    """The #308-style MAJOR fix: even when the subprocess returns a faithful heal,
    the parent must not run its own (un-interruptible) reimport if too little budget
    remains — it FAILs untouched and persists the healed STEP to a path instead, so
    the worker can't be pushed past its op-timeout and SIGKILLed."""
    from build123d import export_step

    import build123d_mcp.tools.recover as recover_mod

    execute_code(session, "show(Box(10, 10, 10), 'part')")
    before = session.objects["part"]
    session.exec_timeout = 60  # budget = 60; time_left ≈ 45

    class _FakeProc:
        returncode = 0
        stderr = ""

        def __init__(self, out_step):
            export_step(Box(10, 10, 10).solid(), out_step)  # the "healed" artifact
            self.stdout = "RECOVER_RESULT:" + json.dumps(
                {"status": "recovered", "method": "defeature", "hausdorff": 0.1,
                 "eps": 1.0, "reimport_secs": 100.0}  # 45 < 100*2 -> decline
            )

    def fake_run(cmd, **kw):
        return _FakeProc(cmd[-1])

    monkeypatch.setattr(recover_mod.subprocess, "run", fake_run)

    out = recover(session, "part")
    assert out.startswith("Recovery: FAIL")
    payload = json.loads(out.split("\n", 1)[1])
    assert "recovered_step" in payload  # heal persisted, not thrown away
    assert os.path.exists(payload["recovered_step"])
    assert session.objects["part"] is before  # untouched


def _fake_recovered_run(out_step_writer):
    """A subprocess.run stand-in that writes the healed STEP via out_step_writer(path)
    and reports a fast 'recovered' result (so the parent does its own reimport)."""
    import build123d_mcp.tools.recover as recover_mod

    class _FakeProc:
        returncode = 0
        stderr = ""

        def __init__(self, out_step):
            out_step_writer(out_step)
            self.stdout = "RECOVER_RESULT:" + json.dumps(
                {"status": "recovered", "method": "defeature", "hausdorff": 0.1,
                 "eps": 1.0, "reimport_secs": 0.1}
            )

    return recover_mod, lambda cmd, **kw: _FakeProc(cmd[-1])


def test_recover_parent_regate_rejects_bad_artifact(session, monkeypatch):
    """The parent does its OWN reimport of the healed STEP, so it must re-gate what
    it actually loads — never trust the subprocess's word about a file it reads
    separately. If that load yields no valid solid (here: a corrupt STEP), recover
    FAILs untouched instead of registering it as PASS or raising."""
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    before = session.objects["part"]

    def write_garbage(out_step):
        with open(out_step, "w") as f:
            f.write("NOT A VALID STEP FILE")

    recover_mod, fake_run = _fake_recovered_run(write_garbage)
    monkeypatch.setattr(recover_mod.subprocess, "run", fake_run)

    out = recover(session, "part")
    assert out.startswith("Recovery: FAIL")
    assert "re-check" in out
    assert session.objects["part"] is before  # not replaced by the bad artifact


def test_recover_store_as_does_not_hijack_current_shape(session, monkeypatch):
    """store_as redirects the heal to a NEW object; the current shape the user was
    looking at must not be silently switched to the healed solid."""
    from build123d import export_step

    execute_code(session, "show(Box(10, 10, 10), 'part')")
    execute_code(session, "show(Box(5, 5, 5), 'other')")
    session.current_shape = session.objects["other"]
    current_before = session.current_shape
    part_before = session.objects["part"]

    recover_mod, fake_run = _fake_recovered_run(
        lambda out_step: export_step(Box(10, 10, 10).solid(), out_step)
    )
    monkeypatch.setattr(recover_mod.subprocess, "run", fake_run)

    out = recover(session, "part", store_as="healed")
    assert out.startswith("Recovery: PASS")
    assert "healed" in session.objects  # the heal landed under the new name
    assert session.objects["part"] is part_before  # source untouched
    assert session.current_shape is current_before  # NOT hijacked


def test_recover_refuses_compound_with_free_faces(session):
    """A 1-solid Compound that also carries free faces/PMI is refused, not silently
    stripped down to the bare solid on PASS."""
    from build123d import Compound

    box = Box(10, 10, 10)
    mixed = Compound(children=[box.solid(), box.faces()[0]])  # one solid + a loose face
    session.objects["mixed"] = mixed
    before = session.objects["mixed"]

    out = recover(session, "mixed")
    payload = json.loads(out)
    assert payload.get("free_faces", 0) > 0
    assert "free face" in payload["error"]
    assert session.objects["mixed"] is before  # untouched
