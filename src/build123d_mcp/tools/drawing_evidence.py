"""Model-directed raster crop and coordinate calibration helpers."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
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


def _points(values: list[list[float]], name: str) -> np.ndarray:
    points = np.asarray(values, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2:
        raise ValueError(f"{name} must be a list of [x, y] points")
    if not np.isfinite(points).all():
        raise ValueError(f"{name} must contain only finite numbers")
    return points


def _similarity(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    if len(source) < 2:
        raise ValueError("similarity calibration requires at least 2 point pairs")
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    src = source - source_center
    dst = target - target_center
    variance = float((src * src).sum())
    if variance <= 1e-12:
        raise ValueError("pixel_points must contain distinct points")
    u, singular, vt = np.linalg.svd(src.T @ dst)
    rotation = u @ vt
    # Permit reflection: drawing image Y normally increases downward while a
    # conventional drawing coordinate Y increases upward.
    scale = float(singular.sum() / variance)
    linear = scale * rotation
    translation = target_center - source_center @ linear
    matrix = np.eye(3)
    matrix[:2, :2] = linear.T
    matrix[:2, 2] = translation
    return matrix


def _affine(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    if len(source) < 3:
        raise ValueError("affine calibration requires at least 3 point pairs")
    design = np.column_stack([source, np.ones(len(source))])
    coefficients, _residuals, rank, _singular = np.linalg.lstsq(design, target, rcond=None)
    if rank < 3:
        raise ValueError("affine pixel_points must include 3 non-collinear points")
    matrix = np.eye(3)
    matrix[:2, :] = coefficients.T
    return matrix


def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack([points, np.ones(len(points))])
    return (matrix @ homogeneous.T).T[:, :2]


def calibrate_drawing(
    pixel_points: list[list[float]],
    drawing_points_mm: list[list[float]],
    mode: str = "similarity",
    query_pixels: list[list[float]] | None = None,
    query_drawing_mm: list[list[float]] | None = None,
) -> str:
    """Fit and apply a model-supplied pixel↔millimetre view transform."""
    pixels = _points(pixel_points, "pixel_points")
    drawing = _points(drawing_points_mm, "drawing_points_mm")
    if len(pixels) != len(drawing):
        raise ValueError("pixel_points and drawing_points_mm must have equal length")
    mode = mode.lower()
    if mode == "similarity":
        matrix = _similarity(pixels, drawing)
    elif mode == "affine":
        matrix = _affine(pixels, drawing)
    else:
        raise ValueError("mode must be 'similarity' or 'affine'")
    inverse = np.linalg.inv(matrix)
    fitted = _apply(matrix, pixels)
    errors = np.linalg.norm(fitted - drawing, axis=1)
    linear = matrix[:2, :2]
    singular = np.linalg.svd(linear, compute_uv=False)
    response = {
        "mode": mode,
        "pixel_to_drawing_mm": matrix.round(12).tolist(),
        "drawing_mm_to_pixel": inverse.round(12).tolist(),
        "rms_error_mm": round(float(np.sqrt(np.mean(errors**2))), 6),
        "max_error_mm": round(float(errors.max()), 6),
        "mm_per_pixel": [round(float(v), 9) for v in singular],
        "warning": (
            "The transform is only as accurate as the supplied correspondences. "
            "Use printed dimensions and points from the same orthographic view; do not calibrate an isometric view."
        ),
    }
    if query_pixels:
        response["query_drawing_mm"] = (
            _apply(matrix, _points(query_pixels, "query_pixels")).round(6).tolist()
        )
    if query_drawing_mm:
        response["query_pixels"] = (
            _apply(inverse, _points(query_drawing_mm, "query_drawing_mm")).round(6).tolist()
        )
    return json.dumps(response, indent=2)
