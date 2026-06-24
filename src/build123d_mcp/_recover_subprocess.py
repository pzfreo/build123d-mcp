"""Out-of-process heal worker for ``recover()`` (see tools/recover.py).

Run as ``python -m build123d_mcp._recover_subprocess <in.step> <out.step>``. The
parent bounds this whole process with a hard ``subprocess`` timeout, so the
un-interruptible OCC defeature/ShapeFix native calls can never block the worker
past its op budget (which would kill the session). On success the accepted healed
solid is written to ``out.step`` and a ``RECOVER_RESULT:{...}`` line is printed.

The heal ladder is deliberately small and geometry-preserving: ``ShapeFix_Shape``
then **defeature** the BRepCheck-invalid faces. A candidate is accepted only if
the *reimported* STEP (the authoritative artifact) passes the exact gate AND it
preserves the geometry to within a **surface-deviation (Hausdorff) bound** — no
point of either surface moves more than ``max(_HAUSDORFF_ABS, _HAUSDORFF_REL ×
diagonal)``.

Why Hausdorff, not bbox/volume. The two cheap proxies tried before both have a
blind spot a distorting heal can hide in: a bounding-box guard is blind to the
loss of an *internal* feature (defeaturing away a Ø16 through-bore fills the part
solid — volume +14% — while the bbox does not move at all), and a signed-volume
guard is unreliable on the very defect class we target (a reversed face halves
it). The symmetric surface Hausdorff is the actual invariant a faithful heal must
satisfy: *no surface point moved more than ε*. The removed bore wall sits ~12 mm
from the nearest surface of the filled solid, so it is caught; a faithful sliver
defeature moves the surface sub-millimetre, so it passes.

The deviation is computed by sampling both surfaces densely (triangulation nodes
+ triangle centroids, so planar-face interiors are covered, not just corners) and
taking the max over both directions of each sample's nearest-neighbour distance
to the other surface (a KD-tree over the point sets — fast even for ~100k
points). Point-to-sample distance can only over-state the true distance, so the
guard never under-reports a distortion. The whole computation runs inside the
parent's hard time bound, so on a part too large to check in budget the
subprocess is simply killed and recover() FAILs with the geometry untouched.
"""

import json
import sys
import time

import numpy as np
from build123d import Solid, export_step, import_step
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.ShapeFix import ShapeFix_Shape
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_ListOfShape
from scipy.spatial import cKDTree

from build123d_mcp.tools.validate import _gate_report

# Accept a heal only if no surface point moved more than this. Absolute floor for
# small parts; relative term (fraction of the bbox diagonal) scales it up for
# large ones. Tuned against the benchmark: a faithful defeature genuinely extends
# the neighbour faces to close the gap left by the removed defect (≈1.1 mm of
# surface movement on fixture 217, diag 193 mm → ε≈1.93 mm, passes), while losing
# a real internal feature moves the surface far more (a filled Ø16 bore ≈16 mm,
# refused) — a wide margin between the two.
_HAUSDORFF_ABS = 1.0
_HAUSDORFF_REL = 0.01


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


def _faces(s):
    out = []
    e = TopExp_Explorer(s, TopAbs_FACE)
    while e.More():
        out.append(TopoDS.Face_s(e.Current()))
        e.Next()
    return out


def _invalid_faces(s):
    a = BRepCheck_Analyzer(s)
    return [f for f in _faces(s) if not a.IsValid(f)]


def _bbox6(s):
    b = Bnd_Box()
    BRepBndLib.Add_s(s, b)
    return b.Get()  # (xmin, ymin, zmin, xmax, ymax, zmax)


def _diag(bb):
    x0, y0, z0, x1, y1, z1 = bb
    return ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5


def _mesh(solid, lin):
    # Clean first: BRepMesh is *incremental*, so a coarser triangulation left on the
    # solid by an earlier gate would be kept, leaving the surface sparsely sampled
    # and inflating the point-to-sample distance. Cleaning forces a fresh mesh at
    # exactly the requested deflection, so the sampling density is deterministic.
    BRepTools.Clean_s(solid)
    BRepMesh_IncrementalMesh(solid, lin, False, 0.5, True)


