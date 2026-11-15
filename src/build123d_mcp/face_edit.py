"""Explicit local B-rep edits for execute(); no registration or implicit selection.

All OCCT work uses a private geometric copy. Only Solid values cross back into
user code; modules, builders and operation histories remain internal.
"""

import math

# A real module-level dependency also keeps this server-internal module out of
# security.py's transitively-safe user import set. OCP imports stay restricted.
from OCP.BRepOffset import BRepOffset_MakeOffset


def _vector(value):
    from build123d import Vector

    try:
        v = Vector(value)
    except Exception as exc:
        raise ValueError("expected a finite three-component vector") from exc
    if not all(math.isfinite(x) for x in v):
        raise ValueError("expected a finite three-component vector")
    return v


def _copy_selection(shape, faces):
    from build123d import Compound, Face
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

    if len(shape.solids()) != 1:
        raise ValueError("expected exactly one input Solid")
    faces = list(faces)
    if not faces or any(not isinstance(f, Face) for f in faces):
        raise ValueError("faces must be a nonempty selection of live Faces")
    if len(set(faces)) != len(faces):
        raise ValueError("duplicate selected face")
    if any(f not in shape.faces() for f in faces):
        raise ValueError("selected face does not belong to shape")
    copy = BRepBuilderAPI_Copy(shape.wrapped, True, False)
    return Compound(copy.Shape()), [Face(copy.ModifiedShape(f.wrapped)) for f in faces]


def _solid_from_shells(raw, shell_count):
    from build123d import Compound, Solid
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.ShapeFix import ShapeFix_Solid

    shells = Compound(raw).shells()
    if len(shells) != shell_count:
        raise ValueError("topology change: shell count changed (open trim or collapsed face)")
    maker = BRepBuilderAPI_MakeSolid()
    for shell in shells:
        maker.Add(shell.wrapped)
    # Orient exterior and closed void shells, without changing their surfaces.
    fix = ShapeFix_Solid(maker.Solid())
    fix.Perform()
    return Solid(fix.Solid())


def _finish(result, original, *, extra_faces=0):
    from OCP.BOPAlgo import BOPAlgo_CheckerSI
    from OCP.TopTools import TopTools_ListOfShape

    from build123d_mcp.tools.validate import _gate_report

    if len(result.faces()) != len(original.faces()) + extra_faces:
        raise ValueError("topology change: a face collapsed, disappeared or split unexpectedly")
    if any(f.area <= 1e-10 for f in result.faces()):
        raise ValueError("topology change: a face collapsed to zero area")
    report = _gate_report(result, exact=True, early_exit=True)
    if not report["passes_gate"] or report["mesh_check"] not in {"exact", "exact-subprocess"}:
        raise ValueError(
            "validity gate refused result: " + "; ".join(report["reasons"] + report["warnings"])
        )
    if len(result.solids()) != 1 or len(result.shells()) != len(original.shells()):
        raise ValueError("topology change: expected one closed Solid with the original shells")
    # BRepCheck does not detect all geometrically crossing faces/void shells.
    args = TopTools_ListOfShape()
    args.Append(result.wrapped)
    checker = BOPAlgo_CheckerSI()
    checker.SetArguments(args)
    checker.SetNonDestructive(True)
    checker.SetLevelOfCheck(5)
    checker.Perform()
    if checker.HasErrors() or not checker.DS().Interferences().IsEmpty():
        raise ValueError("self-intersection: result has interfering faces, edges or vertices")
    return result


def _invariant_support(face, vector, tol):
    """Can this unchanged support contain a rigidly translated boundary?"""
    from build123d import GeomType, Vector
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    surface = BRepAdaptor_Surface(face.wrapped)
    if face.geom_type == GeomType.PLANE:
        return abs(vector.dot(face.normal_at())) <= tol
    if face.geom_type == GeomType.CYLINDER:
        axis = Vector(surface.Cylinder().Axis().Direction().Coord())
        return vector.cross(axis).length <= tol
    if face.geom_type not in {GeomType.CONE, GeomType.SPHERE, GeomType.TORUS}:
        raise ValueError(f"non-analytic neighbour {face.geom_type.name} cannot be extended")
    return vector.length <= tol


