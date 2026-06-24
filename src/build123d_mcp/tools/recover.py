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
  and recover() returns FAIL — the session is untouched. The parent's own reimport
  of the healed STEP is likewise an OCC call, so it only runs when enough budget
  remains for the cost the subprocess measured (else FAIL untouched, heal saved
  to a path the agent can import on a fresh op).
* **It never distorts the geometry.** A heal is accepted only if it passes the
  gate AND the **surface-deviation (Hausdorff) distance** between the input and
  the healed solid stays under ``min(max(1 mm, 1% of the diagonal), 3 mm)`` — no
  surface point moved more than that. This catches the loss of an internal feature
  (a filled-in bore moves its wall ~16 mm) that a bbox or signed-volume proxy is
  blind to. Anything that distorts the part beyond that tolerance is refused. So
  recover() either returns a faithful valid solid or fails and leaves the original
  exactly as it was. (See :mod:`build123d_mcp._recover_subprocess` for the
  guard's measured limits — notably a sub-ε feature can still be lost.)

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
# After the bounded subprocess returns a heal, the parent must reimport the healed
# STEP to bring it into the session — an un-interruptible OCC call running back
# in the worker. The subprocess reports how long ITS reimport of the same file
# took; the parent only performs its own load if at least this multiple of that
# cost is still left in the budget, otherwise it FAILs untouched (and persists the
# healed STEP to a path the agent can import on a fresh op). Without this guard a
# heal that finishes near the time limit plus a slow reimport could push the
# worker past its op-timeout and the parent would SIGKILL it, losing the session.
_REIMPORT_SAFETY = 2.0


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


def _n_faces(shp):
    n = 0
    e = TopExp_Explorer(shp, TopAbs_FACE)
    while e.More():
        n += 1
        e.Next()
    return n


def recover(session, object_name: str = "", store_as: str = "") -> str:
    """Heal an invalid solid so it passes the validity gate, or fail untouched.

    Runs ShapeFix → defeature OUT OF PROCESS (hard-bounded by the op budget so it
    can never block the worker) and keeps the first variant that passes the exact
    gate on the reimported STEP AND preserves the geometry (surface-deviation /
    Hausdorff bound — no point moves more than min(max(1 mm, 1% of the diagonal), 3 mm)).
    On PASS the healed solid is re-registered; on FAIL (or timeout) the original
    is left exactly as it was. Requires a single solid. object_name: named object from show()
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

    # A 1-solid Compound may also carry free faces / PMI alongside the body. recover
    # heals only the solid, so accepting it would silently drop that content on PASS.
    # Refuse rather than lose it (consistent with the multi-solid refusal).
    free_faces = _n_faces(shape.wrapped) - _n_faces(solids[0])
    if free_faces > 0:
        return json.dumps(
            {
                "error": f"'{object_name or 'current shape'}' has a single solid but also "
                f"{free_faces} free face(s)/PMI alongside it, which recover would drop. "
                "Extract the solid (e.g. shape.solids()[0]) and recover that.",
                "free_faces": free_faces,
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
            # The parent's own reimport is an un-interruptible OCC call; only run it
            # if enough budget remains for it (the subprocess measured the same load).
            # Otherwise FAIL untouched rather than risk overrunning the op-timeout —
            # but persist the healed STEP so the successful heal isn't thrown away.
            reimport_secs = result.get("reimport_secs", 0) or 0
            time_left = budget - (time.monotonic() - t0) - _MARGIN_S
            if time_left < reimport_secs * _REIMPORT_SAFETY:
                import shutil

                fd, kept = tempfile.mkstemp(prefix="b123d_recovered_", suffix=".step")
                os.close(fd)
                shutil.copy(out_step, kept)
                result["recovered_step"] = kept
                return (
                    "Recovery: FAIL — a faithful heal was found but too little of the op "
                    "budget remained to load it safely; geometry untouched (session safe). "
                    f"The healed STEP is saved at {kept} — import_cad_file() it on a fresh op, "
                    "or raise --exec-timeout.\n" + json.dumps(result)
                )

            # Verify in the PARENT what it actually loads and registers. The
            # subprocess already ran the authoritative (mesh) gate on these exact
            # bytes; the parent does its own independent OCC load, so it confirms that
            # load yielded a single BRepCheck-valid solid — never trusting the
            # subprocess's word about a file the parent reads separately. A load
            # failure or a corrupt/solid-less STEP returns FAIL untouched (so a bad
            # artifact can't be registered as PASS, and a missing out.step can't raise
            # out of recover()). This check is BRepCheck only (no meshing) so it cannot
            # itself overrun the budget — the expensive mesh check stays in the bounded
            # subprocess, never in the parent after the budget has been spent.
            from build123d import Solid, import_step

            try:
                healed_solids = _solids(import_step(out_step).wrapped)
            except Exception:
                healed_solids = []
            healed = Solid(healed_solids[0]) if len(healed_solids) == 1 else None
            if healed is None or not BRepCheck_Analyzer(healed.wrapped).IsValid():
                return (
                    "Recovery: FAIL — the healed STEP did not load as a single valid "
                    "solid in the parent re-check; geometry untouched.\n"
                    + json.dumps({**result, "status": "failed", "reason": "parent_recheck_failed"})
                )
            dest = store_as or object_name
            if dest:
                session.objects[dest] = healed
            # Update the current shape only when the heal targets it (no object_name)
            # or lands on the same object we resolved (in-place overwrite) — not when
            # store_as redirects the result to a *different* named object the user
            # didn't ask to make current. (dest == object_name covers both the
            # default overwrite and an explicit store_as equal to the source.)
            if not object_name or dest == object_name:
                session.current_shape = healed
            where = f"'{dest}'" if dest else "the current shape"
            return (
                f"Recovery: PASS via {result['method']} "
                f"(surface moved ≤{result.get('hausdorff', 0)} mm, tol {result.get('eps')} mm). "
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
