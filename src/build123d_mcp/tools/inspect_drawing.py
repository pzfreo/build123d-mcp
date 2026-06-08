"""inspect_drawing — structured bbox/annotation report for a 2D drawing session."""

import json
import os
import re
import xml.etree.ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import parse as _safe_xml_parse

from build123d_mcp.tools._paths import check_input_size, safe_input_path


def _bbox_dict(shape) -> dict | None:
    try:
        bb = shape.bounding_box()
        return {
            "min_x": round(bb.min.X, 4),
            "min_y": round(bb.min.Y, 4),
            "max_x": round(bb.max.X, 4),
            "max_y": round(bb.max.Y, 4),
            "width": round(bb.size.X, 4),
            "height": round(bb.size.Y, 4),
        }
    except Exception:
        return None


_SVG_NS = "{http://www.w3.org/2000/svg}"


def _inspect_svg(svg_path: str) -> str:
    """SVG-file mode: parse an SVG and report page bbox, layer ids,
    text contents, and basic structural counts.

    Decouples inspection from the session-registration ceremony, so SVGs
    written by any code path (CI artifacts, third-party exports, prior
    runs) can be inspected.
    """
    # Reject reads outside the allowed roots before touching the filesystem;
    # the .dims.json sidecar below derives from this resolved path so it stays
    # within the same validated directory.
    svg_path = safe_input_path(svg_path)
    check_input_size(svg_path, "svg")
    if not os.path.isfile(svg_path):
        return json.dumps({"error": f"SVG file not found: {svg_path}"})
    try:
        # Hardened parse: defusedxml forbids entity/DTD expansion, so a
        # billion-laughs SVG is rejected instead of exhausting memory (#189).
        tree = _safe_xml_parse(svg_path)
    except ET.ParseError as e:
        return json.dumps({"error": f"SVG parse error: {e}"})
    except DefusedXmlException as e:
        return json.dumps({"error": f"SVG rejected by XML hardening: {e}"})

    root = tree.getroot()

    def _strip_unit(val: str | None) -> float | None:
        if not val:
            return None
        m = re.match(r"([\d.\-eE]+)", val)
        return float(m.group(1)) if m else None

    page = {
        "width": _strip_unit(root.get("width")),
        "height": _strip_unit(root.get("height")),
        "viewBox": root.get("viewBox"),
    }

    layers: list[dict] = []
    text_entries: list[dict] = []
    counts = {
        "path": 0,
        "line": 0,
        "polyline": 0,
        "polygon": 0,
        "rect": 0,
        "circle": 0,
        "g": 0,
        "text": 0,
    }

    for elem in root.iter():
        tag = elem.tag.replace(_SVG_NS, "")
        if tag in counts:
            counts[tag] += 1
        if tag == "g":
            layers.append(
                {
                    "id": elem.get("id"),
                    "transform": elem.get("transform"),
                    "fill": elem.get("fill"),
                }
            )
        elif tag == "text":
            text_entries.append(
                {
                    "id": elem.get("id"),
                    "x": _strip_unit(elem.get("x")),
                    "y": _strip_unit(elem.get("y")),
                    "text": "".join(elem.itertext()).strip(),
                    "fill": elem.get("fill"),
                }
            )

    # Sidecar: read <svg_path>.dims.json written by save_drawing_annotations().
    # build123d renders Text as filled glyph paths so <text> elements are absent;
    # the sidecar restores label content and measured-length metadata.
    sidecar_path = os.path.splitext(svg_path)[0] + ".dims.json"
    # The sidecar is a separate caller-influenced file; bound its size too (#189)
    # so it can't be the unguarded side door the SVG limit above closes.
    check_input_size(sidecar_path, "svg")
    annotations: dict = {}
    if os.path.isfile(sidecar_path):
        try:
            with open(sidecar_path) as f:
                annotations = json.load(f)
        except Exception:
            pass

    return json.dumps(
        {
            "mode": "svg",
            "path": svg_path,
            "page": page,
            "layers": layers,
            "text": text_entries,
            "counts": counts,
            "annotations": annotations,
            "annotations_note": (
                "annotations loaded from sidecar"
                if annotations
                else "no sidecar found — call save_drawing_annotations(svg_path) after building"
            ),
        },
        indent=2,
    )


