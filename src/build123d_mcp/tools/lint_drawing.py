"""lint_drawing — standalone structural checks on a 2D drawing.

Two modes:

  1. Session mode: scans session.objects + session.drawing_annotations and runs
     the same checks inspect_drawing reports inline (label-vs-measured-length
     divergence, leader elbow inside the label bbox).

  2. SVG mode: scans an SVG file on disk for layer-level pathologies that only
     show up at export time — most importantly text on a layer with no fill
     (renders as illegible thick outlines).

The structured violation list is JSON so the LLM can iterate on a drawing
without rendering it.
"""
import json
import re
import xml.etree.ElementTree as ET


def _lint_annotations(annotations: dict, objects: dict | None = None) -> list[dict]:
    """Run label-vs-measured and leader checks against an annotations dict.

    Args:
        annotations: mapping of name → annotation metadata dict.
        objects: optional session.objects for leader elbow bbox check (unavailable
            in SVG mode — the geometry isn't loaded).
    """
    violations: list[dict] = []

    for name, ann in annotations.items():
        label = ann.get("label_str", "")
        measured = ann.get("measured_length")
        if label and measured and measured > 1e-6:
            nums = re.findall(
                r"\d+\.?\d*",
                label.split("±")[0].split("+")[0].lstrip("ø⌀Rr"),
            )
            if nums:
                try:
                    label_val = float(nums[0])
                    ratio = abs(label_val - measured) / measured
                    if ratio > 0.005:
                        violations.append({
                            "severity": "error",
                            "check": "label_vs_measured",
                            "object": name,
                            "message": (
                                f"label '{label}' value {label_val:.3f} differs from "
                                f"measured length {measured:.3f} by {ratio*100:.1f}% "
                                f"— possible axis swap"
                            ),
                        })
                except ValueError:
                    pass

        elbow = ann.get("elbow")
        if elbow and objects is not None and name in objects:
            try:
                bb = objects[name].bounding_box()
                if (bb.min.X <= elbow[0] <= bb.max.X
                        and bb.min.Y <= elbow[1] <= bb.max.Y):
                    violations.append({
                        "severity": "warning",
                        "check": "leader_elbow_in_label",
                        "object": name,
                        "message": (
                            f"leader elbow ({elbow[0]:.2f}, {elbow[1]:.2f}) "
                            f"may be inside the label bbox"
                        ),
                    })
            except Exception:
                pass

    return violations


def _lint_overlap(annotations: dict, objects: dict) -> list[dict]:
    """Check every pair of annotations for bounding-box overlap.

    Uses dim_level_y (stored by annotate()) to skip stacked dims whose
    extension lines share the same X range but occupy different Y levels.

    Handles centerline objects specially:
    - centerline-vs-centerline pairs are skipped.
    - dim-vs-centerline pairs use the dim's label_bbox for precision.
    """
    violations: list[dict] = []
    names = [n for n in annotations if n in objects]
    for i, name_a in enumerate(names):
        for name_b in names[i + 1:]:
            try:
                is_cl_a = annotations[name_a].get("type") == "centerline"
                is_cl_b = annotations[name_b].get("type") == "centerline"

                # Skip centerline-vs-centerline
                if is_cl_a and is_cl_b:
                    continue

                # Centerline-vs-dim: use label_bbox for precision
                if is_cl_a or is_cl_b:
                    dim_name = name_b if is_cl_a else name_a
                    cl_name = name_a if is_cl_a else name_b
                    v = _lint_centerline_label_overlap(
                        dim_name, annotations[dim_name], objects[dim_name],
                        cl_name, objects[cl_name],
                    )
                    if v:
                        violations.append(v)
                    continue

                level_a = annotations[name_a].get("dim_level_y")
                level_b = annotations[name_b].get("dim_level_y")
                if (level_a is not None and level_b is not None
                        and abs(level_a - level_b) > 3.0):
                    continue  # different Y levels → stacked, not colliding
                ba = objects[name_a].bounding_box()
                bb = objects[name_b].bounding_box()
                ox = max(0.0, min(ba.max.X, bb.max.X) - max(ba.min.X, bb.min.X))
                oy = max(0.0, min(ba.max.Y, bb.max.Y) - max(ba.min.Y, bb.min.Y))
                if ox > 0.5 and oy > 0.5:
                    violations.append({
                        "severity": "warning",
                        "check": "annotation_overlap",
                        "object": f"{name_a}+{name_b}",
                        "message": (
                            f"annotations '{name_a}' and '{name_b}' overlap by "
                            f"{ox:.1f}×{oy:.1f} mm — increase offset or spacing"
                        ),
                    })
            except Exception:
                pass
    return violations


