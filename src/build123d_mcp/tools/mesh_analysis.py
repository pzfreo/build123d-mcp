"""Cross-section analysis for meshes.

`import_cad_file` on an STL yields a single `Face` - a shell with no solids
and no topology - so `find_holes`, `find_countersinks` and the rest of the
recogniser family return nothing on a downloaded mesh. That is the common case
when remixing a model from Printables or Thingiverse: you need the existing
mounting features in someone else's STL before you can design a part that
bolts to it.

Slicing works where topology does not. Intersect the tessellated triangles
with a plane, chain the segments into closed loops, and read features off the
loops: a loop enclosed by material is a passage through it.

Both tools tessellate via `Shape.tessellate`, so they work on solids too -
useful as a second opinion on `find_holes`, and as the only way to prove an
internal duct is enclosed rather than an open groove.
"""

import json

_MAX_TRIANGLES = 400_000

_AXES = {"X": 0, "Y": 1, "Z": 2}


def _axis_index(axis: str) -> int:
    try:
        return _AXES[axis.upper()]
    except KeyError:
        raise ValueError(f"axis must be X, Y or Z, got {axis!r}") from None


def _triangles(shape, tolerance: float):
    verts, tris = shape.tessellate(tolerance)
    if len(tris) > _MAX_TRIANGLES:
        raise ValueError(
            f"mesh has {len(tris)} triangles (limit {_MAX_TRIANGLES}); "
            "raise `tolerance` to tessellate more coarsely"
        )
    pts = [(v.X, v.Y, v.Z) for v in verts]
    return [(pts[a], pts[b], pts[c]) for a, b, c in tris]


def _slice(tris, axis: int, value: float):
    """Segments where the plane axis=value cuts the mesh."""
    segs = []
    for tri in tris:
        d = [p[axis] - value for p in tri]
        hits = []
        for i in range(3):
            j = (i + 1) % 3
            if (d[i] > 0.0) != (d[j] > 0.0):
                f = d[i] / (d[i] - d[j])
                hits.append(tuple(tri[i][k] + f * (tri[j][k] - tri[i][k]) for k in range(3)))
        if len(hits) == 2:
            segs.append((hits[0], hits[1]))
    return segs


def _chain(segs, weld: float):
    """Join segments end-to-end into closed loops."""

    def key(p):
        return (round(p[0] / weld), round(p[1] / weld), round(p[2] / weld))

    adj: dict = {}
    for a, b in segs:
        adj.setdefault(key(a), []).append((key(b), b))
        adj.setdefault(key(b), []).append((key(a), a))

    seen: set = set()
    loops = []
    for start in list(adj):
        if start in seen:
            continue
        loop, cur, prev = [], start, None
        while cur is not None and cur not in seen:
            seen.add(cur)
            nxt = None
            for k, p in adj.get(cur, []):
                if k != prev and k not in seen:
                    loop.append(p)
                    nxt = k
                    break
            prev, cur = cur, nxt
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _to_2d(loop, axis: int):
    u, v = [k for k in range(3) if k != axis]
    return [(p[u], p[v]) for p in loop]


