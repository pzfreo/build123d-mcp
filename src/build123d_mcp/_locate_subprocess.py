"""Out-of-process defect locator for ``locate_gate_defects`` (see tools/locate.py).

Run as ``python -m build123d_mcp._locate_subprocess <in.step> <out.json> [budget_s]``.
Imports the STEP, runs the same checks the validity gate uses, and writes a JSON list
of defects **with 3D coordinates** — so the agent can repair a specific edge/face
instead of guessing blindly (the validate/export gate reports only counts).
``budget_s`` is the wall-clock the parent will wait before killing us; the mesh
checks bound themselves below it so a slow check returns a per-check error instead
of losing every defect the cheap checks already found.

Why a subprocess: the mesh non-manifold check tessellates (OCC ``BRepMesh``, an
un-interruptible native call that can run for minutes on a complex part). The
worker is a daemon, so — like the export gate and the render fix — this runs as a
real ``subprocess.run`` the parent hard-bounds, never blocking/SIGKILLing the
worker. The B-rep checks (BRepCheck, edge→face map) are cheap and run here too.
"""

import json
import math
import sys
import time

# Base triangle budget for the vertex-deflection exact pass. The wall-clock bound
# comes from the caller's deadline (the parent's own subprocess budget, less the
# margin below); with no deadline the gate applies its own _GATE_MESH_BUDGET_S.
# Either way the deadline is finite, which is what activates the gate's finer-rung
# triangle ceiling — so the same bounds apply when process creation is unavailable
# and collect_defects() runs in-process.
_VERTEX_DEFLECTION_MAX_TRIS = 300_000

# Wall-clock reserved out of the parent's budget for importing the STEP and writing
# the JSON, so we finish reporting before the parent's hard kill lands.
_OUTPUT_MARGIN_S = 5.0


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
                    "hint": (
                        "malformed face — remove/replace this local face explicitly in "
                        "execute() or rebuild the patch"
                    ),
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


def _weld(shape) -> tuple[list, dict]:
    """Tessellate and coordinate-weld (exactly as the gate's mesh check). Returns
    ``(welded_tris, coord)`` where welded_tris is a list of (a, b, c) welded-node
    triangles and coord maps a welded node to a representative (x, y, z)."""
    bb = shape.bounding_box()
    diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
    if diag <= 0:
        return [], {}
    verts, tris = shape.tessellate(max(diag * 1e-3, 1e-4))
    if not verts or not tris:
        return [], {}
    q = diag * 1e-5
    remap: list[int] = []
    keys: dict = {}
    coord: dict = {}  # welded index -> representative (x, y, z)
    for v in verts:
        k = (round(v.X / q), round(v.Y / q), round(v.Z / q))
        wi = keys.setdefault(k, len(keys))
        remap.append(wi)
        coord.setdefault(wi, (v.X, v.Y, v.Z))
    welded = []
    for t in tris:
        a, b, c = remap[t[0]], remap[t[1]], remap[t[2]]
        if len({a, b, c}) == 3:  # drop degenerate triangles after welding
            welded.append((a, b, c))
    return welded, coord


def _mesh_nonmanifold_edges(welded, coord) -> list:
    """Mesh edges shared by >2 triangles (self-touch a scorer rejects), with midpoint."""
    from collections import Counter

    edge_count: Counter = Counter()
    for a, b, c in welded:
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


def _mesh_open_edges(welded, coord) -> list:
    """Mesh edges shared by exactly 1 triangle (an unclosed tessellated boundary —
    ``mesh_open_edges`` in the gate report), with midpoint. Same welded mesh as
    ``_mesh_nonmanifold_edges``; a coordinate weld rather than the gate's own
    topology-stitched exact check, so treat a location here as a starting point to
    inspect, not a guaranteed match to the gate's count — re-check with the export
    gate after any fix."""
    from collections import Counter

    edge_count: Counter = Counter()
    for a, b, c in welded:
        for e in ((a, b), (b, c), (a, c)):
            edge_count[tuple(sorted(e))] += 1
    out = []
    for (a, b), n in edge_count.items():
        if n == 1:
            pa, pb = coord[a], coord[b]
            mid = [round((pa[i] + pb[i]) / 2, 3) for i in range(3)]
            out.append(
                {
                    "kind": "mesh_open_edge",
                    "where": mid,
                    "hint": (
                        "unclosed tessellated boundary here — an unsewn or missing face; "
                        "sew the shell or add the face"
                    ),
                }
            )
    return out


