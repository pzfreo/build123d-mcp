"""Prepare a raster engineering drawing for efficient model inspection.

This is deliberately a layout/evidence tool, not a drawing interpreter.  It
groups substantial ink regions, emits labelled crops, and reports their pixel
bounds.  It never assigns CAD semantics to a line or overrides printed values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from build123d_mcp.tools._paths import check_input_size, safe_input_path, safe_output_path


def _merge_boxes(boxes: list[list[int]], gap_x: int, gap_y: int) -> list[list[int]]:
    """Merge overlapping/nearby boxes until stable."""
    pending = boxes[:]
    merged: list[list[int]] = []
    while pending:
        a = pending.pop()
        changed = True
        while changed:
            changed = False
            keep = []
            for b in pending:
                separated = (
                    a[2] + gap_x < b[0]
                    or b[2] + gap_x < a[0]
                    or a[3] + gap_y < b[1]
                    or b[3] + gap_y < a[1]
                )
                if separated:
                    keep.append(b)
                else:
                    a = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    changed = True
            pending = keep
        merged.append(a)
    return merged


def prepare_drawing(
    image_path: str,
    output_dir: str = "drawing_regions",
    max_regions: int = 12,
    padding: int = 24,
) -> str:
    """Detect substantial regions in a raster drawing and save labelled crops."""
    path = safe_input_path(image_path)
    check_input_size(path, "raster")
    if not os.path.isfile(path):
        return json.dumps({"error": f"Raster drawing not found: {image_path}"})
    if max_regions < 1 or max_regions > 30:
        raise ValueError("max_regions must be between 1 and 30")
    if padding < 0 or padding > 500:
        raise ValueError("padding must be between 0 and 500 pixels")

    out = Path(safe_output_path(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    image = Image.open(path).convert("RGB")
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape

    # A permissive ink mask works for anti-aliased black/grey linework. Suppress
    # page borders and long title-block/table rules before grouping; otherwise
    # one rule touching the border can turn the entire sheet into one component.
    ink = gray < 235
    ink = ndimage.binary_closing(ink, structure=np.ones((3, 3), dtype=bool))
    layout_ink = ink.copy()
    margin_y, margin_x = round(height * 0.025), round(width * 0.025)
    layout_ink[:margin_y] = False
    layout_ink[height - margin_y :] = False
    layout_ink[:, :margin_x] = False
    layout_ink[:, width - margin_x :] = False
    horizontal_rules = ndimage.binary_opening(
        layout_ink, structure=np.ones((1, max(80, width // 12)), dtype=bool)
    )
    vertical_rules = ndimage.binary_opening(
        layout_ink, structure=np.ones((max(80, height // 12), 1), dtype=bool)
    )
    rules = ndimage.binary_dilation(
        horizontal_rules | vertical_rules, structure=np.ones((5, 5), dtype=bool)
    )
    layout_ink &= ~rules

    # Associate nearby object lines, dimensions and labels while preserving the
    # whitespace between distinct views on a typical benchmark sheet.
    dy = max(5, min(25, round(height * 0.004)))
    dx = max(5, min(25, round(width * 0.004)))
    grouped = ndimage.binary_dilation(layout_ink, structure=np.ones((dy, dx), dtype=bool))
    labels, count = ndimage.label(grouped)
    objects = ndimage.find_objects(labels)

    min_w, min_h = max(50, width // 35), max(40, height // 35)
    min_area = width * height * 0.002
    boxes: list[list[int]] = []
    for slc in objects:
        if slc is None:
            continue
        ys, xs = slc
        x0, y0, x1, y1 = xs.start, ys.start, xs.stop, ys.stop
        if x1 - x0 < min_w or y1 - y0 < min_h:
            continue
        local_ink = int(ink[y0:y1, x0:x1].sum())
        if (x1 - x0) * (y1 - y0) < min_area or local_ink < 100:
            continue
        boxes.append([x0, y0, x1, y1])

    boxes = _merge_boxes(boxes, max(12, round(width * 0.035)), max(12, round(height * 0.02)))
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    boxes = boxes[:max_regions]
    boxes.sort(key=lambda b: (b[1], b[0]))

    overview = image.copy()
    draw = ImageDraw.Draw(overview)
    regions = []
    for index, box in enumerate(boxes, 1):
        x0 = max(0, box[0] - padding)
        y0 = max(0, box[1] - padding)
        x1 = min(width, box[2] + padding)
        y1 = min(height, box[3] + padding)
        crop_path = out / f"region_{index:02d}.png"
        image.crop((x0, y0, x1, y1)).save(crop_path)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(220, 0, 0), width=5)
        draw.rectangle((x0, y0, min(x0 + 58, x1), min(y0 + 34, y1)), fill=(255, 255, 255))
        draw.text((x0 + 6, y0 + 5), str(index), fill=(220, 0, 0))
        regions.append(
            {
                "id": index,
                "bbox_px": [x0, y0, x1, y1],
                "size_px": [x1 - x0, y1 - y0],
                "crop": str(crop_path),
                "ink_fraction": round(float(ink[y0:y1, x0:x1].mean()), 4),
                "layout_hint": (
                    "likely_sheet_metadata"
                    if x0 > width * 0.45 and y0 > height * 0.68
                    else "likely_drawing_content"
                ),
            }
        )

    overview_path = out / "overview.png"
    overview.save(overview_path)
    return json.dumps(
        {
            "image": path,
            "image_size_px": [width, height],
            "overview": str(overview_path),
            "regions": regions,
            "method": "ink-connected layout regions; labels are spatial only, not view semantics",
            "warning": (
                "Printed dimensions remain authoritative. Crops include annotation ink; "
                "do not treat detected bounds as part geometry or trace them automatically."
            ),
            "components_considered": count,
        },
        indent=2,
    )
