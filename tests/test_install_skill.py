"""Tests for the install_skill tool (all four targets)."""

from pathlib import Path

from build123d_mcp.tools.install_skill import (
    _END,
    _START,
    TARGETS,
    _dest_exists,
    _load_raw,
    _strip_claude_markers,
    install_skill,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _load_raw
# ---------------------------------------------------------------------------


def test_load_raw_returns_skill_content():
    content = _load_raw()
    assert "Engineering Drawing" in content
    assert len(content) > 1000


def test_load_raw_modeling_returns_skill_content():
    content = _load_raw("modeling")
    assert "Build 3D Geometry" in content
    assert len(content) > 1000


# ---------------------------------------------------------------------------
# _strip_claude_markers
# ---------------------------------------------------------------------------


def test_strip_send_marker():
    out = _strip_claude_markers("See [SEND: /tmp/foo.png] for the result.")
    assert "[SEND:" not in out
    assert "/tmp/foo.png" in out


def test_strip_ask_marker_with_options():
    out = _strip_claude_markers("[ASK: Which size? | A4 | A3]")
    assert "[ASK:" not in out
    assert "Which size?" in out
    assert "A4" in out


def test_strip_ask_marker_no_options():
    out = _strip_claude_markers("[ASK: Ready to proceed?]")
    assert "[ASK:" not in out
    assert "Ready to proceed?" in out


# ---------------------------------------------------------------------------
# target: claude
# ---------------------------------------------------------------------------


def test_install_claude(tmp_path):
    result = install_skill(target="claude", cwd=tmp_path)
    dest = tmp_path / ".claude" / "skills" / "b123d-drawing" / "SKILL.md"
    assert dest.exists()
    assert "Engineering Drawing" in _read(dest)
    assert "Installed" in result


def test_install_claude_no_overwrite(tmp_path):
    install_skill(target="claude", cwd=tmp_path)
    result = install_skill(target="claude", cwd=tmp_path)
    assert "already" in result.lower()


def test_install_claude_force(tmp_path):
    install_skill(target="claude", cwd=tmp_path)
    result = install_skill(target="claude", force=True, cwd=tmp_path)
    assert "Installed" in result


def test_install_claude_preserves_claude_markers(tmp_path):
    install_skill(target="claude", cwd=tmp_path)
    dest = tmp_path / ".claude" / "skills" / "b123d-drawing" / "SKILL.md"
    # Claude target keeps [SEND:] / [ASK:] markers intact
    raw = _load_raw()
    assert _read(dest) == raw


# ---------------------------------------------------------------------------
# target: agents-md
# ---------------------------------------------------------------------------


def test_install_agents_md_creates_file(tmp_path):
    result = install_skill(target="agents-md", cwd=tmp_path)
    dest = tmp_path / "AGENTS.md"
    assert dest.exists()
    assert _START in _read(dest)
    assert _END in _read(dest)
    assert "[SEND:" not in _read(dest)
    assert "Installed" in result


def test_install_agents_md_appends_to_existing(tmp_path):
    existing = "# My project\n\nSome existing instructions.\n"
    (tmp_path / "AGENTS.md").write_text(existing, encoding="utf-8")
    install_skill(target="agents-md", cwd=tmp_path)
    content = _read(tmp_path / "AGENTS.md")
    assert "Some existing instructions." in content
    assert _START in content


def test_install_agents_md_no_overwrite(tmp_path):
    install_skill(target="agents-md", cwd=tmp_path)
    result = install_skill(target="agents-md", cwd=tmp_path)
    assert "already" in result.lower()


def test_install_agents_md_force_replaces_section(tmp_path):
    install_skill(target="agents-md", cwd=tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        _read(tmp_path / "AGENTS.md").replace("Engineering Drawing", "OLD CONTENT"),
        encoding="utf-8",
    )
    install_skill(target="agents-md", force=True, cwd=tmp_path)
    content = _read(tmp_path / "AGENTS.md")
    assert "OLD CONTENT" not in content
    assert "Engineering Drawing" in content
    # Only one copy of the section
    assert content.count(_START) == 1


# ---------------------------------------------------------------------------
# target: cursor
# ---------------------------------------------------------------------------


def test_install_cursor_creates_mdc(tmp_path):
    result = install_skill(target="cursor", cwd=tmp_path)
    dest = tmp_path / ".cursor" / "rules" / "b123d-drawing.mdc"
    assert dest.exists()
    content = _read(dest)
    assert "alwaysApply: false" in content
    assert "description:" in content
    assert "Engineering Drawing" in content
    assert "[SEND:" not in content
    assert "Installed" in result
    # globs must be a quoted string, not a YAML block list
    assert 'globs: "' in content
    assert "  - " not in content.split("---")[1]  # no YAML list items in frontmatter


def test_install_cursor_no_overwrite(tmp_path):
    install_skill(target="cursor", cwd=tmp_path)
    result = install_skill(target="cursor", cwd=tmp_path)
    assert "already" in result.lower()


def test_install_cursor_force(tmp_path):
    install_skill(target="cursor", cwd=tmp_path)
    result = install_skill(target="cursor", force=True, cwd=tmp_path)
    assert "Installed" in result


# ---------------------------------------------------------------------------
# target: windsurf
# ---------------------------------------------------------------------------


def test_install_windsurf_creates_file(tmp_path):
    result = install_skill(target="windsurf", cwd=tmp_path)
    dest = tmp_path / ".windsurfrules"
    assert dest.exists()
    content = _read(dest)
    assert _START in content
    assert "[SEND:" not in content
    assert "Installed" in result


def test_install_windsurf_appends_to_existing(tmp_path):
    existing = "# existing windsurf rules\n\nUse tabs.\n"
    (tmp_path / ".windsurfrules").write_text(existing, encoding="utf-8")
    install_skill(target="windsurf", cwd=tmp_path)
    content = _read(tmp_path / ".windsurfrules")
    assert "Use tabs." in content
    assert _START in content


def test_install_windsurf_force_replaces_section(tmp_path):
    install_skill(target="windsurf", cwd=tmp_path)
    install_skill(target="windsurf", force=True, cwd=tmp_path)
    content = _read(tmp_path / ".windsurfrules")
    assert content.count(_START) == 1


# ---------------------------------------------------------------------------
# _dest_exists
# ---------------------------------------------------------------------------


def test_dest_exists_false_before_install(tmp_path):
    for target in TARGETS:
        assert not _dest_exists(target, cwd=tmp_path)


def test_dest_exists_true_after_install(tmp_path):
    for target in TARGETS:
        install_skill(target=target, cwd=tmp_path)
        assert _dest_exists(target, cwd=tmp_path)


def test_dest_exists_unknown_target():
    assert not _dest_exists("unknown")


# ---------------------------------------------------------------------------
# skill: modeling
# ---------------------------------------------------------------------------


def test_install_claude_modeling(tmp_path):
    result = install_skill(target="claude", cwd=tmp_path, skill="modeling")
    dest = tmp_path / ".claude" / "skills" / "b123d-modeling" / "SKILL.md"
    assert dest.exists()
    assert "Build 3D Geometry" in _read(dest)
    assert "Installed" in result


def test_agents_md_both_skills_coexist(tmp_path):
    install_skill(target="agents-md", cwd=tmp_path, skill="drawing")
    install_skill(target="agents-md", cwd=tmp_path, skill="modeling")
    content = _read(tmp_path / "AGENTS.md")
    assert "<!-- b123d-drawing:start -->" in content
    assert "<!-- b123d-modeling:start -->" in content
    assert "Engineering Drawing" in content
    assert "Build 3D Geometry" in content


def test_agents_md_modeling_force_only_replaces_own_section(tmp_path):
    install_skill(target="agents-md", cwd=tmp_path, skill="drawing")
    install_skill(target="agents-md", cwd=tmp_path, skill="modeling")
    install_skill(target="agents-md", cwd=tmp_path, skill="modeling", force=True)
    content = _read(tmp_path / "AGENTS.md")
    assert content.count("<!-- b123d-drawing:start -->") == 1
    assert content.count("<!-- b123d-modeling:start -->") == 1
    assert "Engineering Drawing" in content


def test_install_cursor_modeling_description_routed(tmp_path):
    install_skill(target="cursor", cwd=tmp_path, skill="modeling")
    content = _read(tmp_path / ".cursor" / "rules" / "b123d-modeling.mdc")
    assert "description:" in content
    assert "alwaysApply: false" in content
    # modeling has no path affinity — no globs line at all
    assert "globs" not in content.split("---")[1]


def test_dest_exists_tracks_skills_independently(tmp_path):
    install_skill(target="claude", cwd=tmp_path, skill="drawing")
    assert _dest_exists("claude", cwd=tmp_path, skill="drawing")
    assert not _dest_exists("claude", cwd=tmp_path, skill="modeling")


def test_unknown_skill_returns_error():
    result = install_skill(skill="welding")
    assert "Unknown skill" in result
    assert "welding" in result


# ---------------------------------------------------------------------------
# server instructions + skill resources
# ---------------------------------------------------------------------------


def test_server_instructions_registered():
    # The instructions field is what stays visible when MCP clients defer tool
    # schemas behind tool search — it must exist and fit the 2KB client cap.
    from build123d_mcp.server import _INSTRUCTIONS, mcp

    assert mcp.instructions == _INSTRUCTIONS
    assert 0 < len(_INSTRUCTIONS.encode("utf-8")) <= 2048
    for needle in ("execute()", "measure()", "skill/modeling", "skill/drawing"):
        assert needle in _INSTRUCTIONS


def test_modeling_skill_resource_registered():
    import asyncio

    from build123d_mcp.server import mcp

    uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    assert "build123d://skill/modeling" in uris
    assert "build123d://skill/drawing" in uris


# ---------------------------------------------------------------------------
# unknown target
# ---------------------------------------------------------------------------


def test_unknown_target_returns_error():
    result = install_skill(target="unknown-agent")
    assert "Unknown target" in result
    assert "unknown-agent" in result
