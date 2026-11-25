"""install_skill — write a b123d workflow skill to a project's agent config.

Four skills are available:
  drawing   → b123d-drawing  (multi-view engineering drawings from geometry; deprecated)
  modeling  → b123d-modeling (build 3D parts/assemblies via the MCP session)
  edit      → b123d-edit     (modify existing build123d code and verify deltas)
  repair    → b123d-repair   (repair a solid that fails the validity gate)

Supports two Agent Skills spec-compliant targets:
  claude     → .claude/skills/<skill-dir>/SKILL.md  (Claude Code; default)
  dot-agents → .agents/skills/<skill-dir>/SKILL.md
               (Codex CLI, Antigravity, OpenCode, Cursor, GitHub Copilot, Cline)
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

TARGETS = ("claude", "dot-agents")

_RETIRED_TARGETS = ("agents-md", "cursor", "windsurf")

_LEGACY_CLEANUP_HINT = (
    "If AGENTS.md or .windsurfrules contains "
    "<!-- b123d-...:start --> ... <!-- b123d-...:end --> "
    "sections from an earlier install, remove them."
)

_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---(?:\r?\n)+", re.DOTALL)

_TARGET_ROOTS: dict[str, tuple[str, ...]] = {
    "claude": (".claude", "skills"),
    "dot-agents": (".agents", "skills"),
}

_TARGET_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "dot-agents": ".agents/skills",
}

SKILLS = {
    "drawing": {"dir": "b123d-drawing"},
    "modeling": {"dir": "b123d-modeling"},
    "edit": {"dir": "b123d-edit"},
    "repair": {"dir": "b123d-repair"},
}


def _load_raw(skill: str = "modeling") -> str:
    return (files("build123d_mcp") / "skills" / SKILLS[skill]["dir"] / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _strip_frontmatter(text: str) -> str:
    """Remove leading YAML frontmatter (`--- ... ---`) if present."""
    return _FRONTMATTER_RE.sub("", text, count=1)


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


def _dest_file(target: str, base: Path, skill: str) -> Path:
    root = _TARGET_ROOTS[target]
    return base.joinpath(*root, SKILLS[skill]["dir"], "SKILL.md")


def _dest_exists(target: str, cwd: Path | None = None, skill: str = "modeling") -> bool:
    """Return True if *skill* is already installed for *target*."""
    if target not in _TARGET_ROOTS or skill not in SKILLS:
        return False
    return _dest_file(target, cwd or Path.cwd(), skill).exists()


# Prepended to the drawing skill's status line. Phase 1 of #465 announces the
# move without withdrawing anything — a workflow that works today keeps working,
# and Phase 2 (when the tools leave the default surface) is where installing it
# stops making sense.
DRAWING_DEPRECATED = (
    "NOTE — the drawing skill and the tools it drives are DEPRECATED (#465). "
    "Engineering-drawing generation has moved to draftwright "
    "(https://github.com/pzfreo/draftwright), which owns it and publishes its own skill; "
    "it is importable inside execute(), so a drawing is built from live session geometry "
    "without exporting first. These tools are off by default from 0.4.0 and removed in "
    "0.5.0.\n"
)


def install_skill(
    target: str = "claude",
    force: bool = False,
    cwd: Path | None = None,
    skill: str = "modeling",
) -> str:
    """Install a b123d skill into the current project for *target*.

    Returns a human-readable status string. The default is target="claude"
    and skill="modeling"; "drawing" still installs but its status line carries
    the deprecation notice (#465).
    """
    if target in _RETIRED_TARGETS:
        return (
            f"Target '{target}' has been retired in favor of 'dot-agents' (.agents/skills/). "
            f"Supported targets: {', '.join(TARGETS)}. {_LEGACY_CLEANUP_HINT}"
        )
    if target not in TARGETS:
        return f"Unknown target '{target}'. Supported targets: {', '.join(TARGETS)}"
    if skill not in SKILLS:
        return f"Unknown skill '{skill}'. Supported skills: {', '.join(SKILLS)}"
    if skill == "drawing":
        return DRAWING_DEPRECATED + _install_skill(target, force, cwd, skill)
    return _install_skill(target, force, cwd, skill)


def _install_skill(target: str, force: bool, cwd: Path | None, skill: str) -> str:
    base = cwd or Path.cwd()
    skill_dir = SKILLS[skill]["dir"]
    raw = _load_raw(skill)
    content = raw if target == "claude" else _strip_claude_markers(raw)

    dest_file = _dest_file(target, base, skill)
    if dest_file.exists() and not force:
        return f"Already installed at {dest_file} — use force=True to overwrite."
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(content, encoding="utf-8")
    msg = f"Installed {skill_dir} skill for {_TARGET_LABELS[target]} → {dest_file}"
    if target == "dot-agents":
        msg += f"\n{_LEGACY_CLEANUP_HINT}"
    return msg
