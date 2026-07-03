"""verify_spec — check the built solid against a declared design-intent spec.

Composes existing checkers (validity gate, measure, feature recognition, parameter
extraction) into a single conformance report. Each requirement is tagged PASS /
FAIL / UNVERIFIED and carries the *tier* of evidence behind it. The report proves
requested-vs-built for geometry-checkable requirements only and never claims the
design is "correct" — see docs/design-conformance-proposal.md (#335).

Scope: envelope, solid count/validity, volume, hole/hole-pattern/boss/countersink
features, material_at_point and wall_thickness_at probes, top-level numeric parameter
ranges, and global min_wall_mm (augura). Deferred (reported UNVERIFIED, not silently
ignored): parameter robustness (design_audit), non-geometry targets.
"""

import json
import math

from build123d_mcp.tools._paths import safe_output_path
from build123d_mcp.tools.validate import _gate_report, _resolve_shape

# Dimension match tolerance: the larger of an absolute floor and a relative band,
# matching the callout-matching tolerance used elsewhere. Counts match exactly.
_ABS_TOL = 0.1
_REL_TOL = 0.01


def _close(actual, want) -> bool:
    try:
        return abs(actual - want) <= max(_ABS_TOL, abs(want) * _REL_TOL)
    except TypeError:
        return False


def _load_spec(spec: str, spec_path: str):
    """Return (data_dict, error_json_or_None)."""
    try:
        if spec_path:
            with open(safe_output_path(spec_path)) as f:
                data = json.load(f)
        elif spec:
            data = json.loads(spec)
        else:
            return None, json.dumps(
                {"error": "Provide a spec (inline JSON) or spec_path (path to a .json spec)."}
            )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return None, json.dumps({"error": f"Could not read spec: {exc}"})
    if not isinstance(data, dict):
        return None, json.dumps({"error": "Spec must be a JSON object."})
    return data, None


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _spec_shape_error(data: dict) -> str | None:
    """Return an actionable message if a known spec field is the wrong shape, else None.

    Catches the common agent typos (envelope axis as a scalar, features as a dict,
    volume as a list) up front so verify_spec returns a clean error instead of
    crashing on an unpack/attribute access deep in a checker.
    """
    env = data.get("envelope_mm")
    if env is not None:
        if not isinstance(env, dict):
            return 'envelope_mm must be an object like {"x": [lo, hi], ...}'
        for ax in ("x", "y", "z"):
            if ax in env and not (
                isinstance(env[ax], list) and len(env[ax]) == 2 and all(_is_num(v) for v in env[ax])
            ):
                return f"envelope_mm.{ax} must be [lo, hi] numbers"
    vol = data.get("volume_mm3")
    if vol is not None:
        if not isinstance(vol, dict):
            return 'volume_mm3 must be an object like {"min": .., "max": ..}'
        for k in ("min", "max"):
            if k in vol and not _is_num(vol[k]):
                return f"volume_mm3.{k} must be a number"
    if data.get("solid") is not None and not isinstance(data["solid"], dict):
        return 'solid must be an object like {"count": 1, "valid": true}'
    feats = data.get("features")
    if feats is not None:
        if not isinstance(feats, list):
            return "features must be a list of feature objects"
        for i, f in enumerate(feats):
            if not isinstance(f, dict) or "kind" not in f:
                return f"features[{i}] must be an object with a 'kind'"
            if f.get("kind") == "material_at_point":
                pt = f.get("point")
                if not (isinstance(pt, list) and len(pt) == 3 and all(_is_num(v) for v in pt)):
                    return f"features[{i}].point must be [x, y, z] numbers"
                if "expect" in f and f["expect"] not in ("solid", "void"):
                    return f'features[{i}].expect must be "solid" or "void"'
            if f.get("kind") == "wall_thickness_at":
                for key in ("point", "direction"):
                    v = f.get(key)
                    if not (isinstance(v, list) and len(v) == 3 and all(_is_num(x) for x in v)):
                        return f"features[{i}].{key} must be [x, y, z] numbers"
                em = f.get("expect_mm")
                if not (isinstance(em, list) and len(em) == 2 and all(_is_num(x) for x in em)):
                    return f"features[{i}].expect_mm must be [lo, hi] numbers"
            for k in (
                "diameter_mm",
                "depth_mm",
                "bcd_mm",
                "pitch_mm",
                "height_mm",
                "holes",
                "count",
                "major_diameter_mm",
                "drill_diameter_mm",
                "included_angle_deg",
            ):
                if k in f and not _is_num(f[k]):
                    return f"features[{i}].{k} must be a number"
            for sub in ("counterbore", "spotface"):
                if sub not in f:
                    continue
                if not isinstance(f[sub], (dict, bool)):
                    return f"features[{i}].{sub} must be an object {{diameter_mm, depth_mm}} or true/false"
                if isinstance(f[sub], dict):
                    for k in ("diameter_mm", "depth_mm"):
                        if k in f[sub] and not _is_num(f[sub][k]):
                            return f"features[{i}].{sub}.{k} must be a number"
    params = data.get("parameters")
    if params is not None:
        if not isinstance(params, list):
            return "parameters must be a list of {name, min, max} objects"
        for i, p in enumerate(params):
            if not isinstance(p, dict) or "name" not in p:
                return f"parameters[{i}] must be an object with a 'name'"
            for k in ("min", "max"):
                if k in p and not _is_num(p[k]):
                    return f"parameters[{i}].{k} must be a number"
    tgts = data.get("targets")
    if tgts is not None and not (isinstance(tgts, list) and all(isinstance(t, dict) for t in tgts)):
        return "targets must be a list of objects"
    if data.get("min_wall_mm") is not None and not _is_num(data["min_wall_mm"]):
        return "min_wall_mm must be a number"
    return None


