import json


def cross_sections(session, object_name: str = "", axis: str = "Z", num_slices: int = 10) -> str:
    from build123d_mcp.tools.measure import _cross_sections, _resolve_shape

    shape = _resolve_shape(session, object_name)
    return json.dumps(_cross_sections(shape, axis, num_slices), indent=2)
