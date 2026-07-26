"""Out-of-process tessellation worker for render_view (see tools/render.py).

Run as ``python -m build123d_mcp._tessellate_subprocess <manifest.json> <out.pkl>
<linear_deflection> <angular_deflection>``. The manifest is ``[{"name","step"}]``;
each STEP is imported and tessellated, and the result ``{name: (verts, tris)}``
is pickled to ``out.pkl``.

Why a separate program (not ``multiprocessing``): the worker runs as a daemon,
and daemon processes cannot spawn ``multiprocessing`` children — so the only way
to bound OCC's un-interruptible ``BRepMesh`` (which can run for minutes on a
complex part and blow the worker op-timeout, SIGKILLing the session) is a real
subprocess the parent can hard-kill via ``subprocess.run(timeout=...)``. Mirrors
the export mesh gate (``_gate_subprocess``).
"""

import json
import pickle
import sys


def tessellate_shape_faces(shape, lin: float, ang: float) -> tuple[list, list, list[int]]:
    """Tessellate a B-rep face-by-face, skipping faces OCC could not mesh.

    STL-derived shapes can contain renderable mesh data without topological OCC
    faces. Keep build123d's native mesh path for that representation.
    """
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    # Shape.mesh() preserves an existing imported mesh and only invokes OCC's
    # remesher when the shape does not already have usable triangulation.
    shape.mesh(lin, ang)
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape.wrapped, TopAbs_FACE, faces)
    if faces.Size() == 0:
        verts, tris = shape.tessellate(lin, ang)
        return (
            [(vertex.X, vertex.Y, vertex.Z) for vertex in verts],
            [tuple(triangle) for triangle in tris],
            [],
        )

    vertices: list = []
    triangles: list = []
    missing_faces: list[int] = []
    for fi in range(1, faces.Size() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            missing_faces.append(fi)
            continue

        base = len(vertices)
        trsf = loc.Transformation()
        for ni in range(1, tri.NbNodes() + 1):
            point = tri.Node(ni).Transformed(trsf)
            vertices.append((point.X(), point.Y(), point.Z()))

        reversed_face = face.Orientation() == TopAbs_REVERSED
        for ti in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(ti).Get()
            if reversed_face:
                a, b = b, a
            triangles.append((base + a - 1, base + b - 1, base + c - 1))

    # STEP-wrapped STL shells can expose topological faces whose attached OCC
    # triangulations contain no triangles, while build123d can still recover the
    # original mesh. This is distinct from an actual missing (`None`) face mesh.
    if not triangles and not missing_faces:
        verts, tris = shape.tessellate(lin, ang)
        return (
            [(vertex.X, vertex.Y, vertex.Z) for vertex in verts],
            [tuple(triangle) for triangle in tris],
            [],
        )

    return vertices, triangles, missing_faces


def _missing_faces_message(name: str, missing_faces: list[int], rendered: bool) -> str:
    indices = ", ".join(str(index) for index in missing_faces)
    outcome = "rendered remaining faces" if rendered else "no renderable faces remain"
    return f"{name}: missing triangulation for face(s) {indices}; {outcome}"


def main(
    manifest_path: str, out_path: str, linear_deflection: str, angular_deflection: str
) -> None:
    from build123d import import_step

    lin = float(linear_deflection)
    ang = float(angular_deflection)
    manifest = json.loads(open(manifest_path).read())

    meshes: dict = {}
    failed: list = []
    for item in manifest:
        try:
            shape = import_step(item["step"])
            verts, tris, missing_faces = tessellate_shape_faces(shape, lin, ang)
            if tris or not missing_faces:
                meshes[item["name"]] = (verts, tris)
            if missing_faces:
                failed.append(_missing_faces_message(item["name"], missing_faces, bool(tris)))
        except Exception as exc:  # noqa: BLE001 - one bad shape shouldn't fail the render
            failed.append(f"{item['name']}: {exc}")

    with open(out_path, "wb") as f:
        pickle.dump({"meshes": meshes, "failed": failed}, f, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
