import json
import re

_HINTS: list[tuple[list[str], str]] = [
    (
        [r"AttributeError.*'ShapeList'.*has no attribute", r"'ShapeList'.*has no attribute"],
        "ShapeList is not a Part — `Box() + Cylinder()` concatenates shapes into a list "
        "rather than fusing them. Fix: use `Part() + Box(...) + Cylinder(...)` to get a "
        "fused solid, or call `.fuse()` explicitly, or iterate over `.solids()` for "
        "per-solid access.",
    ),
    (
        [r"NoneType.*has no attribute", r"AttributeError.*None"],
        "Shape is None. If you used BuildPart context manager, access the result with "
        "`.part` (e.g. `result = bp.part`). In algebra mode, assign directly: "
        "`result = Box(10,10,10) - Cylinder(3,12)`.",
    ),
    (
        [r"None context requested", r"No context.*requested"],
        "build123d algebra mode requires no context manager — create shapes directly "
        "and assign to `result` or call `show()`. Remove `with BuildPart()` wrappers "
        "if you're using operator-based construction.",
    ),
    (
        [r"cq\.", r"Workplane", r"CadQuery"],
        "CadQuery syntax detected. build123d uses a different API: "
        "`Box(w,h,d)` not `cq.Workplane().box(w,h,d)`. "
        "Replace `.translate()` with `.move(Location((x,y,z)))`, "
        "`.rotate()` with `.rotate(Axis.Z, angle)`, "
        "and `.union()`/`.cut()` with `+`/`-` operators.",
    ),
    (
        [r"TypeError.*Location", r"Location.*argument"],
        "Location syntax: pass a tuple — `Location((x, y, z))` not `Location(x, y, z)`. "
        "For combined translation + rotation: `Location((x,y,z), (rx,ry,rz))`.",
    ),
    (
        [r"[Ff]illet.*edge", r"[Ee]dge.*fillet", r"ValueError.*edges.*fillet"],
        "Fillet edge selection: edges must be non-tangent and the radius must be smaller "
        "than the adjacent wall thickness. Select edges with "
        "`shape.edges().filter_by(Axis.Z)` or index them with `shape.edges()[0]`. "
        "Avoid `shape.edges()` (all edges) on complex shapes — pick specific ones.",
    ),
    (
        [
            r"NameError.*\b(Box|Cylinder|Sphere|Cone|Torus|Extrude|BuildPart|"
            r"BuildSketch|Align|Axis|Location|Plane|Vector|Color|Compound|Shell|"
            r"Fillet|Chamfer|extrude|loft|sweep)\b"
        ],
        "build123d name not in scope. Add `from build123d import *` at the top of "
        "the execute() call. If it was imported in a previous call, re-run that import "
        "or include it in this snippet.",
    ),
    (
        [r"ImportError", r"SecurityError", r"not allowed.*import", r"import.*not allowed"],
        "Import blocked. Allowed modules include: build123d, bd_warehouse, math, numpy, "
        "json, re, collections, itertools, functools, copy, typing, dataclasses, enum, "
        "and most OCP geometry sub-modules (OCP.gp, OCP.BRepGProp, OCP.TopExp, etc.). "
        "Blocked: os, sys, pathlib, subprocess, socket (file/network/shell access). "
        "Pure-Python packages on sys.path whose imports stay within the allowed list "
        "above are permitted automatically — no config needed. "
        "For project geometry (e.g. a build_shaft() function), export to STEP and use "
        "import_cad_file(path, name) to load it without any import restrictions. "
        "To force-allow a package that imports os or similar, use --allow-imports or "
        "BUILD123D_ALLOW_IMPORTS env var.",
    ),
    (
        [r"Constraint failed", r"AssertionError"],
        "Constraint failed — a dimension is physically impossible. Common causes: "
        "fillet radius larger than the adjacent face, hole diameter larger than "
        "the wall, or zero/negative dimensions. Check all numeric parameters.",
    ),
    (
        [r"empty.*[Ss]hape", r"[Ss]hape.*empty", r"degenerate", r"no.*solid"],
        "Degenerate or empty shape after boolean operation. The cutter probably doesn't "
        "overlap the base, or the result has zero volume. Verify positions with "
        "measure(bounding_box) on both shapes before the boolean.",
    ),
    (
        [r"ExecutionTimeout"],
        "Execution timed out. Likely causes: very high-resolution mesh "
        "(lower angular_deflection), deeply nested boolean operations, or an "
        "infinite loop. Simplify the geometry or break it into smaller steps.",
    ),
    (
        [r"\.part\b"],
        "If you see an error referencing `.part`: in BuildPart context manager usage "
        "you must explicitly read `context.part` to get the Shape. "
        "In algebra mode (recommended) you don't need `.part` at all.",
    ),
]


def repair_hints(error_text: str) -> str:
    matches = []
    for patterns, hint in _HINTS:
        if any(re.search(p, error_text) for p in patterns):
            matches.append(hint)

    if not matches:
        matches.append(
            "No specific hint matched. Call last_error() for the exact line and "
            "exception, then check: (1) shapes are non-None before boolean ops, "
            "(2) `from build123d import *` is in scope, "
            "(3) Location uses a tuple argument."
        )

    return json.dumps({"hints": matches}, indent=2)
