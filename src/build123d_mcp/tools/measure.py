import json
import math

# Standard densities in g/cm³ for the measure(material=...) presets.
_DENSITIES = {
    "steel": 7.85,
    "stainless": 8.00,
    "aluminum": 2.70,
    "6061": 2.70,
    "brass": 8.50,
    "copper": 8.96,
    "titanium": 4.43,
    "abs": 1.04,
    "pla": 1.24,
    "petg": 1.27,
    "nylon": 1.15,
}


def _resolve_density(density: float, material: str) -> float:
    """Return the effective density in g/cm³, or 0.0 when none was requested."""
    if material:
        if density:
            raise ValueError("Pass either density or material, not both.")
        key = material.strip().lower()
        if key not in _DENSITIES:
            raise ValueError(
                f"Unknown material '{material}'. Presets: {', '.join(sorted(_DENSITIES))}. "
                "Or pass density in g/cm³ directly."
            )
        return _DENSITIES[key]
    if density < 0:
        raise ValueError("density must be positive (g/cm³).")
    return density


def _resolve_shape(session, object_name: str):
    if object_name:
        if object_name not in session.objects:
            raise ValueError(
                f"Unknown object '{object_name}'. Registered: {list(session.objects.keys())}"
            )
        return session.objects[object_name]
    if session.current_shape is None:
        raise ValueError("No shape in session. Execute code to create geometry first.")
    return session.current_shape


def _center_of_mass(shape) -> dict:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, props)
    com = props.CentreOfMass()
    return {"x": round(com.X(), 4), "y": round(com.Y(), 4), "z": round(com.Z(), 4)}


_SURFACE_NAMES = None


def _get_surface_names():
    global _SURFACE_NAMES
    if _SURFACE_NAMES is None:
        from OCP.GeomAbs import (
            GeomAbs_BSplineSurface,
            GeomAbs_Cone,
            GeomAbs_Cylinder,
            GeomAbs_Plane,
            GeomAbs_Sphere,
            GeomAbs_Torus,
        )

        _SURFACE_NAMES = {
            GeomAbs_Plane: "Plane",
            GeomAbs_Cylinder: "Cylinder",
            GeomAbs_Cone: "Cone",
            GeomAbs_Sphere: "Sphere",
            GeomAbs_Torus: "Torus",
            GeomAbs_BSplineSurface: "BSpline",
        }
    return _SURFACE_NAMES


def _face_inventory(shape) -> list:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Sphere, GeomAbs_Torus
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    surface_names = _get_surface_names()
    explorer = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
    faces = []
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        adaptor = BRepAdaptor_Surface(face)
        stype = adaptor.GetType()
        type_name = surface_names.get(stype, f"Other({stype})")

        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = props.Mass()

        info = {"type": type_name, "area": round(area, 4)}

        if stype == GeomAbs_Cylinder:
            cyl = adaptor.Cylinder()
            info["diameter"] = round(cyl.Radius() * 2, 4)
            d = cyl.Axis().Direction()
            info["axis"] = (round(d.X(), 3), round(d.Y(), 3), round(d.Z(), 3))
        elif stype == GeomAbs_Cone:
            cone = adaptor.Cone()
            # abs(): a cone's half-angle is conventionally non-negative, and the raw
            # sign tracks the axis direction — which a STEP round-trip can flip, so
            # reporting the magnitude keeps the value stable in vs out of process (#360).
            info["semi_angle_deg"] = round(abs(math.degrees(cone.SemiAngle())), 2)
        elif stype == GeomAbs_Sphere:
            sph = adaptor.Sphere()
            info["radius"] = round(sph.Radius(), 4)
        elif stype == GeomAbs_Torus:
            t = adaptor.Torus()
            info["major_r"] = round(t.MajorRadius(), 4)
            info["minor_r"] = round(t.MinorRadius(), 4)

        faces.append(info)
        explorer.Next()

    return _condense_inventory(faces)


# Analytic surface types stay in the inventory verbatim — their parameters
# (diameters, axes, angles) are what lets a drawing be verified numerically.
_ANALYTIC_TYPES = frozenset({"Plane", "Cylinder", "Cone", "Sphere", "Torus"})


