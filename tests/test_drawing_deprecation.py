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
import json

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


def test_description_notice_stays_terse():
    """The description is paid by EVERY client on EVERY connect, including ones
    that never touch drawing. The full explanation belongs in the result, which
    is paid only by a caller who actually used the tool. Making a deprecated
    tool more expensive to ignore than it was to keep is the wrong trade."""
    for name in _DRAWING_TOOLS:
        doc = inspect.getdoc(getattr(server, name)) or ""
        first = doc.split("\n", 1)[0]
        notice = first[: first.index("0.5.0") + len("0.5.0.")]
        assert len(notice.encode()) < 160, f"{name}: description notice is {len(notice)}B"
    # The result may be verbose — it is only paid on use.
    assert len(server._DRAWING_MOVED.encode()) > 200


# --- the notice must not break machine-readable results ----------------------
#
# inspect_drawing, lint_drawing and suggest_view_layout return pure JSON.
# Prefixing prose to those breaks json.loads() for every caller that parses
# them, and a deprecation must not cost correctness while the tool still works.


def test_json_object_keeps_parsing_and_carries_the_notice():
    payload = json.dumps({"violations": [], "scale": 1.0}, indent=2)
    out = server._notice_onto_text(payload)
    parsed = json.loads(out)  # would raise before the type-aware notice
    assert parsed["violations"] == []
    assert parsed["scale"] == 1.0
    assert "draftwright" in parsed["_deprecated"]


def test_notice_is_the_first_key_so_a_model_reads_it_first():
    out = server._notice_onto_text(json.dumps({"z": 1}))
    assert next(iter(json.loads(out))) == "_deprecated"


def test_prose_result_still_gets_the_prefix():
    out = server._notice_onto_text("Warning: no annotations registered.")
    assert out.startswith(server._DRAWING_MOVED)
    assert out.endswith("Warning: no annotations registered.")


def test_malformed_json_falls_back_to_the_prefix():
    """A truncated or invalid payload must not raise inside the wrapper."""
    out = server._notice_onto_text('{"unclosed": ')
    assert out.startswith(server._DRAWING_MOVED)


def test_json_array_is_not_given_a_key_it_cannot_hold():
    out = server._notice_onto_text("[1, 2, 3]")
    assert out.startswith(server._DRAWING_MOVED)


@pytest.mark.parametrize("name", ["inspect_drawing", "lint_drawing", "suggest_view_layout"])
def test_json_returning_tools_stay_parseable_end_to_end(name):
    from build123d_mcp.worker import InProcessSession

    session = InProcessSession(exec_timeout=60)
    server.configure(session)
    session.execute("from build123d import *")
    session.execute("show(Box(60, 40, 10), 'b')")
    calls = {
        "inspect_drawing": lambda f: f(objects="b"),
        "lint_drawing": lambda f: f(),
        "suggest_view_layout": lambda f: f(object_name="b"),
    }
    parsed = json.loads(calls[name](getattr(server, name)))
    assert "_deprecated" in parsed
    assert len(parsed) > 1, "the payload must survive alongside the notice"


def test_no_drawing_tool_is_async():
    """The wrapper is synchronous; an async tool would return a coroutine and
    the notice would be silently dropped."""
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in _DRAWING_TOOLS:
        assert not tools[name].is_async, name