def _point_in_polygon(pt, poly) -> bool:
    """Ray-cast containment test."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
            if x < xint:
                inside = not inside
    return inside


def _enclosed_flags(loops, axis: int):
    """Which loops are enclosed BY another loop.

    Loop count alone is not containment. A groove cut clean across a bar
    splits the cross-section into two disjoint outlines; counting
    `loop_count - 1` then calls an open groove an enclosed passage, which is
    exactly the distinction these tools exist to make.
    """
    polys = [_to_2d(lp, axis) for lp in loops]
    flags = []
    for i, poly in enumerate(polys):
        if not poly:
            flags.append(False)
            continue
        probe = poly[0]
        flags.append(
            any(
                _point_in_polygon(probe, other)
                for j, other in enumerate(polys)
                if j != i and len(other) >= 3
            )
        )
    return flags


def _loop_record(loop, axis: int) -> dict:
    u, v = [k for k in range(3) if k != axis]
    us = [p[u] for p in loop]
    vs = [p[v] for p in loop]
    return {
        "points": len(loop),
        "center": [round((min(us) + max(us)) / 2, 3), round((min(vs) + max(vs)) / 2, 3)],
        "size": [round(max(us) - min(us), 3), round(max(vs) - min(vs), 3)],
        "min": [round(min(us), 3), round(min(vs), 3)],
        "max": [round(max(us), 3), round(max(vs), 3)],
    }


def mesh_section(
    session,
    object_name: str = "",
    axis: str = "Z",
    position: float = 0.0,
    tolerance: float = 0.1,
    weld: float = 0.001,
) -> str:
    """Loops on one cross-section plane, largest first.

    Args:
        object_name: name from show()/import_cad_file (default: current shape)
        axis: "X", "Y" or "Z" - the plane's normal
        position: absolute world coordinate along that axis
        tolerance: tessellation tolerance (larger = coarser = faster)
        weld: point-merge distance when chaining segments into loops

    Returns:
        JSON {axis, position, loop_count, enclosed_passages, loops:[...]} where
        each loop carries points/center/size/min/max in the two axes that are
        not `axis`, plus `enclosed`.

    `enclosed_passages` counts loops that lie INSIDE another loop - a passage
    through the material at this height. Containment is tested, not inferred
    from the loop count: a groove cut clean across a bar splits the section
    into two disjoint outlines, and counting `loop_count - 1` would call that
    an enclosed passage. An open groove reads 0 here however deep it looks in
    a render.
    """
    from build123d_mcp.tools.measure import _resolve_shape

    shape = _resolve_shape(session, object_name)
    ax = _axis_index(axis)
    tris = _triangles(shape, tolerance)
    loops = _chain(_slice(tris, ax, position), weld)
    flags = _enclosed_flags(loops, ax)
    records = []
    for lp, enclosed in zip(loops, flags):
        rec = _loop_record(lp, ax)
        rec["enclosed"] = enclosed
        records.append(rec)
    records.sort(key=lambda r: r["size"][0] * r["size"][1], reverse=True)
    return json.dumps(
        {
            "axis": axis.upper(),
            "position": position,
            "loop_count": len(records),
            "enclosed_passages": sum(1 for r in records if r["enclosed"]),
            "loops": records,
        },
        indent=2,
    )


def mesh_holes(
    session,
    object_name: str = "",
    min_diameter: float = 2.0,
    max_diameter: float = 12.0,
    slices: int = 48,
    min_depth: float = 1.0,
    tolerance: float = 0.1,
    weld: float = 0.001,
) -> str:
    """Find fastener holes in a mesh by slicing it on all three axes.

    Args:
        object_name: name from show()/import_cad_file (default: current shape)
        min_diameter: keep loops at least this wide
        max_diameter: keep loops at most this wide. The defaults cover M2-M8
            clearance holes, counterbores and heat-set insert pockets.
        slices: sample planes per axis
        min_depth: drop features shallower than this. Chamfer rings and
            tessellation slivers read as very shallow holes.
        tolerance: tessellation tolerance
        weld: point-merge distance when chaining segments into loops

    Returns:
        JSON {count, holes:[{axis, diameter, location, span, through}]} where
        `axis` is the drilling direction, `location` the hole centre in world
        coordinates, and `span` how far it runs along `axis`.

    Read `span`: a hole running the full extent is a through hole, one
    appearing only near a face is a blind pocket - which is what a heat-set
    insert sits in. A hole shows up as a round loop only on slices normal to
    its own axis, so scanning a single axis finds a fraction of the part.

    This is the mesh counterpart to find_holes(), which needs real topology and
    returns nothing for an imported STL. It reports what the cross-sections
    show and does not classify counterbores, countersinks or thread forms.
    """
    from build123d_mcp.tools.measure import _resolve_shape

    shape = _resolve_shape(session, object_name)
    tris = _triangles(shape, tolerance)
    bb = shape.bounding_box()
    lo = (bb.min.X, bb.min.Y, bb.min.Z)
    hi = (bb.max.X, bb.max.Y, bb.max.Z)

    holes = []
    for ax in range(3):
        extent = hi[ax] - lo[ax]
        if extent <= 0:
            continue
        step = extent / slices
        found: dict = {}
        for value in _sample_positions(lo[ax], hi[ax], slices):
            for key in _keys_at(tris, ax, value, weld, min_diameter, max_diameter):
                found.setdefault(key, []).append(value)
        u, v = [k for k in range(3) if k != ax]
        for key, positions in sorted(found.items()):
            cu, cv, dia = key
            # One record per contiguous RUN, not per key. Two blind pockets
            # bored into opposite faces of a bar share a key - same centre in
            # the cross plane, same diameter - and merging them reports one
            # through hole where there are two pockets and solid material
            # between.
            spans = []
            for run in _runs(positions, step):
                # Sampling alone cannot give a span either: a shallow pocket
                # can fall between two sample planes. Walk out from a real hit
                # with a fine step to find where the feature actually stops.
                start, end = _refine_span(
                    tris, ax, key, run, step, lo[ax], hi[ax], weld,
                    min_diameter, max_diameter,
                )
                spans.append((start, end))

            # A bore can drop out of detection on individual slices where the
            # tessellation happens to chain badly, fragmenting one through hole
            # into several runs. A gap narrower than the bore is a dropout, not
            # solid material: merge. Pockets bored into opposite faces stay
            # separate because the land between them is far wider than they are.
            for start, end in _merge_spans(spans, dia):
                if end - start < min_depth:
                    continue  # tessellation sliver, chamfer ring, not a hole
                centre = [0.0, 0.0, 0.0]
                centre[u], centre[v] = cu, cv
                centre[ax] = round((start + end) / 2, 3)
                holes.append(
                    {
                        "axis": "XYZ"[ax],
                        "diameter": dia,
                        "location": [round(c, 3) for c in centre],
                        "span": [round(start, 3), round(end, 3)],
                        "depth": round(end - start, 3),
                        "through": (end - start) >= extent * 0.98,
                    }
                )
    return json.dumps({"count": len(holes), "holes": holes}, indent=2)


def _sample_positions(lo: float, hi: float, slices: int):
    """Uniform sample planes, plus a fine sweep just inside each face.

    A uniform grid alone misses shallow blind pockets: a 4 mm insert pocket in
    the end of a 275 mm bar is thinner than one sample step and can fall
    entirely between two planes, so the bar reads as having a pocket at one end
    and nothing at the other. Bores open on faces, so sample near the faces.
    """
    extent = hi - lo
    step = extent / slices
    values = [lo + extent * i / slices for i in range(1, slices)]
    edge = min(step, extent * 0.06)
    for i in range(1, 13):
        values.append(lo + edge * i / 12.0)
        values.append(hi - edge * i / 12.0)
    return sorted(v for v in values if lo < v < hi)


def _merge_spans(spans, diameter):
    """Join spans separated by less than the bore diameter."""
    out = []
    for start, end in sorted(spans):
        if out and start - out[-1][1] < diameter:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _runs(positions, step):
    """Split sorted sample positions into contiguous runs."""
    out, cur = [], [positions[0]]
    for p in positions[1:]:
        if p - cur[-1] <= step * 1.5:
            cur.append(p)
        else:
            out.append(cur)
            cur = [p]
    out.append(cur)
    return out


def _keys_at(tris, ax: int, value: float, weld: float, min_d: float, max_d: float):
    """Feature keys present on one slice: (center_u, center_v, diameter)."""
    keys = []
    loops = _chain(_slice(tris, ax, value), weld)
    for loop, enclosed in zip(loops, _enclosed_flags(loops, ax)):
        if not enclosed:
            continue  # an outline, or a disjoint piece of one - not a bore
        rec = _loop_record(loop, ax)
        dia = max(rec["size"])
        if min_d <= dia <= max_d:
            keys.append((round(rec["center"][0], 1), round(rec["center"][1], 1), round(dia, 1)))
    return keys


def _refine_span(tris, ax, key, run, step, lo, hi, weld, min_d, max_d):
    """True extent of one contiguous run, by fine stepping outwards."""
    fine = step / 16.0
    start = run[0]
    while start - fine >= lo and key in _keys_at(tris, ax, start - fine, weld, min_d, max_d):
        start -= fine
    end = run[-1]
    while end + fine <= hi and key in _keys_at(tris, ax, end + fine, weld, min_d, max_d):
        end += fine
    return start, end
