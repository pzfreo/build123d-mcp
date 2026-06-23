"""``recover()``: heal an invalid solid so it passes the validity gate, or fail
without touching the geometry.

The agent's hand-coded ``ShapeFix``/sew often cannot clear an unorientable or
un-meshable face (e.g. a defect inherited from a malformed imported STEP). This
tool runs a small heal ladder — ``ShapeFix_Shape`` then **defeature** the
BRepCheck-invalid faces (remove them, neighbours extend to close the gap) — and
keeps the first variant that passes the *exact* gate on the written-and-
reimported STEP (the authoritative artifact, not the in-memory shape).

Two safety properties, both required:

* **It cannot kill the session.** Defeaturing a large solid is an un-interruptible
  native OCC call that can run for minutes; run in-process it would block the
  worker past its op budget and the parent would SIGKILL it, losing all session
  state. So the heal runs OUT OF PROCESS, hard-bounded by the time left in the op
  budget (mirroring how export bounds its mesh gate). If it overruns it is killed
  and recover() returns FAIL — the session is untouched.
* **It never distorts the geometry.** A heal is accepted only if it passes the
  gate AND every bounding-box face moves less than ``max(1 mm, 1% of the
  diagonal)`` — bbox, not volume, because an invalid solid's signed volume is
  unreliable (a reversed face halves it). Anything that moves the envelope more
  than a defective sliver is refused. So recover() either returns a faithful
  valid solid or fails and leaves the original exactly as it was.

The actual healing + gating runs in :mod:`build123d_mcp._recover_subprocess`;
this module is the in-worker orchestrator that bounds it and re-registers the
result.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from build123d_mcp.tools.validate import _resolve_shape

# Margin (s) reserved below the op budget for the in-process work that brackets
# the subprocess (writing the input STEP, reimporting the output, teardown) plus
# the parent's poll granularity — so the worker total stays under its op-timeout.
_MARGIN_S = 15
_MIN_SUBPROC_S = 10


def _solids(shp):
    out = []
    e = TopExp_Explorer(shp, TopAbs_SOLID)
    while e.More():
        out.append(TopoDS.Solid_s(e.Current()))
        e.Next()
    return out


def _invalid_faces(s):
    a = BRepCheck_Analyzer(s)
    out = []
    e = TopExp_Explorer(s, TopAbs_FACE)
    while e.More():
        f = TopoDS.Face_s(e.Current())
        if not a.IsValid(f):
            out.append(f)
        e.Next()
    return out


def recover(session, object_name: str = "", store_as: str = "") -> str:
    """Heal an invalid solid so it passes the validity gate, or fail untouched.

    Runs ShapeFix → defeature OUT OF PROCESS (hard-bounded by the op budget so it
    can never block the worker) and keeps the first variant that passes the exact
    gate on the reimported STEP AND preserves the bounding box. On PASS the healed
    solid is re-registered; on FAIL (or timeout) the original is left exactly as
    it was. Requires a single solid. object_name: named object from show()
    (default: current shape). store_as: name to register the healed solid under
    (default: overwrite object_name / update the current shape).
    """
    t0 = time.monotonic()
    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err

    # recover heals one solid: it preserves the bbox of and re-gates a single body.
    # A multi-solid shape is ambiguous (healing one while dropping another would
    # look like a recovery), so refuse it rather than guess.
    solids = _solids(shape.wrapped)
    if len(solids) != 1:
        return json.dumps(
            {
                "error": f"recover heals a single solid, but '{object_name or 'current shape'}' "
                f"has {len(solids)} solids. Fuse them (Part +) or recover each body separately.",
                "n_solids": len(solids),
            }
        )

    budget = max(60, getattr(session, "exec_timeout", 120))
    work = tempfile.mkdtemp(prefix="b123d_recover_")
    in_step = os.path.join(work, "in.step")
    out_step = os.path.join(work, "out.step")
    try:
        from build123d import Solid, export_step

        export_step(Solid(solids[0]), in_step)

        remaining = budget - (time.monotonic() - t0) - _MARGIN_S
        if remaining < _MIN_SUBPROC_S:
            return (
                "Recovery: FAIL — not enough of the op budget left to heal safely; "
                "geometry untouched. Retry on a fresh op or raise --exec-timeout.\n"
                + json.dumps({"recovered": False, "reason": "insufficient_budget"})
            )

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "build123d_mcp._recover_subprocess", in_step, out_step],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return (
                "Recovery: FAIL — the heal exceeded the time budget and was stopped; "
                "geometry untouched (the session is safe). The defect is likely too "
                "large/complex to defeature in budget; inspect with measure()/render "
                "and rebuild the region in execute().\n"
                + json.dumps({"recovered": False, "reason": "timeout"})
            )

        result = None
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("RECOVER_RESULT:"):
                try:
                    result = json.loads(line[len("RECOVER_RESULT:") :])
                except ValueError:
                    result = None
                break
        if result is None:
            return (
                "Recovery: FAIL — the heal worker did not return a result; geometry "
                "untouched.\n"
                + json.dumps({"recovered": False, "reason": "no_result", "stderr": proc.stderr[-300:]})
            )

        status = result.get("status")
        if status == "already_valid":
            return "Recovery: PASS — already valid, no recovery needed.\n" + json.dumps(result)
        if status == "recovered":
            from build123d import import_step

            healed = import_step(out_step)
            session.current_shape = healed
            dest = store_as or object_name
            if dest:
                session.objects[dest] = healed
            where = f"'{dest}'" if dest else "the current shape"
            return (
                f"Recovery: PASS via {result['method']} "
                f"(bbox moved ≤{result.get('bbox_max_delta', 0)} mm, tol {result.get('tol')} mm). "
                f"Updated {where}.\n" + json.dumps(result)
            )
        # status == "failed"
        return (
            "Recovery: FAIL — no geometry-preserving heal cleared the gate; the original "
            "shape is left untouched. The defect may be intrinsic (e.g. a self-intersecting "
            "input face); inspect it with measure()/render and rebuild the region in "
            "execute().\n" + json.dumps(result)
        )
    finally:
        for p in (in_step, out_step):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass
