"""Countersink recognition (in-house, Apache).

build123d builds countersinks (``CounterSinkHole``) but the external hole
recognizer reports them as plain openings, not as countersinks. A countersink is
an internal CONE face that flares from a drilled bore (minor circle) out to a
larger opening (major circle) — coaxial with a CYLINDER of the drill radius. We
key on exactly that: a cone with two distinct-radius circular edges whose smaller
radius matches a coaxial drilled cylinder. This excludes drill-point cones (a
single circle + apex, not flared) and external edge chamfers (no coaxial bore).

``recognise_countersinks(part)`` is pure (build123d/OCP only) so it can move to a
shared recognition package unchanged; ``find_countersinks(session, ...)`` is the
thin MCP tool wrapper.

Heuristic limits (``recognised`` tier): a small lead-in / deburr chamfer at a hole
mouth is geometrically a shallow countersink and also registers (its small
``major_diameter``/``depth`` make that visible); a near-flat cone above
``_MAX_INCLUDED_ANGLE`` (a draft/relief) is excluded; and a countersink cone
clipped by another feature (its edges no longer full circles) is missed.
"""

import json
import math

from build123d import GeomType

_TOL = 0.05  # mm — dimension match tolerance (matches other feature matchers)
_COAXIAL_TOL = 0.1  # mm — how far the opening may sit off the drill's axis line
# Real countersinks are ≤120° included (60/82/90/100/120 standards); a near-flat
# cone is a draft/relief/washer face, not a countersink. 160° keeps every real
# countersink with margin while excluding drafts (~176–178° included).
_MAX_INCLUDED_ANGLE = 160.0


def _parallel(a, b) -> bool:
    return abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) > 1 - 1e-3


def _dist_to_line(pt, line_pt, line_dir) -> float:
    v = (pt[0] - line_pt[0], pt[1] - line_pt[1], pt[2] - line_pt[2])
    t = v[0] * line_dir[0] + v[1] * line_dir[1] + v[2] * line_dir[2]
    perp = (v[0] - t * line_dir[0], v[1] - t * line_dir[1], v[2] - t * line_dir[2])
    return math.sqrt(perp[0] ** 2 + perp[1] ** 2 + perp[2] ** 2)


def recognise_countersinks(part) -> list:
    """Return countersink records: {location (opening), axis, major_diameter,
    drill_diameter, included_angle, depth}. Pure — no session coupling."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    cyls = []
    for cy in part.faces().filter_by(GeomType.CYLINDER):
        ax = BRepAdaptor_Surface(cy.wrapped).Cylinder().Axis()
        p, d = ax.Location(), ax.Direction()
        cyls.append((cy.radius, (p.X(), p.Y(), p.Z()), (d.X(), d.Y(), d.Z())))

    out = []
    for f in part.faces().filter_by(GeomType.CONE):
        circles = sorted(f.edges().filter_by(GeomType.CIRCLE), key=lambda e: e.radius)
        if len(circles) < 2:
            continue  # drill-point cone (one circle + apex) or degenerate
        minor_e, major_e = circles[0], circles[-1]
        minor_r, major_r = minor_e.radius, major_e.radius
        if major_r - minor_r < _TOL:
            continue  # not flared — not a countersink
        cone = BRepAdaptor_Surface(f.wrapped).Cone()
        included_angle = round(2 * abs(math.degrees(cone.SemiAngle())), 2)
        if included_angle > _MAX_INCLUDED_ANGLE:
            continue  # a near-flat cone is a draft/relief/washer face, not a countersink
        opening = major_e.arc_center
        opening_pt = (opening.X, opening.Y, opening.Z)
        mc = minor_e.arc_center
        minor_pt = (mc.X, mc.Y, mc.Z)
        # Axis points INTO the part: from the wide opening toward the drilled bore.
        # (Deterministic — don't trust OCP's cone-axis sign across constructions.)
        av = (minor_pt[0] - opening_pt[0], minor_pt[1] - opening_pt[1], minor_pt[2] - opening_pt[2])
        alen = math.sqrt(av[0] ** 2 + av[1] ** 2 + av[2] ** 2) or 1.0
        axis = (av[0] / alen, av[1] / alen, av[2] / alen)
        # A countersink sits on a drilled bore: a coaxial cylinder of the minor radius.
        if not any(
            abs(r - minor_r) <= _TOL
            and _parallel(axis, ld)
            and _dist_to_line(opening_pt, lp, ld) <= _COAXIAL_TOL
            for r, lp, ld in cyls
        ):
            continue
        out.append(
            {
                "location": [round(v, 4) for v in opening_pt],
                "axis": [round(v, 4) for v in axis],
                "major_diameter": round(2 * major_r, 4),
                "drill_diameter": round(2 * minor_r, 4),
                "included_angle": included_angle,
                "depth": round(alen, 4),
            }
        )
    return out


def find_countersinks(session, object_name: str = "") -> str:
    """Recognise countersinks on a named session object (MCP tool wrapper)."""
    from build123d_mcp.tools.measure import _resolve_shape

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    cs = recognise_countersinks(shape)
    return json.dumps({"count": len(cs), "countersinks": cs})
