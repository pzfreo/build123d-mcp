"""lint_drawing — structural checks on a 2D drawing.

Two modes:

  1. Session mode: reconstructs the session's annotations as lightweight duck-typed
     stand-ins and **delegates the geometry checks to the helpers**
     (`lint_drawing` + `find_interferences`) — single source of truth, no
     duplicated check logic. The MCP keeps only what the helpers can't do from
     the stored data: the leader elbow check (leader text geometry isn't stored
     separately) and the per-edge page-bounds check.

  2. SVG mode: scans an exported SVG for export-only pathologies — native
     `<text>` elements (build123d renders glyph paths, so any `<text>` won't
     DXF-export) — plus sidecar label checks.

The structured violation list is JSON so the LLM can iterate without rendering.
Each violation's `check` is the helpers' stable `LintIssue.code`.
"""
import json
import os
import re
import xml.etree.ElementTree as ET

from types import SimpleNamespace

from build123d_drafting import (
    find_interferences,
    lint_drawing as _helper_lint,
)

from build123d_mcp.tools._paths import safe_input_path

# Checks that apply to a single annotation — run per-item so the violation can
# carry the session object name (the helpers' issues only know the label text).
_PER_ITEM_CODES = {"label_vs_measured", "dim_inside_part", "leader_line_through_text"}

# The MCP elevates these helper "warning"s to "error" in its own contract.
_SEVERITY_OVERRIDE = {"label_vs_measured": "error"}


def _violation(issue, obj: str) -> dict:
    return {
        "severity": _SEVERITY_OVERRIDE.get(issue.code, issue.severity),
        "check": issue.code or "lint",
        "object": obj,
        "message": issue.message,
    }


def _pair_object(issue, items) -> str:
    """Best-effort: session names whose label text appears in a pairwise message."""
    names = []
    for name, res in items:
        label = getattr(res, "label", "")
        if label and (f'"{label}"' in issue.message or f"'{label}'" in issue.message):
            names.append(name)
    return "+".join(dict.fromkeys(names))


def _standin(shape, **attrs):
    """A lightweight duck-typed lint item (helpers 0.2.0 dropped the *Result
    dataclasses; the helper lint reads attributes, not types).

    The caller passes ``segments`` straight from the stored metadata — the
    helper objects precompute their centrelines (cheap tuples), so the
    geometry-precise interference works WITHOUT re-extracting LINE edges from
    the live traced-face geometry at lint time (that ran hundreds of OCC ops
    per annotation and blew the worker's 10 s budget). The live
    ``bounding_box`` is borrowed for the part-overlap and centerline extent
    checks.
    """
    ns = SimpleNamespace(**attrs)
    if "segments" not in attrs:
        ns.segments = []
    if shape is not None:
        ns.bounding_box = shape.bounding_box
    return ns


def _reconstruct(session):
    """[(name, standin)] for each annotation that can be linted from stored data.

    Builds duck-typed stand-ins (label / label_bbox / elbow / measured_length /
    is_centerline) backed by the live geometry, matching the attributes the
    helpers' 0.2.0 linter reads.
    """
    items = []
    for name, meta in session.drawing_annotations.items():
        if name not in session.objects:
            continue
        shape = session.objects[name]
        label = meta.get("label_str", "")
        segs = meta.get("segments") or []
        if meta.get("is_centerline") or meta.get("type") in ("Centerline", "centerline"):
            items.append((name, _standin(shape, label=label, is_centerline=True,
                                         label_bbox=None, segments=segs)))
        elif meta.get("elbow") is not None:
            items.append((name, _standin(shape, label=label,
                                         tip=tuple(meta.get("tip") or (0.0, 0.0)),
                                         elbow=tuple(meta["elbow"]),
                                         label_bbox=meta.get("label_bbox"), segments=segs)))
        else:
            items.append((name, _standin(shape, label=label,
                                         measured_length=meta.get("measured_length") or 0.0,
                                         dim_level_y=meta.get("dim_level_y"),
                                         label_bbox=meta.get("label_bbox"), segments=segs)))
    return items


def _lint_session(
    session, drawing_scale: float = 1.0, view_shape_names: list[str] | None = None
) -> list[dict]:
    items = _reconstruct(session)
    results = [r for _, r in items]
    violations: list[dict] = []

    # per-item checks (carry the session name)
    for name, res in items:
        for issue in _helper_lint([res], drawing_scale=drawing_scale):
            if issue.code in _PER_ITEM_CODES:
                violations.append(_violation(issue, name))

    # pairwise checks + geometry-precise interference over the whole set
    if len(results) >= 2:
        # Resolve named view-outline shapes for view_annotation_overlap / view_overlap checks.
        view_shapes = None
        if view_shape_names is not None:
            missing = [n for n in view_shape_names if n not in session.objects]
            for m in missing:
                violations.append({
                    "severity": "warning",
                    "check": "unknown_view_shape_name",
                    "object": m,
                    "message": (
                        f"view_shape_names: '{m}' not found in session objects "
                        f"— view-overlap check may be incomplete. "
                        f"Call show(shape, '{m}') inside execute() before running lint."
                    ),
                })
            view_shapes = [session.objects[n] for n in view_shape_names if n in session.objects]
        for issue in _helper_lint(results, drawing_scale=drawing_scale, view_shapes=view_shapes):
            if issue.code not in _PER_ITEM_CODES:
                violations.append(_violation(issue, _pair_object(issue, items)))
    for issue in find_interferences(results):
        violations.append(_violation(issue, _pair_object(issue, items)))

    # the one MCP-native check: per-edge page bounds (more detailed than the
    # helper's label↔frame check).
    if session.drawing_page:
        violations += _lint_page_bounds(
            session.drawing_annotations, session.objects, session.drawing_page)
    return violations