def _check_envelope(m: dict, spec: dict, out: list) -> None:
    env = spec.get("envelope_mm")
    if not env:
        return
    bb = m["bbox"]
    for ax, key in (("x", "xsize"), ("y", "ysize"), ("z", "zsize")):
        rng = env.get(ax)
        if not rng:
            continue
        lo, hi = rng
        size = bb[key]
        out.append(
            {
                "requirement": f"envelope {ax} ∈ [{lo}, {hi}] mm",
                "status": "PASS" if lo <= size <= hi else "FAIL",
                "tier": "measured",
                "actual": size,
            }
        )


def _check_volume(m: dict, spec: dict, out: list) -> None:
    v = spec.get("volume_mm3")
    if not v:
        return
    vol = m["volume"]
    lo, hi = v.get("min"), v.get("max")
    ok = (lo is None or vol >= lo) and (hi is None or vol <= hi)
    out.append(
        {
            "requirement": f"volume ∈ [{lo}, {hi}] mm³",
            "status": "PASS" if ok else "FAIL",
            "tier": "measured",
            "actual": vol,
        }
    )


def _check_solid(gate: dict, spec: dict, out: list) -> None:
    s = spec.get("solid")
    if not s:
        return
    if "count" in s:
        ok = gate["n_solids"] == s["count"]
        out.append(
            {
                "requirement": f"{s['count']} solid body(ies)",
                "status": "PASS" if ok else "FAIL",
                "tier": "measured",
                "actual": gate["n_solids"],
            }
        )
    if s.get("valid"):
        ok = gate["passes_gate"]
        entry = {
            "requirement": "watertight, manifold, valid solid",
            "status": "PASS" if ok else "FAIL",
            "tier": "structural",
        }
        if not ok:
            entry["hint"] = (
                "; ".join(gate.get("reasons", [])) or "see validate()/locate_gate_defects()"
            )
        out.append(entry)


def _recognise(fn, session, object_name: str, key: str):
    """Return (items_list, error_or_None) from a feature-recognition tool."""
    r = json.loads(fn(session, object_name))
    return r.get(key, []), r.get("error")


def _check_hole_pattern(f: dict, patterns: list, err, out: list) -> None:
    want_type = f.get("pattern", "bolt_circle")
    req = f"{f.get('holes', '?')}× Ø{f.get('diameter_mm', '?')} {want_type}"
    if err:
        out.append({"requirement": req, "status": "UNVERIFIED", "tier": "unverified", "note": err})
        return
    for p in patterns:
        if p.get("type") != want_type:
            continue
        if "holes" in f and len(p.get("holes", [])) != f["holes"]:
            continue
        if (
            want_type == "bolt_circle"
            and "bcd_mm" in f
            and not _close(p.get("diameter"), f["bcd_mm"])
        ):
            continue
        if (
            want_type == "linear_array"
            and "pitch_mm" in f
            and not _close(p.get("pitch"), f["pitch_mm"])
        ):
            continue
        if "diameter_mm" in f:
            hd = [h.get("diameter") for h in p.get("holes", []) if h.get("diameter") is not None]
            if not hd or not _close(hd[0], f["diameter_mm"]):
                continue
        found = {"holes": len(p.get("holes", []))}
        if want_type == "bolt_circle":
            found["bcd"] = p.get("diameter")
        elif want_type == "linear_array":
            found["pitch"] = p.get("pitch")
        out.append({"requirement": req, "status": "PASS", "tier": "recognised", "found": found})
        return
    n_type = sum(1 for p in patterns if p.get("type") == want_type)
    out.append(
        {
            "requirement": req,
            "status": "FAIL",
            "tier": "recognised",
            "hint": f"found {n_type} {want_type} pattern(s); none matched holes/BCD/pitch/Ø",
        }
    )


