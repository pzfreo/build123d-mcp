"""apply_tool_visibility() trims optional tools from the served surface (#367): the
part-library tools auto-hide with no library, and named groups (drawing) can be
opted out. Fast + cross-platform; the stdio end-to-end variants are in test_outcomes.

Each test snapshots the shared _tool_manager registry and restores it, so removing
tools doesn't pollute other in-process tests.
"""

import contextlib

from build123d_mcp import server

_DRAWING = {
    "inspect_drawing",
    "lint_drawing",
    "render_drawing",
    "view_axes",
    "save_drawing_annotations",
    "suggest_view_layout",
}


@contextlib.contextmanager
def _restoring_registry():
    tm = server.mcp._tool_manager
    snapshot = dict(tm._tools)
    try:
        yield tm
    finally:
        tm._tools.clear()
        tm._tools.update(snapshot)


def test_default_keeps_all_optional_tools():
    with _restoring_registry() as tm:
        server.apply_tool_visibility((), has_library=True)
        names = set(tm._tools)
    assert {"search_library", "load_part"} <= names
    assert _DRAWING <= names


def test_library_tools_hide_without_library():
    with _restoring_registry() as tm:
        server.apply_tool_visibility((), has_library=False)
        names = set(tm._tools)
    assert "search_library" not in names and "load_part" not in names
    assert _DRAWING <= names  # drawing untouched
    assert "measure" in names  # core untouched


def test_disable_drawing_group():
    with _restoring_registry() as tm:
        server.apply_tool_visibility(("drawing",), has_library=True)
        names = set(tm._tools)
    assert not (_DRAWING & names)
    assert "search_library" in names  # library kept (has_library=True)
    assert {"execute", "measure", "render_view"} <= names  # core untouched


def test_unknown_group_warns_and_is_ignored(capsys):
    with _restoring_registry() as tm:
        server.apply_tool_visibility(("bogus",), has_library=True)
        names = set(tm._tools)
    assert "bogus" in capsys.readouterr().err
    assert _DRAWING <= names and "search_library" in names  # nothing dropped
