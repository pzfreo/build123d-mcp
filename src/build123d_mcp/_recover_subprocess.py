"""Out-of-process heal worker for ``recover()`` (see tools/recover.py).

Run as ``python -m build123d_mcp._recover_subprocess <in.step> <out.step>``. The
parent bounds this whole process with a hard ``subprocess`` timeout, so the
un-interruptible OCC defeature/ShapeFix native calls can never block the worker
past its op budget (which would kill the session). On success the accepted healed
solid is written to ``out.step`` and a ``RECOVER_RESULT:{...}`` line is printed.

The heal ladder is deliberately small and geometry-preserving: ``ShapeFix_Shape``
then **defeature** the BRepCheck-invalid faces. A candidate is accepted only if
the *reimported* STEP (the authoritative artifact) passes the exact gate AND its
bounding box is within ``max(1 mm, 1% of the diagonal)`` of the input on every
face — bbox, not volume, because an invalid solid's signed volume is unreliable.
"""

import json
import sys

from build123d import Solid, export_step, import_step
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.ShapeFix import ShapeFix_Shape
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_ListOfShape

from build123d_mcp.tools.validate import _gate_report


def _solids(shp):
    out = []
    e = TopExp_Explorer(shp, TopAbs_SOLID)
    while e.More():
        out.append(TopoDS.Solid_s(e.Current()))
        e.Next()
    return out


def _first_solid(shp):
    s = _solids(shp)
    return s[0] if s else None


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


def _bbox6(s):
    b = Bnd_Box()
    BRepBndLib.Add_s(s, b)
    return b.Get()  # (xmin, ymin, zmin, xmax, ymax, zmax)


def _diag(bb):
    x0, y0, z0, x1, y1, z1 = bb
    return ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5


def _gate_pass(solid):
    return _gate_report(Solid(solid), exact=True)["passes_gate"]


def _shapefix(src):
    fx = ShapeFix_Shape(src)
    fx.Perform()
    return _first_solid(fx.Shape())


def _defeature(src):
    bad = _invalid_faces(src)
    if not bad:
        return None
    df = BRepAlgoAPI_Defeaturing()
    df.SetShape(src)
    faces = TopTools_ListOfShape()
    for f in bad:
        faces.Append(f)
    df.AddFacesToRemove(faces)
    df.Build()
    return _first_solid(df.Shape()) if df.IsDone() else None


def _emit(obj):
    print("RECOVER_RESULT:" + json.dumps(obj))


def main(in_step, out_step):
    src = _first_solid(import_step(in_step).wrapped)
    if src is None:
        _emit({"status": "failed", "attempts": [{"method": "load", "result": "no solid in input"}]})
        return
    if _gate_pass(src):
        _emit({"status": "already_valid"})
        return

    bb0 = _bbox6(src)
    tol = max(1.0, 0.01 * _diag(bb0))
    attempts = []
    for name, heal in (("shapefix", _shapefix), ("defeature", _defeature)):
        try:
            cand = heal(src)
        except Exception as exc:  # noqa: BLE001 - record and keep trying
            attempts.append({"method": name, "result": f"error: {repr(exc)[:120]}"})
            continue
        if cand is None or _invalid_faces(cand):
            attempts.append({"method": name, "result": "no-op / still invalid"})
            continue
        # Gate the WRITTEN-AND-REIMPORTED STEP (the authoritative artifact), not the
        # in-memory candidate — serialization can degrade a shape that passed in
        # memory (the bug export's reimport gate guards against).
        export_step(Solid(cand), out_step)
        reimp = _first_solid(import_step(out_step).wrapped)
        if reimp is None or not _gate_pass(reimp):
            attempts.append({"method": name, "result": "reimported artifact still fails gate"})
            continue
        dmax = max(abs(a - b) for a, b in zip(bb0, _bbox6(reimp)))
        if dmax > tol:
            attempts.append(
                {"method": name, "result": "rejected — alters geometry", "bbox_max_delta": round(dmax, 4)}
            )
            continue
        _emit(
            {
                "status": "recovered",
                "method": name,
                "bbox_max_delta": round(dmax, 4),
                "tol": round(tol, 4),
                "attempts": attempts,
            }
        )
        return

    _emit({"status": "failed", "attempts": attempts})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
