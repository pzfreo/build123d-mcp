"""Fold a flat sheet-metal pattern into its formed solid, for execute().

Drawings of sheet-metal parts give the flat blank, the bend lines, UP/DOWN,
angle, inside radius, thickness and neutral (K) factor. Folding by hand is where
models go wrong: a flange built one thickness off barely overlaps the real part.
``fold_sheet`` does the fold exactly:

* each bend line is the centre line of its bend zone in the flat pattern; the
  zone is ``angle * (radius + k * thickness)`` wide (the bend allowance);
* the flat pattern lies in the XY plane with the sheet from z = 0 to z = thickness;
  the region holding ``fixed`` stays put;
* "up" bends towards +Z with the inside radius on the top face, "down" towards -Z
  with the inside radius on the bottom face;
* every flange beyond a bend is carried along, so bends chain in any order.
"""

import math
from typing import Any

_EPS = 1e-6


def _xy(p, what):
    try:
        x, y = (float(v) for v in p)
    except Exception as exc:
        raise ValueError(f"fold_sheet: {what} must be an (x, y) pair") from exc
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"fold_sheet: {what} must be finite")
    return x, y


def _positive(value, what, allow_zero=False):
    try:
        v = float(value)
    except Exception as exc:
        raise ValueError(f"fold_sheet: {what} must be a number") from exc
    if not math.isfinite(v) or v < 0 or (v == 0 and not allow_zero):
        raise ValueError(f"fold_sheet: {what} must be {'>= 0' if allow_zero else '> 0'}")
    return v