def _condense_inventory(faces: list) -> list:
    """Cut inventory noise for LLM consumers (#238): fold non-analytic sliver
    faces (thread fades etc.) into one summary entry and collapse identical
    entries into a single record with a count."""
    total_area = sum(f["area"] for f in faces)
    # Relative threshold so big parts shed their sliver noise, with an absolute
    # floor so genuinely small parts keep their genuinely small faces.
    sliver_threshold = max(0.01, total_area * 1e-4)

    kept: list = []
    slivers: list = []
    for f in faces:
        if f["type"] not in _ANALYTIC_TYPES and f["area"] < sliver_threshold:
            slivers.append(f)
        else:
            kept.append(f)

    # Collapse bit-identical entries (after rounding) into one with a count.
    grouped: dict = {}
    for f in kept:
        key = tuple(sorted(f.items()))
        if key in grouped:
            grouped[key]["count"] = grouped[key].get("count", 1) + 1
        else:
            grouped[key] = f
    result = list(grouped.values())
    result.sort(key=lambda f: (f["type"], -f["area"]))

    if slivers:
        result.append(
            {
                "type": "slivers_folded",
                "count": len(slivers),
                "total_area": round(sum(f["area"] for f in slivers), 4),
                "note": f"non-analytic faces < {round(sliver_threshold, 4)} mm² each",
            }
        )
    return result


def _inertia(shape) -> dict:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, props)
    mat = props.MatrixOfInertia()
    return {
        "Ixx": round(mat.Value(1, 1), 4),
        "Iyy": round(mat.Value(2, 2), 4),
        "Izz": round(mat.Value(3, 3), 4),
        "Ixy": round(mat.Value(1, 2), 4),
        "Ixz": round(mat.Value(1, 3), 4),
        "Iyz": round(mat.Value(2, 3), 4),
    }


def _cross_sections(shape, axis: str = "Z", num_slices: int = 10) -> list:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepGProp import BRepGProp
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_HSequenceOfShape

    axis = axis.upper()
    bb = shape.bounding_box()

    if axis == "X":
        lo, hi = bb.min.X, bb.max.X
        pln_dir = gp_Dir(1, 0, 0)
        make_pnt = lambda pos: gp_Pnt(pos, 0, 0)
    elif axis == "Y":
        lo, hi = bb.min.Y, bb.max.Y
        pln_dir = gp_Dir(0, 1, 0)
        make_pnt = lambda pos: gp_Pnt(0, pos, 0)
    else:
        lo, hi = bb.min.Z, bb.max.Z
        pln_dir = gp_Dir(0, 0, 1)
        make_pnt = lambda pos: gp_Pnt(0, 0, pos)

    span = hi - lo
    lo_s = lo + span * 0.01
    hi_s = hi - span * 0.01
    num_slices = max(num_slices, 2)
    step = (hi_s - lo_s) / (num_slices - 1)

    results = []
    for i in range(num_slices):
        pos = lo_s + i * step
        plane = gp_Pln(make_pnt(pos), pln_dir)

        section = BRepAlgoAPI_Section(shape.wrapped, plane, False)
        section.Build()

        edges = TopTools_HSequenceOfShape()
        exp = TopExp_Explorer(section.Shape(), TopAbs_EDGE)
        while exp.More():
            edges.Append(exp.Current())
            exp.Next()

        wires = TopTools_HSequenceOfShape()
        ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(edges, 1e-7, False, wires)

        total_area = 0.0
        for j in range(1, wires.Length() + 1):
            wire = TopoDS.Wire_s(wires.Value(j))
            try:
                face_maker = BRepBuilderAPI_MakeFace(plane, wire)
                if face_maker.IsDone():
                    face = face_maker.Face()
                    props = GProp_GProps()
                    BRepGProp.SurfaceProperties_s(face, props)
                    total_area += abs(props.Mass())
            except Exception:
                pass

        results.append({"position": round(pos, 4), "area": round(total_area, 4)})

    return results


def measure(session, object_name: str = "", density: float = 0.0, material: str = "") -> str:
    from build123d_mcp.tools._bounded import run_bounded_shape_op

    rho = _resolve_density(density, material)
    shape = _resolve_shape(session, object_name)
    return run_bounded_shape_op(
        session,
        "measure",
        {"": shape},
        {"rho": rho},
        in_process=lambda: _measure_report(shape, rho),
    )


