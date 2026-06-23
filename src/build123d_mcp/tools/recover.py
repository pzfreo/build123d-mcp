"""``recover()``: heal an invalid solid so it passes the validity gate.

The agent's hand-coded ``ShapeFix``/sew often cannot clear an unorientable or
un-meshable face (e.g. a defect inherited from a malformed imported STEP). This
tool runs a heal ladder and keeps the first variant that passes the *exact*
validity gate, re-registering it:

1. ``ShapeFix_Shape`` — cheap, reorient/fix in place (what the agent already does).
2. **Defeature** the BRepCheck-invalid faces — remove them and let neighbours
   extend to close the gap (fixes unorientable patches a sew cannot).
3. **Drop + sew** the invalid faces over a tolerance ladder, rebuild the solid,
   ``ShapeFix_Solid`` — bridges a thin invalid sliver the defeature cannot remove.

A heal is accepted only if it passes the gate AND leaves the volume and bounding
box essentially unchanged: a heal that distorts the part is a fake recovery, so
it is refused and the original geometry is left untouched. recover() therefore
either returns a faithful valid solid or fails honestly — it never hands back a
mangled solid that merely happens to be watertight. A candidate is only sent
through the expensive mesh gate once it is BRepCheck-clean, so the ladder stays
cheap on the common no-op rungs.
"""

import json

from build123d import Solid
from OCP.Bnd import Bnd_Box
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Solid
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_ListOfShape

from build123d_mcp.tools.validate import _gate_report, _resolve_shape

_SEW_TOLS = (0.1, 0.3, 0.6, 1.0, 1.5)

# A heal that clears the gate but moved the geometry is NOT a heal — it would
# fool the caller into thinking a part was recovered when it was actually
# mangled. Accept a candidate only if both the volume and the bounding-box
# diagonal are within these (small) fractional tolerances of the original.
_MAX_VOL_FRAC = 0.01  # 1% volume change
_MAX_BBOX_FRAC = 0.01  # 1% bbox-diagonal change — loose enough to allow removing
# a protruding defective sliver (which legitimately shrinks the envelope a little)
# but tight enough to reject a heal that drops a real face and bridges a gap.


def _vol(s):
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(s, g)
    return g.Mass()


def _bbox_diag(s):
    b = Bnd_Box()
    BRepBndLib.Add_s(s, b)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5


def _solids(shp):
    out = []
    e = TopExp_Explorer(shp, TopAbs_SOLID)
    while e.More():
        out.append(TopoDS.Solid_s(e.Current()))
        e.Next()
    return out


def _first_solid(shp):
    solids = _solids(shp)
    return solids[0] if solids else shp


def _first_shell(shp):
    e = TopExp_Explorer(shp, TopAbs_SHELL)
    return TopoDS.Shell_s(e.Current()) if e.More() else None


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


def _shapefix(s):
    fx = ShapeFix_Shape(s)
    fx.Perform()
    return _first_solid(fx.Shape())


def _defeature(s):
    bad = _invalid_faces(s)
    if not bad:
        return None
    df = BRepAlgoAPI_Defeaturing()
    df.SetShape(s)
    faces = TopTools_ListOfShape()
    for f in bad:
        faces.Append(f)
    df.AddFacesToRemove(faces)
    df.Build()
    return _first_solid(df.Shape()) if df.IsDone() else None


def _dropsew(s, tol):
    bad = {f.TShape() for f in _invalid_faces(s)}
    if not bad:
        return None
    sew = BRepBuilderAPI_Sewing(tol)
    e = TopExp_Explorer(s, TopAbs_FACE)
    while e.More():
        f = TopoDS.Face_s(e.Current())
        if f.TShape() not in bad:
            sew.Add(f)
        e.Next()
    sew.Perform()
    shell = _first_shell(sew.SewedShape())
    if shell is None:
        return None
    sol = BRepBuilderAPI_MakeSolid(shell).Solid()
    fs = ShapeFix_Solid(sol)
    fs.Perform()
    return fs.Solid()


def _ladder(src_solid):
    """(name, thunk) heals, cheapest / most geometry-preserving first."""
    yield "shapefix", lambda: _shapefix(src_solid())
    yield "defeature", lambda: _defeature(src_solid())
    for tol in _SEW_TOLS:
        yield f"dropsew_tol_{tol}", (lambda t: lambda: _dropsew(src_solid(), t))(tol)