def _parse_bend(i, bend, thickness, default_k):
    if not isinstance(bend, dict):
        raise ValueError(f"fold_sheet: bend {i} must be a dict")
    allowed = {"line", "angle", "direction", "radius", "k", "position"}
    unknown = set(bend) - allowed
    if unknown:
        raise ValueError(f"fold_sheet: bend {i} has unknown keys {sorted(unknown)}")
    if "line" not in bend:
        raise ValueError(f"fold_sheet: bend {i} needs line=((x1, y1), (x2, y2))")
    try:
        a, b = bend["line"]
    except Exception as exc:
        raise ValueError(f"fold_sheet: bend {i} line must be two (x, y) points") from exc
    p1, p2 = _xy(a, f"bend {i} line start"), _xy(b, f"bend {i} line end")
    length = math.dist(p1, p2)
    if length < _EPS:
        raise ValueError(f"fold_sheet: bend {i} line has zero length")
    angle = _positive(bend.get("angle", 90.0), f"bend {i} angle")
    if angle >= 180:
        raise ValueError(f"fold_sheet: bend {i} angle must be < 180 degrees")
    direction = str(bend.get("direction", "up")).lower()
    if direction not in ("up", "down"):
        raise ValueError(f"fold_sheet: bend {i} direction must be 'up' or 'down'")
    radius = _positive(bend.get("radius", thickness), f"bend {i} radius", allow_zero=True)
    k = float(bend.get("k", default_k))
    if not (0.0 <= k <= 1.0):
        raise ValueError(f"fold_sheet: bend {i} k must be between 0 and 1")
    position = str(bend.get("position", "center")).lower()
    if position not in ("center", "near", "far"):
        raise ValueError(f"fold_sheet: bend {i} position must be 'center', 'near' or 'far'")
    theta = math.radians(angle)
    d = ((p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length)
    return {
        "p1": p1,
        "p2": p2,
        "d": d,
        "n": (-d[1], d[0]),
        "length": length,
        "theta": theta,
        "up": direction == "up",
        "radius": radius,
        "allowance": theta * (radius + k * thickness),
        "position": position,
    }


def _centre_lines(parsed, fixed):
    """Move 'near'/'far' lines to the bend-zone centre, relative to the fixed side."""
    for i, b in enumerate(parsed):
        if b["position"] == "center":
            continue
        if fixed is None:
            raise ValueError(f"fold_sheet: bend {i} position={b['position']!r} needs fixed=(x, y)")
        side = (fixed[0] - b["p1"][0]) * b["n"][0] + (fixed[1] - b["p1"][1]) * b["n"][1]
        if abs(side) < _EPS:
            raise ValueError(f"fold_sheet: fixed point lies on bend {i}'s line")
        towards_fixed = 1.0 if side > 0 else -1.0
        # 'far': the line is the zone edge away from the fixed side, so the centre
        # is half a bend allowance towards the fixed side; 'near' is the opposite.
        shift = towards_fixed * (1 if b["position"] == "far" else -1) * b["allowance"] / 2
        dx, dy = b["n"][0] * shift, b["n"][1] * shift
        b["p1"] = (b["p1"][0] + dx, b["p1"][1] + dy)
        b["p2"] = (b["p2"][0] + dx, b["p2"][1] + dy)


def _strip(b, half_width, margin):
    """Rectangle of the bend zone (or one of its edges when half_width is 0)."""
    from build123d import Face, Polyline, Wire

    (x1, y1), (dx, dy), (nx, ny) = b["p1"], b["d"], b["n"]
    s0, s1 = -margin, b["length"] + margin

    def at(s, w):
        return (x1 + dx * s + nx * w, y1 + dy * s + ny * w, 0.0)

    pts = [at(s0, -half_width), at(s1, -half_width), at(s1, half_width), at(s0, half_width)]
    return Face(Wire(Polyline(*pts, close=True).edges()))


def fold_sheet(
    outline: Any,
    thickness: float,
    bends: list,
    fixed: Any = None,
    k: float = 0.33,
):
    """Fold a flat pattern into a formed sheet-metal Solid.

    Args:
        outline: the flat blank as a planar Face (or Sketch) in the XY plane,
            holes included. Bend zones must be inside it.
        thickness: sheet thickness (mm).
        bends: one dict per bend: ``line=((x1, y1), (x2, y2))`` the bend centre
            line across the full flange width, ``direction="up"|"down"``,
            ``angle`` in degrees (default 90), ``radius`` the inside bend radius
            (default = thickness), optionally ``k`` (neutral factor) and
            ``position``: "center" (default) when the line is the bend centre
            line, "far" when it is the bend-zone edge away from the fixed side,
            "near" when it is the edge on the fixed side. "near"/"far" need
            ``fixed``.
        fixed: an (x, y) point inside the region that stays flat in place
            (default: the largest region).
        k: default neutral factor for bends that do not give one.

    Returns:
        One formed Solid. Check it with validate() and measure() as usual.
    """
    from build123d import Axis, Compound, Edge, Face, Location, Solid, Vector, extrude
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    t = _positive(thickness, "thickness")
    if not isinstance(bends, (list, tuple)) or not bends:
        raise ValueError("fold_sheet: bends must be a nonempty list of dicts")
    faces = outline.faces() if hasattr(outline, "faces") else []
    if not faces:
        raise ValueError("fold_sheet: outline must be a planar Face or Sketch")
    blank = faces[0] if len(faces) == 1 else Compound(faces).fuse()
    bb = blank.bounding_box()
    if abs(bb.min.Z) > 1e-4 or abs(bb.max.Z) > 1e-4:
        raise ValueError("fold_sheet: outline must lie in the XY plane at z = 0")
    parsed = [_parse_bend(i, b, t, k) for i, b in enumerate(bends)]
    _centre_lines(parsed, None if fixed is None else _xy(fixed, "fixed"))

    # Split the blank into flat regions and bend zones.
    zones = [blank & _strip(b, b["allowance"] / 2, 0.5) for b in parsed]
    rest = blank
    for b in parsed:
        rest = rest - _strip(b, b["allowance"] / 2, 0.5)
    regions = [f for f in rest.faces() if f.area > _EPS]
    if not regions:
        raise ValueError("fold_sheet: bend zones cover the whole blank")

    def touches(face, b, side):
        off = side * b["allowance"] / 2
        (x1, y1), (dx, dy), (nx, ny) = b["p1"], b["d"], b["n"]
        edge = Edge.make_line(
            (x1 - dx * 0.5 + nx * off, y1 - dy * 0.5 + ny * off, 0),
            (x1 + dx * (b["length"] + 0.5) + nx * off, y1 + dy * (b["length"] + 0.5) + ny * off, 0),
        )
        return face.distance_to(edge) < 1e-4

    if fixed is None:
        root = max(range(len(regions)), key=lambda i: regions[i].area)
    else:
        fx, fy = _xy(fixed, "fixed")
        hits = [i for i, r in enumerate(regions) if r.is_inside(Vector(fx, fy, 0))]
        if not hits:
            raise ValueError("fold_sheet: fixed point is not inside any flat region")
        root = hits[0]

    placement = {root: Location()}
    parts = []
    queue = [root]
    used = set()
    while queue:
        r = queue.pop(0)
        for bi, b in enumerate(parsed):
            if bi in used:
                continue
            side = 1 if touches(regions[r], b, 1) else (-1 if touches(regions[r], b, -1) else 0)
            if not side:
                continue
            children = [
                c
                for c in range(len(regions))
                if c not in placement and touches(regions[c], b, -side)
            ]
            used.add(bi)
            half = b["allowance"] / 2
            # The parent edge of the zone; material runs from here away from the parent.
            ax, ay = b["p1"][0] + b["n"][0] * side * half, b["p1"][1] + b["n"][1] * side * half
            ux, uy = -side * b["n"][0], -side * b["n"][1]
            centre_z = t + b["radius"] if b["up"] else -b["radius"]
            # Rotation sign: an "up" bend turns the sheet's run direction towards +Z.
            sign = 1.0
            trial = gp_Trsf()
            trial.SetRotation(
                gp_Ax1(gp_Pnt(ax, ay, centre_z), gp_Dir(b["d"][0], b["d"][1], 0)), b["theta"]
            )
            if (gp_Vec(ux, uy, 0).Transformed(trial).Z() > 0) != b["up"]:
                sign = -1.0
            axis_point = gp_Pnt(ax, ay, centre_z)
            axis_dir = gp_Dir(b["d"][0], b["d"][1], 0)
            rot = gp_Trsf()
            rot.SetRotation(gp_Ax1(axis_point, axis_dir), sign * b["theta"])
            # The child is first pulled back by the bend allowance so its start sits
            # on the axis plane, then swung round the bend.
            local = Location(rot) * Location((-ux * b["allowance"], -uy * b["allowance"], 0))
            # The bent zone: revolve the zone's cross-section at the parent edge about
            # the bend axis, once per stretch of the bend line that crosses material.
            centre_line = Edge.make_line(
                (b["p1"][0] - b["d"][0] * 0.5, b["p1"][1] - b["d"][1] * 0.5, 0),
                (b["p2"][0] + b["d"][0] * 0.5, b["p2"][1] + b["d"][1] * 0.5, 0),
            )
            zone = zones[bi]
            crossings = zone & centre_line if zone.faces() else None
            for e in crossings.edges() if crossings is not None else []:
                if e.length <= _EPS:
                    continue
                ends = []
                for v in (e.start_point(), e.end_point()):
                    s_along = (v.X - b["p1"][0]) * b["d"][0] + (v.Y - b["p1"][1]) * b["d"][1]
                    ends.append(
                        (
                            b["p1"][0] + b["d"][0] * s_along + b["n"][0] * side * half,
                            b["p1"][1] + b["d"][1] * s_along + b["n"][1] * side * half,
                        )
                    )
                (x0, y0), (x1, y1) = ends
                section = Face(_wire([(x0, y0, 0), (x1, y1, 0), (x1, y1, t), (x0, y0, t)]))
                axis = Axis((ax, ay, centre_z), (b["d"][0], b["d"][1], 0))
                bent = Solid.revolve(section, math.degrees(sign * b["theta"]), axis)
                parts.append(bent.moved(placement[r]))
            for c in children:
                placement[c] = placement[r] * local
                queue.append(c)
    unplaced = [i for i in range(len(regions)) if i not in placement]
    if unplaced:
        raise ValueError(
            f"fold_sheet: {len(unplaced)} flat region(s) are not connected to the fixed region by a bend"
        )
    for i, region in enumerate(regions):
        parts.append(extrude(region, t).moved(placement[i]))
    formed = parts[0]
    for p in parts[1:]:
        formed = formed.fuse(p)
    formed = formed.clean()
    solids = formed.solids()
    if len(solids) != 1:
        raise ValueError(f"fold_sheet: folding produced {len(solids)} solids, expected 1")
    return solids[0]


def _wire(points):
    from build123d import Polyline, Wire

    return Wire(Polyline(*points, close=True).edges())
