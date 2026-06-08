"""Tests for the build123d://bd_warehouse resource."""

from build123d_mcp.bd_warehouse_resource import build_bd_warehouse_text


def test_bd_warehouse_resource_contains_key_sections():
    text = build_bd_warehouse_text()
    assert "BD_WAREHOUSE COMPONENTS" in text
    assert "Bearings" in text
    assert "Fasteners" in text
    assert "Gears" in text
    assert "Threads" in text


def test_bd_warehouse_resource_lists_common_classes():
    text = build_bd_warehouse_text()
    assert "SocketHeadCapScrew" in text
    assert "HexNut" in text
    assert "SingleRowDeepGrooveBallBearing" in text
    assert "SpurGear" in text


def test_bd_warehouse_resource_shows_sizes_and_types():
    text = build_bd_warehouse_text()
    # Fastener types and sizes should be present
    assert "iso4762" in text  # SocketHeadCapScrew default type
    assert "M6-1" in text  # Common metric size


def test_bd_warehouse_resource_no_double_quoted_defaults():
    text = build_bd_warehouse_text()
    # Defaults should render as 'iso4762' not "'iso4762'"
    assert "\"'iso" not in text


def test_bd_warehouse_resource_registered_in_server():
    from build123d_mcp.server import build123d_bd_warehouse

    text = build123d_bd_warehouse()
    assert "BD_WAREHOUSE" in text
