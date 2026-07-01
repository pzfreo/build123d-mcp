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
        if "diameter_mm" in f:
            hd = [h.get("diameter") for h in p.get("holes", []) if h.get("diameter") is not None]
            if not hd or not _close(hd[0], f["diameter_mm"]):
                continue
        out.append(
            {
                "requirement": req,
                "status": "PASS",
                "tier": "recognised",
                "found": {"holes": len(p.get("holes", [])), "bcd": p.get("diameter")},
            }
        )
        return
    n_type = sum(1 for p in patterns if p.get("type") == want_type)
    out.append(
        {
            "requirement": req,
            "status": "FAIL",
            "tier": "recognised",
            "hint": f"found {n_type} {want_type} pattern(s); none matched holes/BCD/Ø",
        }
    )


def _check_hole(f: dict, holes: list, err, out: list) -> None:
    d = f.get("diameter_mm")
    want = f.get("count", 1)
    req = f"{want}× Ø{d} hole" if d is not None else f"{want} hole(s)"
    if err:
        out.append({"requirement": req, "status": "UNVERIFIED", "tier": "unverified", "note": err})
        return
    matching = [h for h in holes if d is None or _close(h.get("diameter"), d)]
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


def _check_features(session, object_name: str, features: list, out: list) -> None:
    from build123d_mcp.tools.find_features import find_bosses, find_hole_patterns, find_holes

    holes = pats = bosses = None
    holes_err = pats_err = bosses_err = None
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

    shape, shape_err = _resolve_shape(session, object_name)
    if shape_err is not None:
        return shape_err

    out: list = []
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

    n_fail = sum(1 for e in out if e["status"] == "FAIL")
    n_pass = sum(1 for e in out if e["status"] == "PASS")
    n_unv = sum(1 for e in out if e["status"] == "UNVERIFIED")
    return json.dumps(
        {
            "conformance": out,
            "summary": {
                "pass": n_pass,
                "fail": n_fail,
                "unverified": n_unv,
                "conforms": n_fail == 0,
            },
            "note": (
                "Proves requested-vs-built for the geometry-checkable requirements only. "
                "conforms means no FAILs; UNVERIFIED requirements are NOT met — they are out of scope "
                "for this gate (declared unverifiable, deferred, or an unrecognised feature). Each line "
                "carries its evidence tier (measured/structural/recognised). This is not a certification; "
                "a human must sign off."
            ),
        },
        indent=2,
    )