def translate_faces(shape, faces, vector):
    """Rigidly translate explicitly selected faces; return a new, gated Solid.

    Adjacent planes/cylinders are trimmed/extended on their existing supports.
    Select tangent blends together with the moving cap/wall to preserve them.
    Cone/sphere/torus neighbours whose supports cannot contain the moved boundary,
    non-analytic neighbours, non-line connecting edges, topology changes and
    self-intersections are refused. The input is untouched, including on failure.
    No registration or display occurs. Check diff_step and show() explicitly.

    Example (a Z-axis boss with a top torus fillet):
        cap = select_faces(part, kind="plane", centroid=(0, 0, 23))
        blend = select_faces(part, kind="torus", radius=3)
        edited = translate_faces(part, cap + blend, (0, 0, -5))
        show(edited, "edited")
    Then call diff_step(old="part", new="edited") and inspect protected features;
    call show(edited, "part") only after accepting the edit.
    """
    from build123d import Compound, Edge, Face, GeomType, Vector
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_Sewing,
        BRepBuilderAPI_Transform,
    )
    from OCP.BRepTools import BRepTools_ReShape
    from OCP.gp import gp_Trsf
    from OCP.ShapeFix import ShapeFix_Face
    from OCP.TopAbs import TopAbs_FORWARD
    from OCP.TopoDS import TopoDS

    try:
        vector = _vector(vector)
        source, selected = _copy_selection(shape, faces)
        tol = 1e-7
        transform = gp_Trsf()
        transform.SetTranslation(vector.wrapped)
        moved = BRepBuilderAPI_Transform(Compound(selected).wrapped, transform, True)
        replacements = BRepTools_ReShape()
        moved_edges = {e for f in selected for e in f.edges()}
        moved_vertices = {v: Vector(v) + vector for f in selected for v in f.vertices()}
        for edge in moved_edges:
            replacements.Replace(edge.wrapped, moved.ModifiedShape(edge.wrapped))
        for face in source.faces():
            if face in selected or not any(e in moved_edges for e in face.edges()):
                continue
            if not _invariant_support(face, vector, tol):
                raise ValueError(
                    f"neighbour {face.geom_type.name} cannot be extended on its existing support; "
                    "select a tangent blend with the moving faces or rebuild parametrically"
                )
        for edge in source.edges():
            if edge in moved_edges or not any(v in moved_vertices for v in edge.vertices()):
                continue
            if edge.geom_type != GeomType.LINE:
                raise ValueError(f"connecting edge {edge.geom_type.name} cannot be extended")
            # Normalize orientation before replacing curve geometry. A boolean may
            # enumerate the same edge reversed in its first incident face.
            edge = Edge(TopoDS.Edge_s(edge.wrapped.Oriented(TopAbs_FORWARD)))
            vertices = sorted(
                edge.vertices(), key=lambda v: (Vector(v) - edge.position_at(0)).length
            )
            p, q = [moved_vertices.get(v, Vector(v)) for v in vertices]
            old_direction = edge.position_at(1) - edge.position_at(0)
            if (q - p).length <= tol or (q - p).dot(old_direction) <= 0:
                raise ValueError(
                    "topology change: move collapses or inverts a connecting edge/face"
                )
            if (p - edge.position_at(0)).cross(old_direction.normalized()).length > tol or (
                q - edge.position_at(1)
            ).cross(old_direction.normalized()).length > tol:
                raise ValueError("connecting edge cannot be extended along its original line")
            replacement = Edge.make_line(p, q)
            replacements.Replace(edge.wrapped.Oriented(TopAbs_FORWARD), replacement.wrapped)
        sew = BRepBuilderAPI_Sewing(tol)
        for face in source.faces():
            if face in selected:
                sew.Add(moved.ModifiedShape(face.wrapped))
            elif not any(replacements.IsRecorded(e.wrapped) for e in face.edges()):
                sew.Add(face.wrapped)
            else:
                if face.geom_type not in {
                    GeomType.PLANE,
                    GeomType.CYLINDER,
                    GeomType.CONE,
                    GeomType.SPHERE,
                    GeomType.TORUS,
                }:
                    raise ValueError(
                        f"non-analytic neighbour {face.geom_type.name} cannot be extended"
                    )
                forward = Face(TopoDS.Face_s(face.wrapped.Oriented(TopAbs_FORWARD)))
                outer = TopoDS.Wire_s(replacements.Apply(forward.outer_wire().wrapped))
                maker = BRepBuilderAPI_MakeFace(BRep_Tool.Surface_s(face.wrapped), outer, True)
                for wire in forward.inner_wires():
                    maker.Add(TopoDS.Wire_s(replacements.Apply(wire.wrapped)))
                rebuilt = maker.Face()
                rebuilt.Orientation(face.wrapped.Orientation())
                fix = ShapeFix_Face(rebuilt)
                fix.SetPrecision(tol)
                fix.SetMaxTolerance(tol)
                fix.Perform()
                sew.Add(fix.Face())
        sew.Perform()
        return _finish(_solid_from_shells(sew.SewedShape(), len(source.shells())), source)
    except Exception as exc:
        raise ValueError(f"translate_faces: {exc}") from None


