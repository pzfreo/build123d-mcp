"""Phase 1 of the drawing-tool deprecation (#465).

Engineering-drawing generation moves to draftwright, which owns it and publishes
its own skill. This phase announces the move; nothing is withdrawn.

The consumer is a model reading text, not a developer reading warnings, so a
Python DeprecationWarning would be invisible. Two channels reach an agent: the
tool DESCRIPTION (read once, at connect) and the tool RESULT (read on every
call — the only channel that reaches an agent already mid-session holding the
tool in its context, which matters because these tools still work and nothing
else would tell it).
"""

import inspect

import pytest

from build123d_mcp import server

_DRAWING_TOOLS = (
    "inspect_drawing",
    "view_axes",
    "lint_drawing",
    "render_drawing",
    "save_drawing_annotations",
    "suggest_view_layout",
)


@pytest.mark.parametrize("name", _DRAWING_TOOLS)
def test_description_announces_the_move(name):
    doc = inspect.getdoc(getattr(server, name)) or ""
    assert doc.startswith("DEPRECATED")
    assert "draftwright" in doc
    assert "0.5.0" in doc, "the description must say when it goes, not just that it will"


@pytest.mark.parametrize("name", _DRAWING_TOOLS)
def test_deprecated_set_matches_the_tool_group(name):
    """The group already drives --disable-tool-groups; the deprecation must
    cover exactly it, or the two disagree about what 'drawing' means."""
    assert name in server._TOOL_GROUPS["drawing"]


def test_tool_group_has_no_undeprecated_members():
    assert set(server._TOOL_GROUPS["drawing"]) == set(_DRAWING_TOOLS)


def test_string_result_is_prefixed_with_the_notice():
    """The channel that reaches an agent already mid-session."""
    wrapped = server._drawing_deprecation(lambda: "original body")
    result = wrapped()
    assert result.startswith(server._DRAWING_MOVED)
    assert result.endswith("original body")


def test_content_block_result_leads_with_the_notice():
    """render_drawing returns marshalled content blocks, not a string; the
    notice has to survive that path too or the image tool stays silent."""
    from mcp.types import TextContent

    blocks = [TextContent(type="text", text="rendered")]
    result = server._drawing_deprecation(lambda: blocks)()
    assert len(result) == len(blocks) + 1
    assert result[0].text == server._DRAWING_MOVED
    assert result[1:] == blocks


def test_notice_gives_the_replacement_call_not_just_a_url():
    assert "draftwright" in server._DRAWING_MOVED
    assert "make_drawing" in server._DRAWING_MOVED
    assert "execute(" in server._DRAWING_MOVED


def test_wrapper_preserves_the_advertised_contract():
    """functools.wraps must carry the docstring and signature through, or the
    description and input schema the client sees would change."""
    for name in _DRAWING_TOOLS:
        fn = getattr(server, name)
        assert hasattr(fn, "__wrapped__"), f"{name} is not wrapped"
        assert inspect.signature(fn) == inspect.signature(fn.__wrapped__)
        assert fn.__name__ == name


def test_non_drawing_tools_are_untouched():
    """render_view stays — it is model rendering, not drawing — and must not
    have picked up a notice."""
    for name in ("render_view", "measure", "validate", "export"):
        doc = inspect.getdoc(getattr(server, name)) or ""
        assert not doc.startswith("DEPRECATED"), name


def test_install_skill_still_installs_drawing_but_says_it_moved(tmp_path):
    """Phase 1 withdraws nothing: a working workflow keeps working."""
    from build123d_mcp.tools.install_skill import install_skill

    result = install_skill(target="claude", cwd=tmp_path, skill="drawing")
    assert (tmp_path / ".claude" / "skills" / "b123d-drawing" / "SKILL.md").exists()
    assert "DEPRECATED" in result
    assert "draftwright" in result


def test_install_skill_defaults_to_modeling(tmp_path):
    from build123d_mcp.tools.install_skill import install_skill

    install_skill(target="claude", cwd=tmp_path)
    assert (tmp_path / ".claude" / "skills" / "b123d-modeling" / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / "skills" / "b123d-drawing").exists()


def test_draftwright_is_importable_inside_execute():
    """The whole migration rests on this: draftwright is allowlisted, so a
    drawing is built from live session geometry rather than an exported file.
    (The package need not be installed — only permitted.)"""
    from build123d_mcp.security import IMPORT_ALLOWLIST

    assert "draftwright" in IMPORT_ALLOWLIST
