"""install_skill — write the b123d-drawing workflow to a project's agent config.

Supports four targets:
  claude     → .claude/skills/b123d-drawing/SKILL.md
  agents-md  → AGENTS.md  (Codex CLI, Antigravity, GitHub Copilot, Cline)
  cursor     → .cursor/rules/b123d-drawing.mdc
  windsurf   → .windsurfrules
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

TARGETS = ("claude", "agents-md", "cursor", "windsurf")

# Delimiters used when appending to shared files (AGENTS.md, .windsurfrules)
_START = "<!-- b123d-drawing:start -->"
_END = "<!-- b123d-drawing:end -->"


def _load_raw() -> str:
    return (files("build123d_mcp") / "skills" / "b123d-drawing" / "SKILL.md").read_text(
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


def _cursor_frontmatter() -> str:
    return (
        "---\n"
        "description: Engineering drawing workflow for build123d geometry using the build123d-mcp MCP server\n"
        'globs: "scripts/drawings/**, scripts/*_drawing.py, drawings/**"\n'
        "alwaysApply: false\n"
        "---\n\n"
    )


def _wrap_section(content: str) -> str:
    """Wrap content in delimiters for safe replacement in shared files."""
    return f"{_START}\n{content.strip()}\n{_END}\n"


def _replace_or_append(existing: str, new_section: str) -> str:
    """Replace delimited section if present, else append."""
    pattern = re.compile(
        re.escape(_START) + r".*?" + re.escape(_END),
        re.DOTALL,
    )
    if pattern.search(existing):
        return pattern.sub(new_section.rstrip(), existing, count=1)
    return existing.rstrip() + "\n\n" + new_section


def _dest_exists(target: str, cwd: Path | None = None) -> bool:
    """Return True if the skill is already installed for *target*.

    NOTE: the file-path logic here must stay in sync with install_skill() below.
    If you change a target's destination path, update both functions.
    """
    base = cwd or Path.cwd()
    if target == "claude":
        return (base / ".claude" / "skills" / "b123d-drawing" / "SKILL.md").exists()
    if target == "agents-md":
        f = base / "AGENTS.md"
        return f.exists() and _START in f.read_text(encoding="utf-8")
    if target == "cursor":
        return (base / ".cursor" / "rules" / "b123d-drawing.mdc").exists()
    if target == "windsurf":
        f = base / ".windsurfrules"
        return f.exists() and _START in f.read_text(encoding="utf-8")
    return False


def install_skill(target: str = "claude", force: bool = False, cwd: Path | None = None) -> str:
    """Install the b123d-drawing skill into the current project for *target*.

    Returns a human-readable status string.
    """
    if target not in TARGETS:
        return f"Unknown target '{target}'. Supported targets: {', '.join(TARGETS)}"

    base = cwd or Path.cwd()
    raw = _load_raw()
    adapted = _strip_claude_markers(raw)

    if target == "claude":
        dest_dir = base / ".claude" / "skills" / "b123d-drawing"
        dest_file = dest_dir / "SKILL.md"
        if dest_file.exists() and not force:
            return f"Already installed at {dest_file} — use force=True to overwrite."
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(raw, encoding="utf-8")
        return f"Installed b123d-drawing skill for Claude Code → {dest_file}"

    if target == "agents-md":
        dest_file = base / "AGENTS.md"
        section = _wrap_section(adapted)
        if dest_file.exists():
            existing = dest_file.read_text(encoding="utf-8")
            if _START in existing and not force:
                return f"b123d-drawing section already present in {dest_file} — use force=True to overwrite."
            dest_file.write_text(_replace_or_append(existing, section), encoding="utf-8")
        else:
            dest_file.write_text(section, encoding="utf-8")
        return f"Installed b123d-drawing skill into {dest_file} (Codex / Antigravity / Copilot / Cline)"

    if target == "cursor":
        dest_file = base / ".cursor" / "rules" / "b123d-drawing.mdc"
        if dest_file.exists() and not force:
            return f"Already installed at {dest_file} — use force=True to overwrite."
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(_cursor_frontmatter() + adapted, encoding="utf-8")
        return f"Installed b123d-drawing rule for Cursor → {dest_file}"

    if target == "windsurf":
        dest_file = base / ".windsurfrules"
        section = _wrap_section(adapted)
        if dest_file.exists():
            existing = dest_file.read_text(encoding="utf-8")
            if _START in existing and not force:
                return f"b123d-drawing section already present in {dest_file} — use force=True to overwrite."
            dest_file.write_text(_replace_or_append(existing, section), encoding="utf-8")
        else:
            dest_file.write_text(section, encoding="utf-8")
        return f"Installed b123d-drawing skill into {dest_file} (Windsurf)"

    return "Unreachable"  # mypy
