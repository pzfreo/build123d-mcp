"""Model-directed raster crop helpers."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageOps

from build123d_mcp.tools._paths import check_input_size, safe_input_path, safe_output_path


def crop_drawing(
    image_path: str,
    bbox_px: list[int],
    output_path: str = "drawing_crop.png",
    scale: float = 2.0,
    autocontrast: bool = True,
) -> str:
    """Save one exact, enlarged crop and return its source-pixel mapping."""
    path = safe_input_path(image_path)
    check_input_size(path, "raster")
    if len(bbox_px) != 4:
        raise ValueError("bbox_px must be [x0, y0, x1, y1]")
    if not 0.25 <= scale <= 12:
        raise ValueError("scale must be between 0.25 and 12")
    image = Image.open(path).convert("RGB")
    width, height = image.size
    x0, y0, x1, y1 = (int(v) for v in bbox_px)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(f"bbox_px {bbox_px} is outside image bounds [0, 0, {width}, {height}]")
    crop = image.crop((x0, y0, x1, y1))
    if autocontrast:
        crop = ImageOps.autocontrast(crop)
    out_size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
    crop = crop.resize(out_size, Image.Resampling.LANCZOS)
    output = Path(safe_output_path(output_path))
    output.parent.mkdir(parents=True, exist_ok=True)
    crop.save(output)
    return json.dumps(
        {
            "crop": str(output),
            "source_image": path,
            "source_bbox_px": [x0, y0, x1, y1],
            "crop_size_px": list(out_size),
            "scale": scale,
            "coordinate_mapping": {
                "crop_to_source": [
                    [round(1.0 / scale, 12), 0.0, x0],
                    [0.0, round(1.0 / scale, 12), y0],
                    [0.0, 0.0, 1.0],
                ],
                "formula": "source_x=x0+crop_x/scale; source_y=y0+crop_y/scale",
            },
        },
        indent=2,
    )
