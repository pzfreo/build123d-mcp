"""Read-only geometry ops isolate large shapes out-of-process, hard-bounded (#360).

measure/cross_sections/clearance run a native OCC analysis (BRepCheck, BRepMesh,
booleans) that is un-interruptible and can outlast the op timeout on a big B-rep — in
the worker that would SIGKILL the whole session. So a large shape's work runs in a real
`subprocess.run` bounded by the op budget; on overrun the child is killed and a clean
error returned, the worker survives. A small shape keeps the fast in-worker path (a STEP
round-trip would dominate). On a host that blocks child processes (#143) it falls back
to in-process. (validate isolates only its mesh stitch, like export — see
test_validate_open_edge_381.)
"""

import json
import subprocess

from build123d import Box

from build123d_mcp.tools import _bounded
from build123d_mcp.tools.cross_sections import cross_sections
from build123d_mcp.tools.measure import clearance, measure


class _StubSession:
    exec_timeout = 120

    def __init__(self, current=None, objects=None):
        self.current_shape = current
        self.objects = objects or {}


def _pair():
    return _StubSession(objects={"a": Box(10, 10, 10), "b": Box(4, 4, 4)})


def test_small_shape_runs_in_process(monkeypatch):
    """A small shape never spawns a subprocess — the fast path is taken."""

    def _boom(*a, **k):
        raise AssertionError("small shape must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    out = json.loads(measure(_StubSession(current=Box(10, 10, 10))))
    assert out["volume"] == 1000.0 and out["topology"]["faces"] == 6


def test_large_shape_runs_out_of_process_and_matches(monkeypatch):
    """Forcing the size-gate low routes a small Box through the real subprocess; the
    result must match the in-process computation."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    out = json.loads(measure(_StubSession(current=Box(10, 10, 10))))
    assert out["volume"] == 1000.0 and out["topology"]["faces"] == 6


def test_all_three_ops_round_trip_out_of_process(monkeypatch):
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    assert json.loads(measure(_StubSession(current=Box(8, 8, 8))))["volume"] == 512.0
    assert len(json.loads(cross_sections(_StubSession(current=Box(8, 8, 8))))) > 0
    assert json.loads(clearance(_pair(), "a", "b"))["status"] == "containing"


def test_clearance_containing_wall_thickness_survives_round_trip(monkeypatch):
    # distance_to() collapses to 0 for a bare Solid that contains the other shape, but
    # returns the true surface gap for a Compound — and a STEP round-trip flips the
    # wrapper EITHER way. clearance normalises the wrapper so wall thickness is identical
    # in vs out of process for BOTH modeller (Compound) and extracted (bare Solid) inputs
    # — and non-zero (the documented wall thickness), not the bare-Solid 0 (#360).
    cases = {
        "compound": (Box(30, 30, 30), Box(5, 5, 5)),
        "bare_solid": (Box(30, 30, 30).solid(), Box(5, 5, 5).solid()),
    }
    base = {
        k: json.loads(clearance(_StubSession(objects={"a": a, "b": b}), "a", "b"))
        for k, (a, b) in cases.items()
    }
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    for k, (a, b) in cases.items():
        out = json.loads(clearance(_StubSession(objects={"a": a, "b": b}), "a", "b"))
        assert out["status"] == base[k]["status"] == "containing", k
        assert base[k]["clearance"] > 0 and out["clearance"] == base[k]["clearance"], k


def test_cone_semi_angle_matches_round_trip(monkeypatch):
    # The raw cone half-angle sign tracks the axis direction, which a STEP round-trip
    # can flip; reporting the magnitude keeps measure() stable in vs out of process.
    from build123d import Cone

    def _semi(report):
        return [f["semi_angle_deg"] for f in report["face_inventory"] if "semi_angle_deg" in f]

    base = _semi(json.loads(measure(_StubSession(current=Cone(6, 2, 12)))))
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)
    out = _semi(json.loads(measure(_StubSession(current=Cone(6, 2, 12)))))
    assert base and out and base == out and base[0] >= 0


def test_timeout_returns_clean_error_not_worker_kill(monkeypatch):
    """An overrun raises TimeoutExpired inside the tool; it must become a clean error
    string (child killed), NOT propagate as a hang / worker SIGKILL."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="shape_op", timeout=45)

    monkeypatch.setattr(subprocess, "run", _timeout)
    out = measure(_StubSession(current=Box(10, 10, 10)))
    assert "exceeded" in out and "budget" in out and "--exec-timeout" in out


def test_subprocess_failure_surfaces_error(monkeypatch):
    """A non-zero subprocess exit surfaces a clear error, not a silent empty result."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)

    class _Proc:
        returncode = 1
        stderr = "boom in the shape-op worker"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    out = measure(_StubSession(current=Box(10, 10, 10)))
    assert out.startswith("Error:") and "subprocess failed" in out


def test_blocked_host_falls_back_in_process(monkeypatch):
    """On a host that blocks child-process creation (#143 / InProcessSession),
    subprocess.run raises OSError — the op must fall back to running in-process and
    still return a correct result."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)

    def _blocked(*a, **k):
        raise PermissionError("child process creation is not permitted")

    monkeypatch.setattr(subprocess, "run", _blocked)
    out = json.loads(measure(_StubSession(current=Box(10, 10, 10))))
    assert out["volume"] == 1000.0  # computed in-process, not an error


def test_unreadable_result_is_clean_error(monkeypatch, tmp_path):
    """A subprocess that exits 0 but leaves garbage in out.json surfaces a clean error."""
    monkeypatch.setattr(_bounded, "_FACE_GATE", 1)

    class _Proc:
        returncode = 0
        stderr = ""

    def _run(cmd, *a, **k):
        # cmd[-1] is the out.json path the worker should have written.
        with open(cmd[-1], "w") as f:
            f.write("{not valid json")
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _run)
    out = measure(_StubSession(current=Box(10, 10, 10)))
    assert out.startswith("Error:") and "unreadable" in out