def _sub_matches(sub: dict | None, want) -> bool:
    """Match a counterbore/spotface sub-feature. `want` is:
    True  → require presence, False → require absence (symmetric with `through`),
    or an object with optional diameter_mm/depth_mm (depth is matched against the
    *recognizer-measured* depth, which may differ from a drawing callout)."""
    if want is False:
        return not sub  # explicitly assert NO counterbore/spotface
    if not sub:
        return False
    if isinstance(want, dict):
        if "diameter_mm" in want and not _close(sub.get("diameter"), want["diameter_mm"]):
            return False
        if "depth_mm" in want and not _close(sub.get("depth"), want["depth_mm"]):
            return False
    return True


def _hole_matches(h: dict, f: dict) -> bool:
    """Does a recognised hole record satisfy the requested hole spec? All
    frame-independent attributes the recognizer exposes (Ø, depth, through/blind,
    counterbore, spotface); absolute location is intentionally not matched."""
    if "diameter_mm" in f and not _close(h.get("diameter"), f["diameter_mm"]):
        return False
    if "depth_mm" in f and not _close(h.get("depth"), f["depth_mm"]):
        return False
    if "through" in f and (h.get("bottom") == "through") != bool(f["through"]):
        return False
    if "counterbore" in f and not _sub_matches(h.get("cbore"), f["counterbore"]):
        return False
    if "spotface" in f and not _sub_matches(h.get("spotface"), f["spotface"]):
        return False
    return True


def _check_hole(f: dict, holes: list, err, out: list) -> None:
    d = f.get("diameter_mm")
    want = f.get("count", 1)
    attrs = []
    if "depth_mm" in f:
        attrs.append(f"depth {f['depth_mm']}")
    if f.get("through") is True:
        attrs.append("through")
    elif f.get("through") is False:
        attrs.append("blind")
    if "counterbore" in f:
        attrs.append("counterbore")
    if "spotface" in f:
        attrs.append("spotface")
    base = f"{want}× Ø{d} hole" if d is not None else f"{want} hole(s)"
    req = base + (f" ({', '.join(attrs)})" if attrs else "")
    if err:
        out.append({"requirement": req, "status": "UNVERIFIED", "tier": "unverified", "note": err})
        return
    matching = [h for h in holes if _hole_matches(h, f)]
    ok = len(matching) == want if "count" in f else len(matching) >= 1
    out.append(
        {
            "requirement": req,
            "status": "PASS" if ok else "FAIL",
            "tier": "recognised",
            "found": len(matching),
        }
    )


def _check_boss(f: dict, bosses: list, err, out: list) -> None:
    d, h = f.get("diameter_mm"), f.get("height_mm")
    req = f"boss Ø{d}" + (f"×{h}h" if h is not None else "")
    if err:
        out.append({"requirement": req, "status": "UNVERIFIED", "tier": "unverified", "note": err})
        return
    for b in bosses:
        if d is not None and not _close(b.get("diameter"), d):
            continue
        if h is not None and not _close(b.get("height"), h):
            continue
        out.append({"requirement": req, "status": "PASS", "tier": "recognised", "found": b})
        return
    out.append({"requirement": req, "status": "FAIL", "tier": "recognised"})