def _measure_report(shape, rho: float) -> str:
    bb = shape.bounding_box()
    cx = round((bb.min.X + bb.max.X) / 2, 4)
    cy = round((bb.min.Y + bb.max.Y) / 2, 4)
    cz = round((bb.min.Z + bb.max.Z) / 2, 4)

    inertia = _inertia(shape)
    mass_fields = {}
    if rho:
        # volume mm³ × g/cm³ → g (1 cm³ = 1000 mm³); OCCT's volume inertia
        # (mm³·mm² = mm⁵) × g/mm³ → true mass moment of inertia in g·mm².
        mass_fields = {
            "density_g_cm3": rho,
            "mass_g": round(shape.volume * rho / 1000, 4),
        }
        inertia = {k: round(v * rho / 1000, 4) for k, v in inertia.items()}

    return json.dumps(
        {
            "volume": round(shape.volume, 4),
            "area": round(shape.area, 4),
            **mass_fields,
            "topology": {
                "faces": len(shape.faces()),
                "edges": len(shape.edges()),
                "vertices": len(shape.vertices()),
            },
            "bbox": {
                "xmin": round(bb.min.X, 4),
                "xmax": round(bb.max.X, 4),
                "ymin": round(bb.min.Y, 4),
                "ymax": round(bb.max.Y, 4),
                "zmin": round(bb.min.Z, 4),
                "zmax": round(bb.max.Z, 4),
                "xsize": round(bb.size.X, 4),
                "ysize": round(bb.size.Y, 4),
                "zsize": round(bb.size.Z, 4),
                "center": {"x": cx, "y": cy, "z": cz},
            },
            "center_of_mass": _center_of_mass(shape),
            "inertia": inertia,
            "inertia_units": "g·mm²" if rho else "mm⁵ (volume inertia; pass density/material)",
            "face_inventory": _face_inventory(shape),
        },
        indent=2,
    )


def clearance(session, object_a: str, object_b: str) -> str:
    """Spatial relationship between two named shapes.

    Returns the literal min-surface-to-min-surface distance plus a `status`
    that disambiguates the four cases the same distance value can mean:

      apart            — surfaces don't touch; clearance = gap (mm)
      containing       — one shape fully inside the other; clearance = wall thickness
                         (smallest gap from inner surface to outer hull)
      touching         — surfaces meet exactly; clearance = 0, no overlap volume
      interpenetrating — partial overlap; clearance = 0, both shapes have volume
                         outside the other (the wall-piercing case)

    Always reports intersection_volume + a_volume_outside_b + b_volume_outside_a
    so the LLM can reason about the magnitude of overlap without a second call.
    """
    from build123d_mcp.tools._bounded import run_bounded_shape_op

    for name in (object_a, object_b):
        if name not in session.objects:
            raise ValueError(f"Unknown object '{name}'. Registered: {list(session.objects.keys())}")
    a = session.objects[object_a]
    b = session.objects[object_b]
    return run_bounded_shape_op(
        session,
        "clearance",
        {"a": a, "b": b},
        {},
        in_process=lambda: _clearance_report(a, b),
    )


def _as_compound(s):
    # Normalise a shape's wrapper to a Compound. Whether a build123d shape is a bare
    # Solid or a Compound is unstable — a STEP round-trip flips it either way, and a
    # .solids() extraction / moved import yields a Solid — and clearance answers differ
    # by wrapper: distance_to() reads 0 for a shape contained in a Solid (vs the true
    # surface gap for a Compound), and on build123d 0.10 an empty boolean (one shape
    # fully inside the other) returns a ShapeList with no .volume. A modeller shape is
    # already a Compound, so this is a no-op there and leaves the common case unchanged.
    from build123d import Compound

    return s if type(s.wrapped).__name__ == "TopoDS_Compound" else Compound([s])


def _clearance_report(a, b) -> str:
    # Normalise both wrappers so the distance AND the containment booleans are computed
    # the same way regardless of how each shape was built or serialised — clearance is
    # then identical in vs out of process, and correct for imported/extracted bare solids.
    a = _as_compound(a)
    b = _as_compound(b)
    dist = a.distance_to(b)

    # Boolean ops for containment / overlap detection. Each can fail for
    # degenerate or non-solid shapes; None means "couldn't tell".
    def _safe_volume(op):
        try:
            return op().volume
        except Exception:
            return None

    a_outside_b = _safe_volume(lambda: a - b)
    b_outside_a = _safe_volume(lambda: b - a)
    intersection = _safe_volume(lambda: a & b)

    a_in_b = a_outside_b is not None and a_outside_b < 1e-6
    b_in_a = b_outside_a is not None and b_outside_a < 1e-6
    overlapping = intersection is not None and intersection > 1e-6

    if a_in_b:
        containment = "a_in_b"
    elif b_in_a:
        containment = "b_in_a"
    else:
        containment = "neither"

    if a_in_b or b_in_a:
        status = "containing"
    elif overlapping:
        status = "interpenetrating"
    elif dist < 1e-9:
        status = "touching"
    else:
        status = "apart"

    return json.dumps(
        {
            "clearance": dist,
            "status": status,
            "containment": containment,
            "intersection_volume": intersection if intersection is not None else 0.0,
            "a_volume_outside_b": a_outside_b,
            "b_volume_outside_a": b_outside_a,
        },
        indent=2,
    )