def _surface_points(solid, lin):
    """Mesh the solid and return an (N, 3) array of surface sample points: every
    triangulation node plus every triangle centroid. The centroids matter because
    a planar face tessellates to its corners only — without them a flat-face
    interior would be unsampled. Best-effort: a face that will not triangulate
    (e.g. an un-meshable defect) is skipped; the heal's change still shows up on
    the faces that do mesh and on the other solid (the symmetric direction)."""
    _mesh(solid, lin)
    pts = []
    for f in _faces(solid):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        nodes = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
        for p in nodes:
            pts.append((p.X(), p.Y(), p.Z()))
        for ti in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(ti).Get()
            p1, p2, p3 = nodes[a - 1], nodes[b - 1], nodes[c - 1]
            pts.append(
                (
                    (p1.X() + p2.X() + p3.X()) / 3,
                    (p1.Y() + p2.Y() + p3.Y()) / 3,
                    (p1.Z() + p2.Z() + p3.Z()) / 3,
                )
            )
    return np.asarray(pts, dtype=float) if pts else np.empty((0, 3))


def _surface_deviation(src, cand, lin):
    """An upper bound on the symmetric surface Hausdorff distance between the input
    and the healed solid: sample both surfaces densely and take the max, over both
    directions, of each sample's nearest-neighbour distance to the other surface's
    samples (via a KD-tree). Both directions matter — src→cand catches feature
    *loss* (a filled bore leaves its wall ~12 mm from any remaining surface),
    cand→src catches feature *addition* (material grown outward by a fix).

    Point-to-sample distance can only *over*-state the true point-to-surface
    distance (the samples lie on the surface), so the guard never under-reports a
    distortion — the safe direction. Dense sampling (deflection well below ε) keeps
    the over-statement small enough that a faithful heal still passes. Measured on
    the in-memory healed solid (``cand``); STEP write/read preserves exact B-rep
    geometry to sub-micron, far below ε, and the *validity* gate runs on the
    reimported artifact. Returns ``inf`` if either surface cannot be sampled at all
    (cannot verify fidelity → caller refuses)."""
    pa = _surface_points(src, lin)
    pb = _surface_points(cand, lin)
    if len(pa) == 0 or len(pb) == 0:
        return float("inf")
    d_ab = cKDTree(pb).query(pa)[0].max()
    d_ba = cKDTree(pa).query(pb)[0].max()
    return float(max(d_ab, d_ba))


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


def _gate_pass(solid):
    return _gate_report(Solid(solid), exact=True)["passes_gate"]


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
    eps = max(_HAUSDORFF_ABS, _HAUSDORFF_REL * _diag(bb0))
    # Sample finer than the tolerance so the point-to-sample over-statement stays
    # well under ε and a faithful heal is not falsely rejected.
    lin = max(0.2, 0.25 * eps)
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
        # memory (the bug export's reimport gate guards against). Time the reimport:
        # the parent must do the same load to bring the solid into the session, and
        # that load is itself an un-interruptible OCC call, so it reports the cost
        # back so the parent can refuse it if too little budget remains.
        export_step(Solid(cand), out_step)
        t_reimp = time.monotonic()
        reimp = _first_solid(import_step(out_step).wrapped)
        reimport_secs = round(time.monotonic() - t_reimp, 3)
        if reimp is None or not _gate_pass(reimp):
            attempts.append({"method": name, "result": "reimported artifact still fails gate"})
            continue
        try:
            hd = _surface_deviation(src, cand, lin)
        except Exception as exc:  # noqa: BLE001 - can't verify fidelity -> refuse
            attempts.append({"method": name, "result": f"fidelity check failed: {repr(exc)[:120]}"})
            continue
        if hd > eps:
            attempts.append(
                {"method": name, "result": "rejected — alters geometry", "hausdorff": round(hd, 4)}
            )
            continue
        _emit(
            {
                "status": "recovered",
                "method": name,
                "hausdorff": round(hd, 4),
                "eps": round(eps, 4),
                "reimport_secs": reimport_secs,
                "attempts": attempts,
            }
        )
        return

    _emit({"status": "failed", "attempts": attempts})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
