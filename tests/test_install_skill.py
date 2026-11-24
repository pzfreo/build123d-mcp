"""Tests for the install_skill tool (Agent Skills spec targets & frontmatter)."""

import inspect
import re
from pathlib import Path

import pytest

from build123d_mcp.tools.install_skill import (
    SKILLS,
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


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse leading YAML frontmatter (`---\\n...\\n---\\n`) into key/value pairs and body."""
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert m is not None, "SKILL.md must start with YAML frontmatter delimited by ---"
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep == ":", f"Malformed frontmatter line: {line!r}"
        fm[key.strip()] = value.strip()
    return fm, m.group(2)


# ---------------------------------------------------------------------------
# _load_raw & Agent Skills spec frontmatter
# ---------------------------------------------------------------------------


def test_load_raw_returns_skill_content():
    content = _load_raw("drawing")
    assert "Engineering Drawing" in content
    assert len(content) > 1000


def test_load_raw_modeling_returns_skill_content():
    content = _load_raw("modeling")
    assert "Build 3D Geometry" in content
    assert "base sketch/solid/shell or cored body" in content
    assert "Choose the body family before detailing" in content
    assert "build123d://skill/repair" in content
    assert "Fillets, chamfers and small detail cuts do not count as dominant-form" in content
    assert "correction; use them only after the main volume" in content
    assert "Thin-walled open cover / tray / housing" in content
    assert "Cored annular flange / circular housing" in content
    assert "Cast gearbox / pump housing with bosses and flanges" in content
    assert "Open spoked / ribbed / windowed casting" in content
    assert "it does not\n  recognize arbitrary spokes" in content
    assert "Axisymmetric stepped part" in content
    assert "Use canonical source only for evidenced global revisions" in content
    assert "Do not globally rebuild a checkpoint" in content
    assert 'execute_file(path="model.py"' in content
    assert "calibrated image evidence may resolve" in content
    assert "Never trace dimension/leader/hatch lines" in content
    assert len(content) > 1000


def test_modeling_skill_contains_dominant_form_correction_loop():
    content = _load_raw("modeling")
    start = content.index("### Dominant-form correction")
    end = content.index("## Step 4", start)
    section = content[start:end]

    assert "first valid render" in section
    assert "diagnostic checkpoint" in section
    assert "dominant body family" in section
    assert "body-class level" in section
    for phrase in (
        "Axisymmetric shell",
        "Repeated radial features",
        "Blind pocket vs through void",
        "Sheet metal",
        "Interface preservation",
    ):
        assert phrase in section
    assert "fixture" not in section.lower()
    assert "Structural validity is not drawing-fidelity evidence" in section


def test_load_raw_edit_returns_skill_content():
    content = _load_raw("edit")
    assert "Edit Existing build123d Geometry" in content
    assert len(content) > 1000


def test_load_raw_repair_returns_skill_content():
    content = _load_raw("repair")
    assert "Repair Invalid Geometry" in content
    assert len(content) > 1000


@pytest.mark.parametrize("skill", tuple(SKILLS))
def test_shipped_skills_have_valid_agent_skills_frontmatter(skill):
    """Every SKILL.md must conform to https://agentskills.io/specification."""
    raw = _load_raw(skill)
    fm, body = _parse_frontmatter(raw)

    expected_dir = SKILLS[skill]["dir"]
    name = fm.get("name", "")
    description = fm.get("description", "")

    assert name == expected_dir, f"name {name!r} must match parent dir {expected_dir!r}"
    assert 1 <= len(name) <= 64
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), f"invalid skill name: {name!r}"

    assert 1 <= len(description) <= 1024
    # Avoid unquoted YAML mapping syntax inside scalar descriptions.
    assert ": " not in description, f"unquoted ': ' in description breaks YAML: {description!r}"
    assert body.lstrip().startswith("# ")


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


def test_shipped_skills_have_no_personal_markers():
    """[SEND:]/[ASK:] are conventions of the maintainer's own client setup —
    the claude install target ships SKILL.md verbatim, so the source files
    must spell the instructions out in plain English instead."""
    for skill in SKILLS:
        raw = _load_raw(skill)
        assert "[SEND:" not in raw, f"{skill} skill contains a [SEND:] marker"
        assert "[ASK:" not in raw, f"{skill} skill contains an [ASK:] marker"


# ---------------------------------------------------------------------------
# target: dot-agents (default)
# ---------------------------------------------------------------------------


def test_install_defaults_to_dot_agents_modeling(tmp_path):
    result = install_skill(cwd=tmp_path)
    dest = tmp_path / ".agents" / "skills" / "b123d-modeling" / "SKILL.md"
    assert dest.exists()
    content = _read(dest)
    fm, _ = _parse_frontmatter(content)
    assert fm["name"] == "b123d-modeling"
    assert "Build 3D Geometry" in content
    assert "Installed" in result


@pytest.mark.parametrize(
    ("skill", "skill_dir", "heading"),
    [
        ("modeling", "b123d-modeling", "Build 3D Geometry"),
        ("edit", "b123d-edit", "Edit Existing build123d Geometry"),
        ("repair", "b123d-repair", "Repair Invalid Geometry"),
        ("drawing", "b123d-drawing", "Engineering Drawing"),
    ],
)
def test_install_dot_agents_all_skills(tmp_path, skill, skill_dir, heading):
    result = install_skill(target="dot-agents", cwd=tmp_path, skill=skill)
    dest = tmp_path / ".agents" / "skills" / skill_dir / "SKILL.md"
    assert dest.exists()
    content = _read(dest)
    fm, _ = _parse_frontmatter(content)
    assert fm["name"] == skill_dir
    assert heading in content
    assert "[SEND:" not in content
    assert "Installed" in result


