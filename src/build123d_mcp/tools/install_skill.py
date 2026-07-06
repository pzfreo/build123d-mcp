"""install_skill — write a b123d workflow skill to a project's agent config.

Three skills are available:
  drawing   → b123d-drawing  (multi-view engineering drawings from geometry)
  modeling  → b123d-modeling (build 3D parts/assemblies via the MCP session)
  repair    → b123d-repair   (heal a solid that fails the validity gate)

Supports four targets:
  claude     → .claude/skills/<skill-dir>/SKILL.md
  agents-md  → AGENTS.md  (Codex CLI, Antigravity, GitHub Copilot, Cline)
  cursor     → .cursor/rules/<skill-dir>.mdc
  windsurf   → .windsurfrules
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

TARGETS = ("claude", "agents-md", "cursor", "windsurf")

SKILLS = {
    "drawing": {
        "dir": "b123d-drawing",
        "cursor_description": (
            "Engineering drawing workflow for build123d geometry using the build123d-mcp MCP server"
        ),
        "cursor_globs": "scripts/drawings/**, scripts/*_drawing.py, drawings/**",
    },
    "modeling": {
        "dir": "b123d-modeling",
        "cursor_description": (
            "3D CAD modeling workflow: build parts and assemblies with build123d "
            "via the build123d-mcp MCP server"
        ),
        # No path affinity — the rule is routed by description (agent-requested).
        "cursor_globs": "",
    },
    "repair": {
        "dir": "b123d-repair",
        "cursor_description": (
            "CAD geometry repair workflow: heal a solid that fails the validity "
            "gate (BRepCheck / open edges / non-manifold / mesh defects) with the "
            "build123d-mcp MCP server"
        ),
        # No path affinity — the rule is routed by description (agent-requested).
        "cursor_globs": "",
    },
}

# Delimiters used when appending to shared files (AGENTS.md, .windsurfrules).
# Kept as drawing-skill constants for back-compat with already-installed files.
_START = "<!-- b123d-drawing:start -->"
_END = "<!-- b123d-drawing:end -->"


def _markers(skill: str) -> tuple[str, str]:
    d = SKILLS[skill]["dir"]
    return f"<!-- {d}:start -->", f"<!-- {d}:end -->"


def _load_raw(skill: str = "drawing") -> str:
    return (files("build123d_mcp") / "skills" / SKILLS[skill]["dir"] / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _strip_claude_markers(text: str) -> str:
    """Remove Claude Code-specific inline markers that other agents don't understand."""
    # [SEND: /tmp/foo.png]  →  show the rendered image at /tmp/foo.png to the user
    text = re.sub(
        r"\[SEND:\s*([^\]]+)\]",
        r"(show the rendered image at \1 to the user)",
        text,
    )

    # [ASK: question | opt1 | opt2]  →  ask the user: question (opt1 / opt2)
    def _ask_replace(m: re.Match) -> str:
        parts = [p.strip() for p in m.group(1).split("|")]
        return (
            f"(ask the user: {parts[0]}"
            + (f" — options: {', '.join(parts[1:])}" if len(parts) > 1 else "")
            + ")"
        )

    text = re.sub(r"\[ASK:\s*([^\]]+)\]", _ask_replace, text)
    return text


def _cursor_frontmatter(skill: str) -> str:
    spec = SKILLS[skill]
    globs_line = f'globs: "{spec["cursor_globs"]}"\n' if spec["cursor_globs"] else ""
    return (
        f"---\ndescription: {spec['cursor_description']}\n{globs_line}alwaysApply: false\n---\n\n"
    )


def _wrap_section(content: str, skill: str) -> str:
    """Wrap content in delimiters for safe replacement in shared files."""
    start, end = _markers(skill)
    return f"{start}\n{content.strip()}\n{end}\n"


def _replace_or_append(existing: str, new_section: str, skill: str) -> str:
    """Replace this skill's delimited section if present, else append."""
    start, end = _markers(skill)
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    if pattern.search(existing):
        return pattern.sub(new_section.rstrip(), existing, count=1)
    return existing.rstrip() + "\n\n" + new_section


def _dest_exists(target: str, cwd: Path | None = None, skill: str = "drawing") -> bool:
    """Return True if *skill* is already installed for *target*.

    NOTE: the file-path logic here must stay in sync with install_skill() below.
    If you change a target's destination path, update both functions.
    """
    base = cwd or Path.cwd()
    skill_dir = SKILLS[skill]["dir"]
    start, _ = _markers(skill)
    if target == "claude":
        return (base / ".claude" / "skills" / skill_dir / "SKILL.md").exists()
    if target == "agents-md":
        f = base / "AGENTS.md"
        return f.exists() and start in f.read_text(encoding="utf-8")
    if target == "cursor":
        return (base / ".cursor" / "rules" / f"{skill_dir}.mdc").exists()
    if target == "windsurf":
        f = base / ".windsurfrules"
        return f.exists() and start in f.read_text(encoding="utf-8")
    return False


def install_skill(
    target: str = "claude",
    force: bool = False,
    cwd: Path | None = None,
    skill: str = "drawing",
) -> str:
    """Install a b123d skill into the current project for *target*.

    Returns a human-readable status string.
    """
    if target not in TARGETS:
        return f"Unknown target '{target}'. Supported targets: {', '.join(TARGETS)}"
    if skill not in SKILLS:
        return f"Unknown skill '{skill}'. Supported skills: {', '.join(SKILLS)}"

    base = cwd or Path.cwd()
    skill_dir = SKILLS[skill]["dir"]
    start, _ = _markers(skill)
    raw = _load_raw(skill)
    adapted = _strip_claude_markers(raw)

    if target == "claude":
        dest_dir = base / ".claude" / "skills" / skill_dir
        dest_file = dest_dir / "SKILL.md"
        if dest_file.exists() and not force:
            return f"Already installed at {dest_file} — use force=True to overwrite."
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(raw, encoding="utf-8")
        return f"Installed {skill_dir} skill for Claude Code → {dest_file}"

    if target == "agents-md":
        dest_file = base / "AGENTS.md"
        section = _wrap_section(adapted, skill)
        if dest_file.exists():
            existing = dest_file.read_text(encoding="utf-8")
            if start in existing and not force:
                return f"{skill_dir} section already present in {dest_file} — use force=True to overwrite."
            dest_file.write_text(_replace_or_append(existing, section, skill), encoding="utf-8")
        else:
            dest_file.write_text(section, encoding="utf-8")
        return (
            f"Installed {skill_dir} skill into {dest_file} (Codex / Antigravity / Copilot / Cline)"
        )

    if target == "cursor":
        dest_file = base / ".cursor" / "rules" / f"{skill_dir}.mdc"
        if dest_file.exists() and not force:
            return f"Already installed at {dest_file} — use force=True to overwrite."
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(_cursor_frontmatter(skill) + adapted, encoding="utf-8")
        return f"Installed {skill_dir} rule for Cursor → {dest_file}"

    if target == "windsurf":
        dest_file = base / ".windsurfrules"
        section = _wrap_section(adapted, skill)
        if dest_file.exists():
            existing = dest_file.read_text(encoding="utf-8")
            if start in existing and not force:
                return f"{skill_dir} section already present in {dest_file} — use force=True to overwrite."
            dest_file.write_text(_replace_or_append(existing, section, skill), encoding="utf-8")
        else:
            dest_file.write_text(section, encoding="utf-8")
        return f"Installed {skill_dir} skill into {dest_file} (Windsurf)"

    return "Unreachable"  # mypy