def _mesh_nonmanifold_vertices(welded, coord) -> list:
    """Mesh vertices where ≥2 surface sheets meet at a single point (corner-to-corner
    touch) — edge-manifold and watertight but not a 2-manifold surface, which a CAD
    scorer rejects (#298). Mirrors the gate's _nonmanifold_vertex_count: a manifold
    vertex's incident triangles form one connected fan, a pinch forms ≥2."""
    from collections import defaultdict

    spokes: dict = defaultdict(list)
    for a, b, c in welded:
        spokes[a].append((b, c))
        spokes[b].append((a, c))
        spokes[c].append((a, b))
    out = []
    for vi, edges in spokes.items():
        par: dict = {}

        def root(x: int, par: dict = par) -> int:
            par.setdefault(x, x)
            r = x
            while par[r] != r:
                r = par[r]
            while par[x] != r:
                par[x], x = r, par[x]
            return r

        for a, b in edges:
            ra, rb = root(a), root(b)
            if ra != rb:
                par[ra] = rb
        if len({root(x) for x in par}) > 1:
            c = coord[vi]
            out.append(
                {
                    "kind": "mesh_nonmanifold_vertex",
                    "where": [round(c[0], 3), round(c[1], 3), round(c[2], 3)],
                    "hint": (
                        "two surface sheets meet at a single point (corner-to-corner touch) — "
                        "BRepCheck-valid but a CAD scorer rejects it; separate the bodies or add "
                        "material so they fuse into one manifold solid"
                    ),
                }
            )
    return out


def _mesh_vertex_deflection_defects(shape, deadline: float | None = None) -> list:
    """Return the exact gate's vertex-deflection evidence with coordinates.

    The gate owns the deflection ladder and stops at the first rung that closes
    the tessellated boundary. Reusing its result here prevents the locator from
    missing defects found only at a visited finer rung or inventing defects from
    finer rungs the gate never reached.
    """
    from build123d_mcp.tools.validate import _mesh_defects_exact

    evidence: dict[tuple[float, float, float], tuple[float, float]] = {}
    result = _mesh_defects_exact(
        shape,
        max_triangles=_VERTEX_DEFLECTION_MAX_TRIS,
        deadline=deadline,
        vertex_deflection_evidence=evidence,
        run_refined_probe=False,
    )
    if not result.ok:
        # The gate could not reach a verdict — too large to mesh/stitch in budget,
        # or open at the base rung and above the ladder's base-triangle cap so the
        # finer rungs never ran. Either way the ladder-only vertices this locator
        # exists to find were not examined; say so rather than return an empty list
        # the caller would read as "no vertex-deflection defects".
        raise RuntimeError(
            "the exact mesh gate could not analyse this shape within its triangle/time "
            "budget, so vertex-deflection defects were not located — other locators "
            "still ran"
        )

    out = []
    for (x, y, z), (worst, deflection) in sorted(evidence.items()):
        out.append(
            {
                "kind": "mesh_vertex_deflection_defect",
                "where": [round(x, 3), round(y, 3), round(z, 3)],
                "max_deviation_mm": round(worst, 4),
                "deflection_mm": round(deflection, 6),
                "hint": (
                    "a tessellated edge endpoint here misses this BREP vertex by "
                    f"{worst:.3g}mm (> deflection {deflection:.3g}mm) — a patched/healed "
                    "face's boundary is topologically closed but geometrically off-vertex; "
                    "re-patch or re-sew this face at a tighter tolerance"
                ),
            }
        )
    return out