def test_install_dot_agents_no_overwrite(tmp_path):
    install_skill(target="dot-agents", cwd=tmp_path, skill="modeling")
    result = install_skill(target="dot-agents", cwd=tmp_path, skill="modeling")
    assert "already" in result.lower()


def test_install_dot_agents_force(tmp_path):
    install_skill(target="dot-agents", cwd=tmp_path, skill="modeling")
    result = install_skill(target="dot-agents", force=True, cwd=tmp_path, skill="modeling")
    assert "Installed" in result


# ---------------------------------------------------------------------------
# target: claude
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("skill", "skill_dir", "heading"),
    [
        ("modeling", "b123d-modeling", "Build 3D Geometry"),
        ("edit", "b123d-edit", "Edit Existing build123d Geometry"),
        ("repair", "b123d-repair", "Repair Invalid Geometry"),
        ("drawing", "b123d-drawing", "Engineering Drawing"),
    ],
)
def test_install_claude_all_skills(tmp_path, skill, skill_dir, heading):
    result = install_skill(target="claude", cwd=tmp_path, skill=skill)
    dest = tmp_path / ".claude" / "skills" / skill_dir / "SKILL.md"
    assert dest.exists()
    content = _read(dest)
    fm, _ = _parse_frontmatter(content)
    assert fm["name"] == skill_dir
    assert heading in content
    assert "Installed" in result


def test_install_claude_no_overwrite(tmp_path):
    install_skill(target="claude", cwd=tmp_path, skill="drawing")
    result = install_skill(target="claude", cwd=tmp_path, skill="drawing")
    assert "already" in result.lower()


def test_install_claude_force(tmp_path):
    install_skill(target="claude", cwd=tmp_path, skill="drawing")
    result = install_skill(target="claude", force=True, cwd=tmp_path, skill="drawing")
    assert "Installed" in result


def test_install_claude_preserves_raw_content(tmp_path):
    install_skill(target="claude", cwd=tmp_path, skill="drawing")
    dest = tmp_path / ".claude" / "skills" / "b123d-drawing" / "SKILL.md"
    raw = _load_raw("drawing")
    assert _read(dest) == raw


# ---------------------------------------------------------------------------
# retired & unknown targets / skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", ("agents-md", "cursor", "windsurf"))
def test_retired_targets_point_to_dot_agents(tmp_path, retired):
    result = install_skill(target=retired, cwd=tmp_path, skill="modeling")
    assert "retired" in result.lower()
    assert "dot-agents" in result
    assert list(tmp_path.iterdir()) == []


def test_unknown_target_returns_error():
    result = install_skill(target="unknown-agent", skill="drawing")
    assert "Unknown target" in result
    assert "unknown-agent" in result


def test_unknown_skill_returns_error():
    result = install_skill(skill="welding")
    assert "Unknown skill" in result
    assert "welding" in result


# ---------------------------------------------------------------------------
# _dest_exists
# ---------------------------------------------------------------------------


def test_dest_exists_false_before_install(tmp_path):
    for target in TARGETS:
        assert not _dest_exists(target, cwd=tmp_path)


def test_dest_exists_true_after_install(tmp_path):
    for target in TARGETS:
        install_skill(target=target, cwd=tmp_path, skill="modeling")
        assert _dest_exists(target, cwd=tmp_path, skill="modeling")


def test_dest_exists_unknown_target():
    assert not _dest_exists("unknown")


def test_dest_exists_tracks_skills_independently(tmp_path):
    install_skill(target="dot-agents", cwd=tmp_path, skill="drawing")
    assert _dest_exists("dot-agents", cwd=tmp_path, skill="drawing")
    assert not _dest_exists("dot-agents", cwd=tmp_path, skill="modeling")
    assert not _dest_exists("dot-agents", cwd=tmp_path, skill="edit")
    assert not _dest_exists("dot-agents", cwd=tmp_path, skill="repair")


# ---------------------------------------------------------------------------
# server instructions, tool signature, + skill resources
# ---------------------------------------------------------------------------


def test_server_install_skill_defaults_match_tool():
    from build123d_mcp import server

    server_sig = inspect.signature(server.install_skill)
    tool_sig = inspect.signature(install_skill)
    assert server_sig.parameters["target"].default == "dot-agents"
    assert server_sig.parameters["target"].default == tool_sig.parameters["target"].default
    assert server_sig.parameters["skill"].default == tool_sig.parameters["skill"].default


def test_server_instructions_registered():
    # The instructions field is what stays visible when MCP clients defer tool
    # schemas behind tool search — it must exist and fit the 2KB client cap.
    from build123d_mcp.server import _INSTRUCTIONS, mcp

    assert mcp.instructions == _INSTRUCTIONS
    assert 0 < len(_INSTRUCTIONS.encode("utf-8")) <= 2048
    for needle in (
        "execute()",
        "measure()",
        "skill/modeling",
        "skill/edit",
        "skill/repair",
        # Drawings moved out to draftwright (#465); the instructions must say
        # where they went rather than silently dropping the capability.
        "draftwright",
    ):
        assert needle in _INSTRUCTIONS
    assert "skill/drawing" not in _INSTRUCTIONS


def test_modeling_skill_resource_registered():
    import asyncio

    from build123d_mcp.server import mcp

    uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    assert "build123d://skill/modeling" in uris
    assert "build123d://skill/edit" in uris
    assert "build123d://skill/drawing" in uris
    assert "build123d://skill/repair" in uris