def inspect_drawing(session, objects: str = "", svg_path: str = "") -> str:
    """Return a JSON report with bounding boxes and annotation metadata.

    For each named object in the session, reports its page-space bounding box,
    topology counts, and — if the object was registered via annotate() using a
    build123d_drafting Dimension or Leader object — the stored label string,
    measured length, and tip/elbow coordinates.

    Args:
        objects: comma-separated object names to inspect. Empty = all objects.
        svg_path: if given, inspect an SVG file from disk instead of the
            session. Reports page size, layer ids, text content + positions,
            and element counts. Useful for SVGs produced outside the sandbox
            (CI artifacts, third-party exports).

    Returns:
        JSON string. Session mode has keys: objects, drawing_bbox, lint.
        SVG mode has keys: mode, path, page, layers, text, counts.
    """
    if svg_path:
        return _inspect_svg(svg_path)

    if objects:
        names = [n.strip() for n in objects.split(",") if n.strip()]
        missing = [n for n in names if n not in session.objects]
        if missing:
            return json.dumps({"error": f"Unknown object(s): {', '.join(missing)}"})
    else:
        names = list(session.objects.keys())

    if not names:
        return json.dumps({"error": "No objects in session. Execute drawing code first."})

    result_objects: dict = {}
    all_min_x = all_min_y = float("inf")
    all_max_x = all_max_y = float("-inf")

    for name in names:
        shape = session.objects[name]
        bb = _bbox_dict(shape)
        if bb:
            all_min_x = min(all_min_x, bb["min_x"])
            all_min_y = min(all_min_y, bb["min_y"])
            all_max_x = max(all_max_x, bb["max_x"])
            all_max_y = max(all_max_y, bb["max_y"])

        try:
            face_count = len(shape.faces())
        except Exception:
            face_count = None
        try:
            edge_count = len(shape.edges())
        except Exception:
            edge_count = None

        result_objects[name] = {
            "bbox": bb,
            "faces": face_count,
            "edges": edge_count,
            "annotation": session.drawing_annotations.get(name),
        }

    drawing_bbox = None
    if all_min_x != float("inf"):
        drawing_bbox = {
            "min_x": round(all_min_x, 4),
            "min_y": round(all_min_y, 4),
            "max_x": round(all_max_x, 4),
            "max_y": round(all_max_y, 4),
            "width": round(all_max_x - all_min_x, 4),
            "height": round(all_max_y - all_min_y, 4),
        }

    lint = _lint(result_objects, session.drawing_annotations)

    return json.dumps(
        {"objects": result_objects, "drawing_bbox": drawing_bbox, "lint": lint},
        indent=2,
    )


def _lint(objects: dict, annotations: dict) -> list[str]:
    warnings = []
    for name, entry in objects.items():
        ann = annotations.get(name)
        if ann is None:
            continue
        label = ann.get("label_str", "")
        measured = ann.get("measured_length")
        if label and measured and measured > 1e-6:
            import re

            nums = re.findall(r"\d+\.?\d*", label.split("±")[0].split("+")[0].lstrip("ø⌀Rr"))
            if nums:
                try:
                    label_val = float(nums[0])
                    ratio = abs(label_val - measured) / measured
                    if ratio > 0.005:
                        warnings.append(
                            f"'{name}': label '{label}' value {label_val:.3f} differs from "
                            f"measured length {measured:.3f} by {ratio * 100:.1f}% — possible axis swap"
                        )
                except ValueError:
                    pass

        bb = entry.get("bbox")
        elbow = ann.get("elbow")
        # For leaders, check elbow vs text bbox (approximated from overall bbox)
        if elbow and bb:
            ex, ey = elbow
            if bb["min_x"] <= ex <= bb["max_x"] and bb["min_y"] <= ey <= bb["max_y"]:
                warnings.append(
                    f"'{name}': Leader elbow ({ex:.2f}, {ey:.2f}) may be inside the label bbox"
                )

    return warnings