def _lint_centerline_label_overlap(
    dim_name: str,
    dim_ann: dict,
    dim_obj,
    cl_name: str,
    cl_obj,
) -> dict | None:
    """Return a violation dict if the dim's label overlaps the centerline, else None."""
    try:
        cl_bb = cl_obj.bounding_box()

        # Use stored label_bbox when available; fall back to full object bbox
        label_bbox = dim_ann.get("label_bbox")
        if label_bbox is not None:
            lmin_x, lmin_y, lmax_x, lmax_y = label_bbox
        else:
            db = dim_obj.bounding_box()
            lmin_x, lmin_y = db.min.X, db.min.Y
            lmax_x, lmax_y = db.max.X, db.max.Y

        cl_w = cl_bb.max.X - cl_bb.min.X
        cl_h = cl_bb.max.Y - cl_bb.min.Y

        if cl_w < 0.1:
            # Vertical centerline: check if its X falls inside label X range
            cl_x = (cl_bb.min.X + cl_bb.max.X) / 2.0
            ox = min(cl_x - lmin_x, lmax_x - cl_x) if lmin_x < cl_x < lmax_x else 0.0
        else:
            ox = max(0.0, min(lmax_x, cl_bb.max.X) - max(lmin_x, cl_bb.min.X))

        if cl_h < 0.1:
            # Horizontal centerline: check if its Y falls inside label Y range
            cl_y = (cl_bb.min.Y + cl_bb.max.Y) / 2.0
            oy = min(cl_y - lmin_y, lmax_y - cl_y) if lmin_y < cl_y < lmax_y else 0.0
        else:
            oy = max(0.0, min(lmax_y, cl_bb.max.Y) - max(lmin_y, cl_bb.min.Y))

        if ox > 0.5 and oy > 0.5:
            dim_label = dim_ann.get("label_str", dim_name)
            return {
                "severity": "warning",
                "check": "label_centerline_overlap",
                "object": f"{dim_name}+{cl_name}",
                "message": (
                    f"label '{dim_label}' overlaps centerline '{cl_name}' by "
                    f"{ox:.1f}×{oy:.1f} mm — use label_offset_x to shift the label "
                    f"or increase the dim offset to clear the centerline"
                ),
            }
    except Exception:
        pass
    return None


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


def _lint_session(session) -> list[dict]:
    """Run structural lint on session-registered annotations."""
    violations = _lint_annotations(session.drawing_annotations, objects=session.objects)
    violations += _lint_overlap(session.drawing_annotations, session.objects)
    if session.drawing_page:
        violations += _lint_page_bounds(
            session.drawing_annotations, session.objects, session.drawing_page
        )
    return violations


_SVG_NS = "{http://www.w3.org/2000/svg}"


def _lint_svg(svg_path: str) -> list[dict]:
    """Layer-level checks on an SVG file, plus sidecar annotation checks.

    Catches:
    - text elements on a group with fill='none' or no fill attribute, which
      renders glyphs as outlines rather than filled shapes.
    - label-vs-measured divergence from the .dims.json sidecar (written by
      save_drawing_annotations()) — same axis-swap check as session mode.
    """
    import os, json as _json
    violations: list[dict] = []
    try:
        tree = ET.parse(svg_path)
    except (FileNotFoundError, ET.ParseError) as e:
        return [{"severity": "error", "check": "svg_parse",
                 "object": svg_path, "message": str(e)}]

    root = tree.getroot()

    def walk(elem, inherited_fill):
        fill = elem.get("fill", inherited_fill)
        style = elem.get("style", "")
        m = re.search(r"fill:\s*([^;]+)", style)
        if m:
            fill = m.group(1).strip()

        tag = elem.tag.replace(_SVG_NS, "")
        if tag == "text":
            if fill in (None, "none", ""):
                layer_id = elem.get("id") or "?"
                violations.append({
                    "severity": "error",
                    "check": "text_no_fill",
                    "object": layer_id,
                    "message": (
                        f"<text> element id='{layer_id}' has fill='{fill}'; "
                        f"glyphs will render as thick outlines, not filled. "
                        f"Set fill_color on the SVG layer when exporting."
                    ),
                })

        for child in elem:
            walk(child, fill)

    walk(root, None)

    # Sidecar annotation checks (label-vs-measured, leader-strikethrough).
    sidecar = os.path.splitext(svg_path)[0] + ".dims.json"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar) as f:
                annotations = _json.load(f)
            violations.extend(_lint_annotations(annotations, objects=None))
        except Exception as exc:
            violations.append({
                "severity": "warning",
                "check": "sidecar_read",
                "object": sidecar,
                "message": f"Could not read sidecar: {exc}",
            })

    return violations


def lint_drawing(session, svg_path: str = "") -> str:
    """Run structural drawing checks; return JSON with a `violations` list.

    Args:
        svg_path: if given, lint the SVG file at this path (mode 2). Otherwise
            lint the live session (mode 1).

    Returns:
        JSON: {"violations": [{severity, check, object, message}, ...]}
    """
    if svg_path:
        violations = _lint_svg(svg_path)
    else:
        violations = _lint_session(session)
    return json.dumps({"violations": violations}, indent=2)