def _lint_page_bounds(annotations: dict, objects: dict, page: dict) -> list[dict]:
    """Check every annotation stays within the drawable page area."""
    violations: list[dict] = []
    for name in annotations:
        if name not in objects:
            continue
        try:
            bb = objects[name].bounding_box()
        except Exception:
            continue
        overshoots = []
        if bb.min.X < page["min_x"]:
            overshoots.append(f"left by {page['min_x'] - bb.min.X:.1f} mm")
        if bb.max.X > page["max_x"]:
            overshoots.append(f"right by {bb.max.X - page['max_x']:.1f} mm")
        if bb.min.Y < page["min_y"]:
            overshoots.append(f"bottom by {page['min_y'] - bb.min.Y:.1f} mm")
        if bb.max.Y > page["max_y"]:
            overshoots.append(f"top by {bb.max.Y - page['max_y']:.1f} mm")
        for detail in overshoots:
            violations.append({
                "severity": "error",
                "check": "annotation_out_of_bounds",
                "object": name,
                "message": (
                    f"annotation '{name}' extends past page edge ({detail}) "
                    f"— move it inward or reduce offset"
                ),
            })
    return violations


_SVG_NS = "{http://www.w3.org/2000/svg}"


def _lint_svg(svg_path: str, drawing_scale: float = 1.0) -> list[dict]:
    """Layer-level checks on an exported SVG file, plus sidecar label checks.

    - **native `<text>` elements** — build123d renders text as filled glyph
      *paths*, never `<text>`. So any `<text>` means the SVG was produced (or
      post-processed) outside the geometry pipeline: it won't survive a DXF
      export and won't scale with the model. Flag it (worse still when it also
      has no fill, where it renders as illegible outlines).
    - label-vs-measured divergence from the `.dims.json` sidecar (no live
      geometry here, so the helper's per-item check is run on shape-less
      duck-typed stand-ins).
    """
    # Reject reads outside the allowed roots before touching the filesystem;
    # the .dims.json sidecar below derives from this resolved path so it stays
    # within the same validated directory.
    svg_path = safe_input_path(svg_path)
    violations: list[dict] = []
    try:
        tree = ET.parse(svg_path)
    except (FileNotFoundError, ET.ParseError) as e:
        return [{"severity": "error", "check": "svg_parse",
                 "object": svg_path, "message": str(e)}]

    def walk(elem, inherited_fill):
        fill = elem.get("fill", inherited_fill)
        m = re.search(r"fill:\s*([^;]+)", elem.get("style", ""))
        if m:
            fill = m.group(1).strip()
        if elem.tag.replace(_SVG_NS, "") == "text":
            layer_id = elem.get("id") or "?"
            no_fill = fill in (None, "none", "")
            detail = (" and has no fill (renders as illegible outlines)"
                      if no_fill else "")
            violations.append({
                "severity": "error",
                "check": "native_svg_text",
                "object": layer_id,
                "message": (
                    f"native <text> element id='{layer_id}'{detail}. build123d "
                    f"renders text as filled glyph paths, not <text> — native SVG "
                    f"text won't export to DXF and won't scale with the model. "
                    f"Re-export the label from build123d geometry."
                ),
            })
        for child in elem:
            walk(child, fill)

    walk(tree.getroot(), None)

    sidecar = os.path.splitext(svg_path)[0] + ".dims.json"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar) as f:
                annotations = json.load(f)
            for name, meta in annotations.items():
                dim = _standin(
                    None,
                    label=meta.get("label_str", ""),
                    measured_length=meta.get("measured_length") or 0.0,
                    label_bbox=None,
                )
                for issue in _helper_lint([dim], drawing_scale=drawing_scale):
                    if issue.code == "label_vs_measured":
                        violations.append(_violation(issue, name))
        except Exception as exc:
            violations.append({
                "severity": "warning",
                "check": "sidecar_read",
                "object": sidecar,
                "message": f"Could not read sidecar: {exc}",
            })

    return violations


def lint_drawing(
    session, svg_path: str = "", drawing_scale: float = 1.0,
    view_shape_names: list[str] | None = None,
) -> str:
    """Run structural drawing checks; return JSON with a `violations` list.

    Args:
        svg_path: if given, lint the SVG file at this path (mode 2). Otherwise
            lint the live session (mode 1).
        drawing_scale: N:1 factor the geometry was scaled by before projecting;
            the label-vs-measured check divides measured lengths by it so labels
            carry the real dimension. Session mode only; defaults to 1.0.

    Returns:
        JSON: {"violations": [{severity, check, object, message}, ...]}
    """
    violations = (_lint_svg(svg_path, drawing_scale) if svg_path
                  else _lint_session(session, drawing_scale, view_shape_names))
    return json.dumps({"violations": violations}, indent=2)
