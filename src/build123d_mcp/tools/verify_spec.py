"""verify_spec — check the built solid against a declared design-intent spec.

Composes existing checkers (validity gate, measure, feature recognition, parameter
extraction) into a single conformance report. Each requirement is tagged PASS /
FAIL / UNVERIFIED and carries the *tier* of evidence behind it. The report proves
requested-vs-built for geometry-checkable requirements only and never claims the
design is "correct" — see docs/design-conformance-proposal.md (#335).

MVP scope: envelope, solid count/validity, volume, hole/hole-pattern/boss features,
and top-level numeric parameter ranges. Deferred (reported UNVERIFIED, not silently
ignored): min_wall_mm, parameter robustness (design_audit), non-geometry targets.
"""

import json

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


def _check_features(session, object_name: str, features: list, out: list) -> None:
    from build123d_mcp.tools.find_features import find_bosses, find_hole_patterns, find_holes
    from build123d_mcp.tools.recognizers.countersink import find_countersinks

    holes = pats = bosses = csinks = None
    holes_err = pats_err = bosses_err = csinks_err = None
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


def _check_deferred(spec: dict, out: list) -> None:
    if "min_wall_mm" in spec:
        out.append(
            {
                "requirement": f"min wall ≥ {spec['min_wall_mm']} mm",
                "status": "UNVERIFIED",
                "tier": "unverified",
                "note": "min-wall checking is not implemented yet (deferred); use analyze_printability",
            }
        )
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