def _same_support(a, b, tol=1e-6):
    """Compare analytic support geometry, independently of trim and UV frame."""
    from build123d import Vector
    from OCP.GeomAbs import (
        GeomAbs_Cone,
        GeomAbs_Cylinder,
        GeomAbs_Plane,
        GeomAbs_Sphere,
        GeomAbs_Torus,
    )

    kind = a.GetType()
    if kind != b.GetType():
        return False
    names = {
        GeomAbs_Plane: "Plane",
        GeomAbs_Cylinder: "Cylinder",
        GeomAbs_Cone: "Cone",
        GeomAbs_Sphere: "Sphere",
        GeomAbs_Torus: "Torus",
    }
    if kind not in names:
        raise ValueError("non-analytic offset support cannot be extended")
    x, y = getattr(a, names[kind])(), getattr(b, names[kind])()
    p, q = Vector(x.Location().Coord()), Vector(y.Location().Coord())
    u, v = (
        Vector(x.Position().Axis().Direction().Coord()),
        Vector(y.Position().Axis().Direction().Coord()),
    )
    parallel = u.cross(v).length <= 1e-7
    if kind == GeomAbs_Plane:
        return parallel and abs((p - q).dot(u)) <= tol
    if kind == GeomAbs_Cylinder:
        return parallel and (p - q).cross(u).length <= tol and abs(x.Radius() - y.Radius()) <= tol
    if kind == GeomAbs_Cone:
        return (
            parallel
            and (Vector(x.Apex().Coord()) - Vector(y.Apex().Coord())).length <= tol
            and abs(x.SemiAngle() - y.SemiAngle()) <= 1e-7
        )
    if kind == GeomAbs_Sphere:
        return (p - q).length <= tol and abs(x.Radius() - y.Radius()) <= tol
    return (
        parallel
        and (p - q).length <= tol
        and abs(x.MajorRadius() - y.MajorRadius()) <= tol
        and abs(x.MinorRadius() - y.MinorRadius()) <= tol
    )


def offset_faces(shape, faces, distance):
    """Offset selected analytic faces along outward normals; return a new Solid.

    Positive distance adds material, including shrinking a cavity. Uses OCCT
    per-face intersection offsets, with zero offset on every unselected face.
    Refuses tangent propagation, changed supports, topology changes and invalid
    results. Input is untouched; nothing is registered, printed or displayed.
    Check diff_step and show() explicitly after success.

    Example:
        walls = select_faces(part, kind="cylinder", radius=8)
        edited = offset_faces(part, walls, 2)
        show(edited, "edited")
    Then call diff_step(old="part", new="edited") to check the radius and
    protected features before show(edited, "part").
    """
    from build123d import GeomType
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepOffset import BRepOffset, BRepOffset_Good, BRepOffset_Skin
    from OCP.GeomAbs import GeomAbs_Intersection
    from OCP.GeomAdaptor import GeomAdaptor_Surface
    from OCP.TopAbs import TopAbs_REVERSED

    try:
        if not isinstance(distance, (int, float)) or not math.isfinite(distance):
            raise ValueError("distance must be finite")
        faces = list(faces)
        source, selected = _copy_selection(shape, faces)
        if all(f.geom_type == GeomType.PLANE for f in selected):
            vectors = [f.normal_at() * distance for f in selected]
            if all((v - vectors[0]).length < 1e-9 for v in vectors):
                return translate_faces(shape, faces, vectors[0])
        expected = []
        offset = BRepOffset_MakeOffset()
        offset.Initialize(
            source.wrapped, 0, 1e-7, BRepOffset_Skin, True, False, GeomAbs_Intersection
        )
        for face in source.faces():
            d = distance if face in selected else 0
            offset.SetOffsetOnFace(face.wrapped, d)
            surface = BRep_Tool.Surface_s(face.wrapped)
            signed = -d if face.wrapped.Orientation() == TopAbs_REVERSED else d
            if d:
                adaptor = BRepAdaptor_Surface(face.wrapped)
                if face.geom_type in {GeomType.CYLINDER, GeomType.SPHERE}:
                    analytic = (
                        adaptor.Cylinder()
                        if face.geom_type == GeomType.CYLINDER
                        else adaptor.Sphere()
                    )
                    if analytic.Radius() + signed <= 1e-7:
                        raise ValueError("topology change: offset collapses or inverts the radius")
                elif face.geom_type == GeomType.TORUS:
                    if adaptor.Torus().MinorRadius() + signed <= 1e-7:
                        raise ValueError(
                            "topology change: offset collapses or inverts the torus minor radius"
                        )
                surface = BRepOffset.Surface_s(surface, signed, BRepOffset_Good)
            expected.append(GeomAdaptor_Surface(surface))
        offset.MakeOffsetShape()
        if not offset.IsDone():
            raise ValueError(f"OCCT intersection offset failed: {offset.Error().name}")
        result = _solid_from_shells(offset.Shape(), len(source.shells()))
        remaining = list(result.faces())
        for support in expected:
            matches = [
                f for f in remaining if _same_support(support, BRepAdaptor_Surface(f.wrapped))
            ]
            if not matches:
                raise ValueError(
                    "tangent propagation or topology change: OCCT changed an unselected support or failed the selected offset"
                )
            remaining.remove(matches[0])
        return _finish(result, source)
    except Exception as exc:
        raise ValueError(f"offset_faces: {exc}") from None


