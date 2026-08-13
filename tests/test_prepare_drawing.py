import json

from PIL import Image, ImageDraw

from build123d_mcp.tools.prepare_drawing import prepare_drawing


def test_prepare_drawing_emits_overview_and_regions(tmp_path):
    source = tmp_path / "drawing.png"
    image = Image.new("RGB", (1000, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((80, 100, 350, 360), outline="black", width=5)
    draw.ellipse((600, 130, 880, 410), outline="black", width=5)
    image.save(source)

    result = json.loads(prepare_drawing(str(source), str(tmp_path / "regions"), padding=10))

    assert result["image_size_px"] == [1000, 700]
    assert len(result["regions"]) == 2
    assert (tmp_path / "regions" / "overview.png").is_file()
    assert (tmp_path / "regions" / "region_01.png").is_file()
    assert (tmp_path / "regions" / "region_02.png").is_file()


def test_prepare_drawing_rejects_bad_limits(tmp_path):
    source = tmp_path / "drawing.png"
    Image.new("RGB", (100, 100), "white").save(source)

    for kwargs in ({"max_regions": 0}, {"max_regions": 31}, {"padding": -1}):
        try:
            prepare_drawing(str(source), str(tmp_path / "regions"), **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {kwargs}")