def _check_countersink(f: dict, csinks: list, err, out: list) -> None:
    want = f.get("count", 1)
    req = f"{want}× countersink" + (
        f" Ø{f['major_diameter_mm']}" if "major_diameter_mm" in f else ""
    )
    if err:
        out.append({"requirement": req, "status": "UNVERIFIED", "tier": "unverified", "note": err})
        return

    def _matches(c: dict) -> bool:
        for spec_key, rec_key in (
            ("major_diameter_mm", "major_diameter"),
            ("drill_diameter_mm", "drill_diameter"),
            ("included_angle_deg", "included_angle"),
            ("depth_mm", "depth"),
        ):
            if spec_key in f and not _close(c.get(rec_key), f[spec_key]):
                return False
        return True

    matching = [c for c in csinks if _matches(c)]
    ok = len(matching) == want if "count" in f else len(matching) >= 1
    out.append(
        {
            "requirement": req,
            "status": "PASS" if ok else "FAIL",
            "tier": "recognised",
            "found": len(matching),
        }
    )


def _check_material_at_point(shape, resolve_err, f: dict, out: list) -> None:
    """Classify which side of the material boundary a declared point falls on.

    Sidesteps face recognition entirely (a partial cylindrical face trimmed by a
    curved surface is invisible to find_holes/find_bosses) — asks the kernel a
    single declarative question: is this point inside the solid? Ideal for
    disambiguating add-vs-remove features (boss vs pocket) the recognizers can't
    see. NOTE: unlike the other feature checks this is **frame-dependent** — the
    point is an absolute coordinate tied to the part's own frame, so it verifies
    a same-session build reliably but is not portable across a repositioned part.
    Like volume/envelope, point classification assumes a valid (watertight) solid;
    on a non-manifold shell is_inside can misclassify — check the ``solid`` gate too.
    """
    point: list = f["point"]  # validated as [x, y, z] by _spec_shape_error
    expect = f.get("expect", "solid")
    req = f"material {expect} at {point}"
    note = "frame-dependent (absolute coordinate, tied to the part's own frame)"
    if resolve_err is not None or shape is None:
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": resolve_err or "no shape",
            }
        )
        return
    # A 2D sketch/face is a Compound too (it has is_inside, but always reports void
    # since it has no volume) — gate on actual solids so it reads UNVERIFIED, not a
    # misleading FAIL.
    try:
        has_solid = len(shape.solids()) > 0
    except Exception:  # noqa: BLE001
        has_solid = False
    if not has_solid:
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": "material_at_point needs a solid; the current shape has none (e.g. a 2D sketch)",
            }
        )
        return
    from build123d import Vector

    try:
        inside = shape.is_inside(Vector(*point))
    except Exception as exc:  # noqa: BLE001 - a bad point must not crash the whole report
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": f"is_inside failed: {exc}",
            }
        )
        return
    actual = "solid" if inside else "void"
    entry = {
        "requirement": req,
        "status": "PASS" if actual == expect else "FAIL",
        "tier": "measured",
        "actual": actual,
        "note": note,
    }
    # A "void" assertion at a point outside the part is trivially satisfied — warn,
    # in the same spirit as the no-vacuous-conforms guard.
    try:
        bb = shape.bounding_box()
        outside_bbox = any(
            point[i] < getattr(bb.min, "XYZ"[i]) or point[i] > getattr(bb.max, "XYZ"[i])
            for i in range(3)
        )
    except Exception:  # noqa: BLE001
        outside_bbox = False
    if expect == "void" and outside_bbox:
        entry["hint"] = (
            "point is outside the part's bounding box — a 'void' pass here is vacuous; "
            "choose a point within the nominal envelope where the readings differ"
        )
    out.append(entry)


def _check_wall_thickness_at(shape, resolve_err, f: dict, out: list) -> None:
    """Measure the local wall thickness along a line through a point and range-check it.

    Fills the dominant thin-wall blind spot: a rib/pocket/shell wall can be well off
    the drawing callout while every hole/envelope check passes. Uses augura's
    BREP-exact ray query (measured tier). Like material_at_point this is
    **frame-dependent** — point/direction are absolute in the part's own frame.
    """
    point = f["point"]
    lo, hi = f["expect_mm"]
    req = f"wall ∈ [{lo}, {hi}] mm at {point}"
    if resolve_err is not None or shape is None:
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": resolve_err or "no shape",
            }
        )
        return
    from augura import wall_thickness_at

    try:
        thickness = wall_thickness_at(shape, point, f["direction"])
    except Exception as exc:  # noqa: BLE001 - a bad probe must not crash the report
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": f"probe failed: {exc}",
            }
        )
        return
    if thickness is None:
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": "no wall found along the direction — place the point in/on the wall and aim across it",
            }
        )
        return
    out.append(
        {
            "requirement": req,
            "status": "PASS" if lo <= thickness <= hi else "FAIL",
            "tier": "measured",
            "actual": round(thickness, 4),
            "note": "frame-dependent (absolute point/direction, tied to the part's own frame)",
        }
    )


