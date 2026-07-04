import json


def cross_sections(session, object_name: str = "", axis: str = "Z", num_slices: int = 10) -> str:
    from build123d_mcp.tools._bounded import run_bounded_shape_op
    from build123d_mcp.tools.measure import _resolve_shape

    shape = _resolve_shape(session, object_name)
    return run_bounded_shape_op(
        session,
        "cross_sections",
        {"": shape},
        {"axis": axis, "num_slices": num_slices},
        in_process=lambda: _cross_sections_report(shape, axis, num_slices),
    )


def _cross_sections_report(shape, axis: str = "Z", num_slices: int = 10) -> str:
    from build123d_mcp.tools.measure import _cross_sections

    return json.dumps(_cross_sections(shape, axis, num_slices), indent=2)
