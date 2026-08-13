import json

import pytest
from PIL import Image

from build123d_mcp.tools.drawing_evidence import calibrate_drawing, crop_drawing


def test_crop_drawing_returns_exact_mapping(tmp_path):
    source = tmp_path / "drawing.png"
    Image.new("RGB", (400, 300), "white").save(source)
    result = json.loads(
        crop_drawing(str(source), [40, 50, 140, 100], str(tmp_path / "crop.png"), 3)
    )
    assert result["crop_size_px"] == [300, 150]
    assert result["coordinate_mapping"]["crop_to_source"] == [
        [pytest.approx(1 / 3), 0.0, 40],
        [0.0, pytest.approx(1 / 3), 50],
        [0.0, 0.0, 1.0],
    ]
    assert (tmp_path / "crop.png").is_file()


def test_calibrate_similarity_and_queries():
    result = json.loads(
        calibrate_drawing(
            [[100, 200], [300, 200], [100, 100]],
            [[0, 0], [100, 0], [0, 50]],
            query_pixels=[[200, 150]],
            query_drawing_mm=[[25, 25]],
        )
    )
    assert result["rms_error_mm"] == pytest.approx(0)
    assert result["query_drawing_mm"][0] == pytest.approx([50, 25])
    assert result["query_pixels"][0] == pytest.approx([150, 150])


def test_calibrate_affine():
    result = json.loads(
        calibrate_drawing(
            [[0, 0], [100, 0], [0, 200]],
            [[10, 20], [60, 20], [10, 70]],
            mode="affine",
            query_pixels=[[50, 100]],
        )
    )
    assert result["rms_error_mm"] == pytest.approx(0)
    assert result["query_drawing_mm"][0] == pytest.approx([35, 45])