def _check_features(session, object_name: str, features: list, out: list) -> None:
    from build123d_mcp.tools.find_features import find_bosses, find_hole_patterns, find_holes
    from build123d_mcp.tools.recognizers.countersink import find_countersinks

    holes = pats = bosses = csinks = None
    holes_err = pats_err = bosses_err = csinks_err = None
    unset = object()
    shape = shape_err = unset  # resolved lazily for material_at_point
    for f in features:
        kind = f.get("kind")
        if kind == "hole_pattern":
            if pats is None:
                pats, pats_err = _recognise(find_hole_patterns, session, object_name, "patterns")
            _check_hole_pattern(f, pats, pats_err, out)
        elif kind == "hole":
            if holes is None:
                holes, holes_err = _recognise(find_holes, session, object_name, "holes")
            _check_hole(f, holes, holes_err, out)
        elif kind == "boss":
            if bosses is None:
                bosses, bosses_err = _recognise(find_bosses, session, object_name, "bosses")
            _check_boss(f, bosses, bosses_err, out)
        elif kind == "countersink":
            if csinks is None:
                csinks, csinks_err = _recognise(
                    find_countersinks, session, object_name, "countersinks"
                )
            _check_countersink(f, csinks, csinks_err, out)
        elif kind == "material_at_point":
            if shape is unset:
                shape, shape_err = _resolve_shape(session, object_name)
            _check_material_at_point(shape, shape_err, f, out)
        elif kind == "wall_thickness_at":
            if shape is unset:
                shape, shape_err = _resolve_shape(session, object_name)
            _check_wall_thickness_at(shape, shape_err, f, out)
        else:
            out.append(
                {
                    "requirement": f"feature {kind!r}",
                    "status": "UNVERIFIED",
                    "tier": "unverified",
                    "note": f"feature kind {kind!r} is not recognised by build123d-mcp",
                }
            )


def _check_parameters(session, params_spec: list, out: list) -> None:
    from build123d_mcp._design_audit_subprocess import _extract_params
    from build123d_mcp.tools.design_audit import _assemble

    program = _assemble(session)
    found = {p["name"]: p["value"] for p in (_extract_params(program)[0] if program else [])}
    for ps in params_spec:
        name = ps.get("name")
        if name not in found:
            out.append(
                {
                    "requirement": f"parameter {name!r} present",
                    "status": "FAIL",
                    "tier": "measured",
                    "hint": "not a top-level numeric assignment in the session program",
                }
            )
            continue
        val = found[name]
        lo, hi = ps.get("min"), ps.get("max")
        ok = (lo is None or val >= lo) and (hi is None or val <= hi)
        out.append(
            {
                "requirement": f"parameter {name} ∈ [{lo}, {hi}]",
                "status": "PASS" if ok else "FAIL",
                "tier": "measured",
                "actual": val,
            }
        )


def _check_min_wall(shape, spec: dict, out: list) -> None:
    """Global minimum wall thickness ≥ a threshold, via augura's sampled ray query."""
    if "min_wall_mm" not in spec:
        return
    want = spec["min_wall_mm"]
    req = f"min wall ≥ {want} mm"
    from augura import min_wall_thickness

    try:
        thinnest = min_wall_thickness(shape)
    except Exception as exc:  # noqa: BLE001
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": f"probe failed: {exc}",
            }
        )
        return
    if thinnest is None:
        out.append(
            {
                "requirement": req,
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": "no opposed surfaces to sample (e.g. a 2D sketch)",
            }
        )
        return
    out.append(
        {
            "requirement": req,
            "status": "PASS" if thinnest >= want else "FAIL",
            "tier": "measured",
            "actual": round(thinnest, 4),
            "note": "sampled minimum over face probes; approximate (curved/large faces may be under-sampled)",
        }
    )


def _check_deferred(spec: dict, out: list) -> None:
    for t in spec.get("targets", []) or []:
        name = t.get("name")
        note = (
            "declared unverifiable — no tool in build123d-mcp can prove this (e.g. needs a solver)"
            if t.get("verifiable") is False
            else "no checker for this target in build123d-mcp"
        )
        out.append(
            {
                "requirement": f"target {name}",
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": note,
            }
        )


