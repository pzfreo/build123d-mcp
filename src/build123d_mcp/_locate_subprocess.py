"""Out-of-process defect locator for ``locate_gate_defects`` (see tools/locate.py).

Run as ``python -m build123d_mcp._locate_subprocess <in.step> <out.json>``. Imports
the STEP, runs the same checks the validity gate uses, and writes a JSON list of
defects **with 3D coordinates** — so the agent can repair a specific edge/face
instead of guessing blindly (the validate/export gate reports only counts).

Why a subprocess: the mesh non-manifold check tessellates (OCC ``BRepMesh``, an
un-interruptible native call that can run for minutes on a complex part). The
worker is a daemon, so — like the export gate and the render fix — this runs as a
real ``subprocess.run`` the parent hard-bounds, never blocking/SIGKILLing the
worker. The B-rep checks (BRepCheck, edge→face map) are cheap and run here too.
"""

import json
import math
import sys


def _first_solid(shp):
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    e = TopExp_Explorer(shp, TopAbs_SOLID)
    return TopoDS.Solid_s(e.Current()) if e.More() else shp


def _brep_invalid_faces(solid) -> list:
    """BRepCheck-invalid faces, each with face index, center, surface type, status."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    a = BRepCheck_Analyzer(solid)
    out = []
    e = TopExp_Explorer(solid, TopAbs_FACE)
    idx = -1
    while e.More():
        idx += 1
        f = TopoDS.Face_s(e.Current())
        if not a.IsValid(f):
            g = GProp_GProps()
            BRepGProp.SurfaceProperties_s(f, g)
            c = g.CentreOfMass()
            try:
                status = [str(s).split(".")[-1] for s in a.Result(f).Status()]
            except Exception:  # noqa: BLE001 - status is best-effort detail
                status = []
            surf = str(BRepAdaptor_Surface(f).GetType()).split(".")[-1].replace("GeomAbs_", "")
            out.append(
                {
                    "kind": "brep_invalid_face",
                    "face_index": idx,
                    "where": [round(c.X(), 3), round(c.Y(), 3), round(c.Z(), 3)],
                    "surface": surf,
                    "status": status,
                    "hint": "malformed face — defeature it (recover) or rebuild the patch",
                }
            )
        e.Next()
    return out


def _brep_edge_defects(solid) -> list:
    """Open (1-face) and non-manifold (>2-face) B-rep edges, with edge midpoint."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, m)
    out = []
    for i in range(1, m.Extent() + 1):
        edge = TopoDS.Edge_s(m.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):  # seam/pole edges aren't boundaries
            continue
        faces = m.FindFromIndex(i).Extent()
        if faces != 1 and faces <= 2:  # 0 = free wire (PMI); 2 = manifold
            continue
        g = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, g)
        c = g.CentreOfMass()
        if faces == 1:
            kind = "open_edge"
            hint = "open boundary — an unsewn or missing face; sew the shell or add the face"
        else:
            kind = "nonmanifold_edge"
            hint = (
                "edge shared by 3+ faces — a self-touch / coincident face from a boolean; "
                "cut a thin relief here or redo the cut"
            )
        out.append(
            {
                "kind": kind,
                "where": [round(c.X(), 3), round(c.Y(), 3), round(c.Z(), 3)],
                "faces_incident": faces,
                "hint": hint,
            }
        )
    return out


def _mesh_nonmanifold_edges(shape) -> list:
    """Mesh edges shared by >2 triangles (BRepCheck-valid but a scorer rejects),
    welded by rounded coordinate exactly as the gate's check, with edge midpoint."""
    from collections import Counter

    bb = shape.bounding_box()
    diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
    if diag <= 0:
        return []
    verts, tris = shape.tessellate(max(diag * 1e-3, 1e-4))
    if not verts or not tris:
        return []
    q = diag * 1e-5
    remap: list[int] = []
    keys: dict = {}
    coord: dict = {}  # welded index -> representative (x, y, z)
    for v in verts:
        k = (round(v.X / q), round(v.Y / q), round(v.Z / q))
        wi = keys.setdefault(k, len(keys))
        remap.append(wi)
        coord.setdefault(wi, (v.X, v.Y, v.Z))
    edge_count: Counter = Counter()
    for t in tris:
        a, b, c = remap[t[0]], remap[t[1]], remap[t[2]]
        if len({a, b, c}) < 3:
            continue
        for e in ((a, b), (b, c), (a, c)):
            edge_count[tuple(sorted(e))] += 1
    out = []
    for (a, b), n in edge_count.items():
        if n > 2:
            pa, pb = coord[a], coord[b]
            mid = [round((pa[i] + pb[i]) / 2, 3) for i in range(3)]
            out.append(
                {
                    "kind": "mesh_nonmanifold_edge",
                    "where": mid,
                    "shared_by_triangles": n,
                    "hint": (
                        "two surface sheets meet >2-ways here (BRepCheck-valid but a CAD scorer "
                        "rejects it) — a self-touch; cut a thin relief or redo the boolean"
                    ),
                }
            )
    return out


def collect_defects(shape) -> list:
    """Run all three locators on a build123d shape and return the defect list.

    Shared by the subprocess ``main`` and the in-process fallback in the tool
    (used when the host blocks child-process creation). One failing check records
    a ``locator_error`` rather than losing the others.
    """
    solid = _first_solid(shape.wrapped)
    defects: list = []
    for finder in (
        lambda: _brep_invalid_faces(solid),
        lambda: _brep_edge_defects(solid),
        lambda: _mesh_nonmanifold_edges(shape),
    ):
        try:
            defects += finder()
        except Exception as exc:  # noqa: BLE001 - one check failing shouldn't lose the rest
            defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    return defects


def main(in_step: str, out_json: str) -> None:
    from build123d import import_step

    defects = collect_defects(import_step(in_step))
    with open(out_json, "w") as f:
        json.dump({"defects": defects}, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