def split_face_chord(shape, face, p0, p1):
    """Split one face by a straight boundary-to-boundary interior chord.

    Thin BRepFeat_SplitShape wrapper for an explicitly diagnosed mesh cusp.
    Endpoints must be on the face (1e-7 mm tolerance), on its boundary, and
    the whole open chord must stay inside it. Analytic generators are supported;
    a straight chord across a curved face generally leaves the surface and is
    refused. Volume drift is limited to 1e-9 of the input volume (OCC round-off).
    Returns a new gated Solid; does not register, show or bank it. Verify with
    diff_step and the written STEP export gate before accepting a repair.

    Example, for a centered 20 x 30 x 10 block:
        top = select_faces(part, kind="plane", direction=(0, 0, 1))[0]
        repaired = split_face_chord(part, top, (-10, 0, 5), (10, 0, 5))
        show(repaired, "repaired")
    """
    from build123d import Edge, Solid, Vertex
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepFeat import BRepFeat_SplitShape

    try:
        source, (face,) = _copy_selection(shape, [face])
        points = [_vector(p0), _vector(p1)]
        tol = 1e-7
        snapped = []
        for point in points:
            probe = BRepExtrema_DistShapeShape(Vertex(*point).wrapped, face.wrapped)
            if not probe.IsDone() or probe.Value() > tol:
                raise ValueError("endpoint is not on the selected face (tolerance 1e-7 mm)")
            snapped.append(_vector(probe.PointOnShape2(1).Coord()))
            if min(Vertex(*point).distance_to(e) for e in face.edges()) > tol:
                raise ValueError("chord endpoints must lie on the face boundary")
        if (snapped[1] - snapped[0]).length <= tol:
            raise ValueError("chord endpoints must be distinct")
        chord = Edge.make_line(*snapped)
        common = BRepAlgoAPI_Common(chord.wrapped, face.wrapped)
        common.SetNonDestructive(True)
        common.Build()
        from build123d import Compound

        inside_length = (
            sum(e.length for e in Compound(common.Shape()).edges()) if common.IsDone() else 0
        )
        if abs(inside_length - chord.length) > tol:
            raise ValueError(
                "chord leaves the selected face (crosses a hole or leaves its surface)"
            )
        splitter = BRepFeat_SplitShape(source.wrapped)
        splitter.Add(chord.wrapped, face.wrapped)
        splitter.Build()
        if not splitter.IsDone():
            raise ValueError("OCCT could not split the face along the chord")
        solids = Compound(splitter.Shape()).solids()
        if len(solids) != 1:
            raise ValueError("split did not produce exactly one Solid")
        result = Solid(solids[0].wrapped)
        if abs(result.volume - source.volume) > 1e-9 * abs(source.volume):
            raise ValueError("split changed volume beyond OCC round-off (1e-9 relative)")
        return _finish(result, source, extra_faces=1)
    except Exception as exc:
        raise ValueError(f"split_face_chord: {exc}") from None