def verify_spec(session, spec: str = "", spec_path: str = "", object_name: str = "") -> str:
    """Verify the current (or named) solid against a declared design-intent spec.

    spec: inline JSON spec. spec_path: path to a .json spec (mutually usable with spec).
    object_name: named object from show() (default: current shape).
    """
    data, err = _load_spec(spec, spec_path)
    if err is not None:
        return err

    shape_err_msg = _spec_shape_error(data)
    if shape_err_msg is not None:
        return json.dumps({"error": f"Malformed spec: {shape_err_msg}"})

    shape, shape_err = _resolve_shape(session, object_name)
    if shape_err is not None:
        return shape_err

    out: list = []
    try:
        if "envelope_mm" in data or "volume_mm3" in data:
            from build123d_mcp.tools.measure import measure as _measure

            m = json.loads(_measure(session, object_name))
            _check_envelope(m, data, out)
            _check_volume(m, data, out)
        if "solid" in data:
            _check_solid(_gate_report(shape), data, out)
        if data.get("features"):
            _check_features(session, object_name, data["features"], out)
        if data.get("parameters"):
            _check_parameters(session, data["parameters"], out)
        _check_min_wall(shape, data, out)
        _check_deferred(data, out)
    except Exception as exc:  # backstop: a spec quirk must return JSON, not crash the worker
        return json.dumps({"error": f"Could not evaluate spec against the shape: {exc}"})

    n_fail = sum(1 for e in out if e["status"] == "FAIL")
    n_pass = sum(1 for e in out if e["status"] == "PASS")
    n_unv = sum(1 for e in out if e["status"] == "UNVERIFIED")
    checked = n_pass + n_fail  # requirements actually verified (PASS/FAIL), not UNVERIFIED
    note = (
        "Proves requested-vs-built for the geometry-checkable requirements only. conforms means no "
        "FAILs AND at least one requirement was checked; UNVERIFIED requirements are NOT met — they "
        "are out of scope for this gate (declared unverifiable, deferred, or an unrecognised feature). "
        "Each line carries its evidence tier (measured/structural/recognised). This is not a "
        "certification; a human must sign off."
    )
    if checked == 0:
        note = (
            "WARNING: no geometry-checkable requirements were evaluated — every spec entry was "
            "unrecognised, deferred, or unverifiable, so conforms is false (nothing was proven). "
            + note
        )
    return json.dumps(
        {
            "conformance": out,
            "summary": {
                "pass": n_pass,
                "fail": n_fail,
                "unverified": n_unv,
                "checked": checked,
                "conforms": n_fail == 0 and checked > 0,
            },
            "note": note,
        },
        indent=2,
    )


# --------------------------------------------------------------------------- #
# suggest_spec — draft a starter design-intent spec from the current shape     #
# --------------------------------------------------------------------------- #


def _pct_band(x: float, dp: int = 2) -> list[float]:
    """A ±2% band floored/ceiled to `dp` decimals so the actual value is always
    bracketed — plain rounding can push a bound past x for sub-0.25 mm dimensions,
    breaking the suggest→verify round-trip."""
    scale = 10**dp
    return [math.floor(x * 0.98 * scale) / scale, math.ceil(x * 1.02 * scale) / scale]


def _round(x, n=3):
    return round(x, n) if isinstance(x, (int, float)) else x


