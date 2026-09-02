import json

import pytest
from PIL import Image

from build123d_mcp.tools.drawing_evidence import crop_drawing


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
