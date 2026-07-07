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

# Triangle-count budget for the vertex-deflection check specifically (the other
# checks here share one cheap welded mesh already built by _weld()). Matches
# tools/validate.py's _EXACT_ISOLATED_MAX_TRIS — this subprocess is bounded the
# same way _gate_subprocess.py's isolated exact check is, by a hard external
# subprocess timeout rather than an in-loop deadline, so a triangle ceiling
# (checked once, before the per-edge walk) is the right-sized guard here.
_VERTEX_DEFLECTION_MAX_TRIS = 300_000


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


def _mesh_vertex_deflection_defects(shape) -> list:
    """BREP vertices where a tessellated edge-polygon endpoint misses the vertex's
    analytic position by more than the mesh deflection (``mesh_vertex_deflection_defects``
    in the gate report), with the vertex's own coordinates.

    Unlike the other mesh checks here, this is NOT a coordinate weld — welding would
    hide exactly this defect by merging the mismatched points, the same way the gate's
    old vertex-merge code silently unioned them. It walks each edge's per-face
    ``PolygonOnTriangulation`` (the true topology-stitch data) and checks its first/last
    node against ``TopExp.FirstVertex_s``/``LastVertex_s`` directly — the same guard
    ``_mesh_defects_exact`` runs (same max-per-axis deflection comparison, not Euclidean —
    they must agree, or this locator can flag a "defect" the authoritative gate does not
    actually fail on), and the same check CADGenBench's own mesh sanity validator uses. A
    patched/healed face (a sliver sew, a tolerance-fudged patch) whose boundary is
    topologically closed but geometrically off-vertex reads as fine to a coordinate-weld
    check and to BRepCheck, yet fails a CAD scorer's own mesh gate.

    Triangle-budgeted (``_VERTEX_DEFLECTION_MAX_TRIS``): raises rather than running
    unbounded on a huge shape, so ``collect_defects()``'s try/except records a
    ``locator_error`` for this one check instead of risking the whole subprocess's
    external timeout — which would otherwise lose every defect the other, already-
    cheap checks in the same run had already found.

    This re-tessellates rather than reusing ``_weld()``'s triangulation from moments
    earlier in ``collect_defects()`` — tried making it reuse ``_weld()``'s finer
    ``angular_tolerance=0.1`` (via ``shape.mesh()``, so OCC's own "already meshed"
    check would skip the second pass) and measured it directly: on the real submission
    this check was written to catch, that finer angle balloons the tessellation from
    42722 triangles (at this function's own ``angular=0.5``, matching
    ``_mesh_defects_exact``) to 611387 — 14x worse, not a saving. The two checks need
    different tessellation density for legitimate reasons (this one mirrors the exact
    gate's coarser, cheaper setting on purpose), so a second full pass at the coarser
    setting is the correct tradeoff; the triangle budget above is what actually bounds
    the worst case, not tessellation sharing."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from build123d_mcp.tools.validate import _edge_face_adjacency

    occ = shape.wrapped
    bb = shape.bounding_box()
    diag = math.dist((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
    if diag <= 0:
        return []
    deflection = min(0.5, max(0.005, diag * 1e-3))
    BRepMesh_IncrementalMesh(occ, deflection, False, 0.5, True)

    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(occ, TopAbs_FACE, faces)
    face_tri = {}
    n_tris = 0
    for fi in range(1, faces.Size() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            face_tri[fi] = (tri, loc)
            n_tris += tri.NbTriangles()
    if not face_tri:
        return []
    if n_tris > _VERTEX_DEFLECTION_MAX_TRIS:
        raise RuntimeError(
            f"shape too large for the vertex-deflection check ({n_tris} triangles > "
            f"{_VERTEX_DEFLECTION_MAX_TRIS}) — skipped, other locators still ran"
        )

    vmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(occ, TopAbs_VERTEX, vmap)
    emap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(occ, TopAbs_EDGE, emap)
    edge_adj = _edge_face_adjacency(occ, faces, emap)

    vertex_nodes: dict = {}  # vertex index -> list of (x, y, z) world points
    for ei in range(1, emap.Size() + 1):
        edge = TopoDS.Edge_s(emap.FindKey(ei))
        v_first = vmap.FindIndex(TopExp.FirstVertex_s(edge))
        v_last = vmap.FindIndex(TopExp.LastVertex_s(edge))
        if not v_first and not v_last:
            continue
        for fi in edge_adj.get(ei, ()):
            if fi not in face_tri:
                continue
            tri, loc = face_tri[fi]
            poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
            if poly is None:
                continue
            nodes = list(poly.Nodes())
            if not nodes:
                continue
            trsf = loc.Transformation()
            if v_first:
                p = tri.Node(nodes[0]).Transformed(trsf)
                vertex_nodes.setdefault(v_first, []).append((p.X(), p.Y(), p.Z()))
            if v_last:
                p = tri.Node(nodes[-1]).Transformed(trsf)
                vertex_nodes.setdefault(v_last, []).append((p.X(), p.Y(), p.Z()))

    out = []
    for vi, pts in vertex_nodes.items():
        vp = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vmap.FindKey(vi)))
        vx, vy, vz = vp.X(), vp.Y(), vp.Z()
        # Max-per-axis (Chebyshev), matching _mesh_defects_exact's
        # np.abs(verts[nodes] - vertex_xyz).max() exactly — NOT Euclidean.
        worst = max(max(abs(x - vx), abs(y - vy), abs(z - vz)) for x, y, z in pts)
        if worst > deflection:
            out.append(
                {
                    "kind": "mesh_vertex_deflection_defect",
                    "where": [round(vx, 3), round(vy, 3), round(vz, 3)],
                    "max_deviation_mm": round(worst, 4),
                    "hint": (
                        "a tessellated edge endpoint here misses this BREP vertex by "
                        f"{worst:.3g}mm (> deflection {deflection:.3g}mm) — a patched/healed "
                        "face's boundary is topologically closed but geometrically off-vertex; "
                        "re-patch or re-sew this face at a tighter tolerance"
                    ),
                }
            )
    return out


def collect_defects(shape) -> list:
    """Run every locator on a build123d shape and return the defect list.

    B-rep checks run on the WHOLE shape (not just the first solid) so a multi-solid
    compound's later bodies aren't missed — matching the gate, which checks the
    whole shape. The mesh soup is welded once and shared by the edge + vertex
    checks. One failing check records a ``locator_error`` rather than losing the
    rest. Shared by the subprocess ``main`` and the tool's in-process fallback.
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
        defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    try:
        defects += _mesh_vertex_deflection_defects(shape)
    except Exception as exc:  # noqa: BLE001
        defects.append({"kind": "locator_error", "detail": repr(exc)[:200]})
    return defects


def main(in_step: str, out_json: str) -> None:
    from build123d import import_step

    defects = collect_defects(import_step(in_step))
    with open(out_json, "w") as f:
        json.dump({"defects": defects}, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