def _suggest_features(session, object_name: str) -> list:
    """Emit hole/hole_pattern/boss feature entries from the recognizers, de-duped
    so a pattern's member holes aren't also counted as standalone holes."""
    from collections import Counter

    from build123d_mcp.tools.find_features import find_bosses, find_hole_patterns, find_holes

    feats: list = []
    pats = json.loads(find_hole_patterns(session, object_name)).get("patterns", [])
    holes = json.loads(find_holes(session, object_name)).get("holes", [])
    bosses = json.loads(find_bosses(session, object_name)).get("bosses", [])

    pattern_dia_counts: Counter = Counter()
    for p in pats:
        members = p.get("holes", [])
        dias = [h.get("diameter") for h in members if h.get("diameter") is not None]
        dia = _round(dias[0]) if dias else None
        entry: dict = {"kind": "hole_pattern", "pattern": p.get("type"), "holes": len(members)}
        if p.get("type") == "bolt_circle" and p.get("diameter") is not None:
            entry["bcd_mm"] = _round(p["diameter"])
        if p.get("type") == "linear_array" and p.get("pitch") is not None:
            entry["pitch_mm"] = _round(p["pitch"])
        if dia is not None:
            entry["diameter_mm"] = dia
            pattern_dia_counts[dia] += len(members)
        feats.append(entry)

    # Standalone (non-pattern) holes, one entry per diameter. Group by verify_spec's
    # OWN match tolerance (`_close`), not an exact key: verify counts every hole
    # within tol of the entry's Ø, so two holes 0.08 mm apart would otherwise each
    # claim count 1 while verify matches both → a self-failing spec.
    hole_dia_counts: Counter = Counter(
        _round(h["diameter"]) for h in holes if h.get("diameter") is not None
    )
    standalone: list[float] = []
    for dia, cnt in hole_dia_counts.items():
        standalone.extend([dia] * max(0, cnt - pattern_dia_counts.get(dia, 0)))

    clusters: list[list] = []  # [representative_diameter, count]
    for dia in sorted(standalone):
        for c in clusters:
            if _close(dia, c[0]):
                c[1] += 1
                break
        else:
            clusters.append([dia, 1])

    for rep, count in clusters:
        entry = {"kind": "hole", "diameter_mm": rep}
        # Assert an exact count only when no pattern shares this Ø (within tol):
        # verify matches by diameter only (pattern members included), so a count
        # alongside a same-Ø pattern would double-count → at-least-one fallback.
        if not any(_close(rep, pd) for pd in pattern_dia_counts):
            entry["count"] = count
        feats.append(entry)

    boss_counts: Counter = Counter(
        (_round(b["diameter"]), _round(b["height"]))
        for b in bosses
        if b.get("diameter") is not None and b.get("height") is not None
    )
    for (dia, h), _cnt in boss_counts.items():
        feats.append({"kind": "boss", "diameter_mm": dia, "height_mm": h})
    return feats


def _suggest_parameters(session) -> list:
    from build123d_mcp._design_audit_subprocess import _extract_params
    from build123d_mcp.tools.design_audit import _assemble

    program = _assemble(session)
    params = _extract_params(program)[0] if program else []
    out = []
    for p in params:
        if p.get("reassigned"):
            continue  # a band around the first (dead) value would be misleading
        v = p["value"]
        if not math.isfinite(v):
            continue  # an overflow literal (1e999→inf) would emit non-strict-JSON
        lo, hi = sorted((v * 0.9, v * 1.1))  # ±10%, order-safe for negative v
        if hi - lo < _ABS_TOL:  # zero / near-zero → widen to an absolute band
            lo, hi = v - _ABS_TOL, v + _ABS_TOL
        out.append({"name": p["name"], "min": round(lo, 4), "max": round(hi, 4)})
    return out


def suggest_spec(session, object_name: str = "") -> str:
    """Draft a starter design-intent spec from the current (or named) shape.

    Introspects the shape with the same primitives verify_spec checks against
    (bbox, validity gate, feature recognition, parameter extraction) and returns
    a spec that describes what was built — so the agent edits detected values
    against the intended drawing rather than authoring from scratch. Envelope /
    volume use a ±2% band and parameters a ±10% band (editable defaults).
    """
    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err

    from build123d_mcp.tools.measure import measure as _measure

    m = json.loads(_measure(session, object_name))
    gate = _gate_report(shape)
    bb = m["bbox"]
    vlo, vhi = _pct_band(m["volume"])
    spec = {
        "envelope_mm": {ax: _pct_band(bb[f"{ax}size"]) for ax in ("x", "y", "z")},
        "solid": {"count": gate["n_solids"], "valid": bool(gate["passes_gate"])},
        "volume_mm3": {"min": vlo, "max": vhi},
        "features": _suggest_features(session, object_name),
        "parameters": _suggest_parameters(session),
    }
    return json.dumps(
        {
            "spec": spec,
            "note": (
                "Detected from the CURRENT shape — a starter, not ground truth. Review and edit each "
                "value against your intended drawing (the bands are editable defaults), then pass the "
                "`spec` object to verify_spec(). Not captured: absolute positions; countersinks (drafted "
                "as plain holes — add `counterbore`/`spotface`/a `countersink` feature yourself), wall "
                "thickness (`wall_thickness_at`/`min_wall_mm`), material_at_point, and cosmetic/other "
                "features (fillets, chamfers, pockets, ribs) the recognizers don't cover."
            ),
        },
        indent=2,
    )