def _mesh_untriangulated_faces(shape) -> list:
    """Faces missing triangulation at the gate's base deflection, with coordinates."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    occ = shape.wrapped
    bb = shape.bounding_box()
    diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
    if diag <= 0:
        return []
    deflection = min(0.5, max(0.005, diag * 1e-3))
    BRepTools.Clean_s(occ)
    BRepMesh_IncrementalMesh(occ, deflection, False, 0.5, True)

    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(occ, TopAbs_FACE, faces)
    out = []
    for fi in range(1, faces.Size() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        if BRep_Tool.Triangulation_s(face, TopLoc_Location()) is not None:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        center = props.CentreOfMass()
        out.append(
            {
                "kind": "mesh_untriangulated_face",
                "face_index": fi,
                "where": [round(center.X(), 3), round(center.Y(), 3), round(center.Z(), 3)],
                "deflection_mm": round(deflection, 6),
                "hint": (
                    "this face has no triangulation at the gate's base tolerance; "
                    "simplify or rebuild the local face before export"
                ),
            }
        )
    return out


def _mesh_refined_untriangulated_faces(shape) -> list:
    """Faces that tessellate at the gate's base deflection but fail at base/4.

    This mirrors the refined probe in ``tools.validate._mesh_defects_exact`` and
    reports a face center for repair. It deliberately does not stitch the mesh; the
    defect is simply that OCC cannot provide a per-face triangulation at the finer
    tolerance a downstream consumer may request.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from build123d_mcp.tools.validate import (
        _REFINED_UNTRIANGULATED_FACTOR,
        _REFINED_UNTRIANGULATED_MAX_TRIS,
    )

    occ = shape.wrapped
    bb = shape.bounding_box()
    diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
    if diag <= 0:
        return []
    base_deflection = min(0.5, max(0.005, diag * 1e-3))
    refined_deflection = base_deflection / _REFINED_UNTRIANGULATED_FACTOR

    # The defect class is "present at the base gate deflection, missing after a
    # modest refinement". Clear any cached triangulation first so an in-process
    # fallback after another mesh-heavy tool does not mistake a retained refined
    # mesh for the base pass.
    BRepTools.Clean_s(occ)
    BRepMesh_IncrementalMesh(occ, base_deflection, False, 0.5, True)

    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(occ, TopAbs_FACE, faces)
    base_triangulated: set[int] = set()
    base_tris = 0
    for fi in range(1, faces.Size() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        base_triangulated.add(fi)
        base_tris += tri.NbTriangles()
    if not base_triangulated:
        return []
    if base_tris > _REFINED_UNTRIANGULATED_MAX_TRIS:
        raise RuntimeError(
            f"shape too large for the refined untriangulated-face locator ({base_tris} "
            f"base triangles > {_REFINED_UNTRIANGULATED_MAX_TRIS}) — skipped, other locators still ran"
        )

    BRepMesh_IncrementalMesh(occ, refined_deflection, False, 0.5, True)

    out = []
    n_tris = 0
    for fi in range(1, faces.Size() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            n_tris += tri.NbTriangles()
            continue
        if fi not in base_triangulated:
            continue
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, g)
        c = g.CentreOfMass()
        out.append(
            {
                "kind": "mesh_refined_untriangulated_face",
                "face_index": fi,
                "where": [round(c.X(), 3), round(c.Y(), 3), round(c.Z(), 3)],
                "deflection_mm": round(refined_deflection, 6),
                "hint": (
                    "this face meshes at the base gate deflection but fails at a finer "
                    "tolerance; treat it as a fragile/unmeshable sliver and re-patch or "
                    "re-sew the local face before export"
                ),
            }
        )
    if n_tris > _REFINED_UNTRIANGULATED_MAX_TRIS and not out:
        raise RuntimeError(
            f"shape too large for the refined untriangulated-face locator ({n_tris} "
            f"triangles > {_REFINED_UNTRIANGULATED_MAX_TRIS}) — skipped, other locators still ran"
        )
    return out


def collect_defects(shape, deadline: float | None = None) -> list:
    """Run every locator on a build123d shape and return the defect list.

    B-rep checks run on the WHOLE shape (not just the first solid) so a multi-solid
    compound's later bodies aren't missed — matching the gate, which checks the
    whole shape. The mesh soup is welded once and shared by the edge + vertex
    checks. One failing check records a ``locator_error`` rather than losing the
    rest. Shared by the subprocess ``main`` and the tool's in-process fallback.

    ``deadline`` is a ``time.monotonic()`` instant the exact vertex-deflection pass
    must stay under — the caller's own budget, so that pass cannot overrun the hard
    kill (or the worker op-timeout on the in-process path) and lose the defects the
    cheap checks already collected. ``None`` leaves the gate's own budget in force.
    """
    defects: list = []
    for finder in (
        lambda: _brep_invalid_faces(shape.wrapped),
        lambda: _brep_edge_defects(shape.wrapped),
    ):
        try:
            defects += finder()
        except Exception as exc:  # noqa: BLE001 - one check failing shouldn't lose the rest
            defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    try:
        welded, coord = _weld(shape)
        defects += _mesh_open_edges(welded, coord)
        defects += _mesh_nonmanifold_edges(welded, coord)
        defects += _mesh_nonmanifold_vertices(welded, coord)
    except Exception as exc:  # noqa: BLE001
        try:
            base_untriangulated = _mesh_untriangulated_faces(shape)
        except Exception:  # noqa: BLE001 - preserve the original, more useful failure
            base_untriangulated = []
        if base_untriangulated:
            defects += base_untriangulated
        else:
            defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    try:
        defects += _mesh_vertex_deflection_defects(shape, deadline)
    except Exception as exc:  # noqa: BLE001
        defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    try:
        defects += _mesh_refined_untriangulated_faces(shape)
    except Exception as exc:  # noqa: BLE001
        defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    return defects


def main(in_step: str, out_json: str, budget_s: float | None = None) -> None:
    from build123d import import_step

    deadline = None if budget_s is None else time.monotonic() + budget_s - _OUTPUT_MARGIN_S
    defects = collect_defects(import_step(in_step), deadline)
    with open(out_json, "w") as f:
        json.dump({"defects": defects}, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else None)