def recover(session, object_name: str = "", store_as: str = "") -> str:
    """Heal an invalid solid so it passes the validity gate, and re-register it.

    Runs a heal ladder (ShapeFix → defeature invalid faces → drop+sew ladder) and
    keeps the first variant that passes the exact gate AND leaves the volume and
    bbox essentially unchanged; a distorting heal is refused so the geometry is
    never silently mangled. Requires a single solid. object_name: named object
    from show() (default: current shape). store_as: name to register the healed
    solid under (default: overwrite object_name / the current shape). Reports the
    method that worked, the volume/bbox change (so you can confirm an edit
    survived), and every rung tried.
    """
    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err

    # recover heals one solid: it measures volume/bbox preservation and re-gates
    # against that single body. A multi-solid shape is ambiguous (healing one body
    # while silently dropping another would look like a recovery), so refuse it
    # rather than guess — fuse or separate the bodies first.
    solids = _solids(shape.wrapped)
    if len(solids) != 1:
        return json.dumps(
            {
                "error": f"recover heals a single solid, but '{object_name or 'current shape'}' "
                f"has {len(solids)} solids. Fuse them (Part +) or recover each body separately.",
                "n_solids": len(solids),
            }
        )

    src = solids[0]
    v0 = _vol(src)
    diag0 = _bbox_diag(src)

    rep0 = _gate_report(Solid(src), exact=True)
    if rep0["passes_gate"]:
        return "Recovery: PASS — already valid, no recovery needed.\n" + json.dumps(rep0, indent=2)

    attempts = []
    for name, thunk in _ladder(lambda: _first_solid(shape.wrapped)):
        try:
            cand = thunk()
        except Exception as exc:  # noqa: BLE001 - record and keep trying
            attempts.append({"method": name, "result": f"error: {repr(exc)[:120]}"})
            continue
        if cand is None:
            attempts.append({"method": name, "result": "no-op"})
            continue
        if _invalid_faces(cand):
            attempts.append({"method": name, "result": "BRepCheck still invalid"})
            continue
        rep = _gate_report(Solid(cand), exact=True)
        if not rep["passes_gate"]:
            attempts.append({"method": name, "result": "still fails gate", "reasons": rep["reasons"]})
            continue
        # Passed the gate — but only accept it if the geometry didn't move. A heal
        # that distorts the part is a fake recovery: refuse it rather than hand
        # back a mangled solid that merely happens to be watertight.
        dv = (_vol(cand) - v0) / v0 if v0 else 0.0
        dd = (_bbox_diag(cand) - diag0) / diag0 if diag0 else 0.0
        if abs(dv) > _MAX_VOL_FRAC or abs(dd) > _MAX_BBOX_FRAC:
            attempts.append(
                {
                    "method": name,
                    "result": "rejected — alters geometry",
                    "volume_delta_pct": round(dv * 100, 3),
                    "bbox_delta_pct": round(dd * 100, 3),
                }
            )
            continue
        healed = Solid(cand)
        dest = store_as or object_name or "recovered"
        session.objects[dest] = healed
        session.current_shape = healed
        out = {
            "recovered": True,
            "method": name,
            "stored_as": dest,
            "volume_before": round(v0, 2),
            "volume_after": round(_vol(cand), 2),
            "volume_delta_pct": round(dv * 100, 3),
            "bbox_delta_pct": round(dd * 100, 3),
            "attempts": attempts,
            "gate": rep,
        }
        return (
            f"Recovery: PASS via {name} (Δvol {dv * 100:+.3f}%, Δbbox {dd * 100:+.3f}%). "
            f"Stored as '{dest}'.\n" + json.dumps(out, indent=2)
        )

    out = {"recovered": False, "invalid_faces": len(_invalid_faces(src)), "attempts": attempts}
    return (
        "Recovery: FAIL — no geometry-preserving heal cleared the gate; the original shape is "
        "left untouched. The defect may be intrinsic (e.g. a self-intersecting input face); "
        "inspect it with measure()/render and fix it in execute(), or rebuild the region.\n"
        + json.dumps(out, indent=2)
    )
