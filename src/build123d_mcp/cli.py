"""Command-line entry point for the build123d MCP server.

Parses CLI arguments / environment variables, wires up the ``WorkerSession``,
and starts the FastMCP server defined in ``server.py``. Kept separate so
``server.py`` stays focused on tool/resource/prompt registration.
"""


def _cmd_install_skill(argv: list) -> None:
    import argparse
    import sys

    from build123d_mcp.tools.install_skill import TARGETS
    from build123d_mcp.tools.install_skill import install_skill as _install

    p = argparse.ArgumentParser(
        prog="build123d-mcp install-skill",
        description="Copy the b123d-drawing skill into the current project for the specified agent.",
    )
    p.add_argument(
        "--target",
        choices=TARGETS,
        default="claude",
        help="Agent to install for (default: claude)",
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing installation")
    args = p.parse_args(argv)

    from build123d_mcp.tools.install_skill import _dest_exists

    if not args.force and _dest_exists(args.target):
        print(
            f"Skill already installed for '{args.target}' — use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(_install(target=args.target, force=args.force))


def main():
    import argparse
    import os
    import sys
    from importlib.metadata import version

    from build123d_mcp import server
    from build123d_mcp.worker import WorkerSession

    if len(sys.argv) > 1 and sys.argv[1] == "install-skill":
        _cmd_install_skill(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="build123d-mcp",
        description="MCP server for interactive 3D CAD via build123d. Communicates over stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Subcommands:
  install-skill     Copy the b123d-drawing Claude Code skill into .claude/skills/ of the current project
                    Usage: build123d-mcp install-skill [--force]

MCP client configuration example:
  {
    "mcpServers": {
      "build123d": {
        "command": "uv",
        "args": ["tool", "run", "--python", "3.12", "build123d-mcp", "--library", "/path/to/parts"]
      }
    }
  }

Tools: discovered by the MCP client over the protocol (the authoritative list).
  Call the workflow_hints tool for guidance on which to use.

Part library file format (Python, any .py file under --library path):
  PART_INFO = {
      "description": "Short description",
      "tags": ["tag1", "tag2"],
      "parameters": {
          "width": {"type": "float", "default": 10.0, "description": "width mm"},
      }
  }
  from build123d import *
  def make(width=10.0):
      return Box(width, width, width)
""",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('build123d-mcp')}"
    )
    parser.add_argument(
        "--library",
        metavar="PATH",
        default=os.environ.get("BUILD123D_PART_LIBRARY", ""),
        help="Path to part library directory (overrides BUILD123D_PART_LIBRARY env var)",
    )
    parser.add_argument(
        "--allow-all-imports",
        action="store_true",
        default=os.environ.get("BUILD123D_ALLOW_ALL_IMPORTS", "").lower() in ("1", "true", "yes"),
        help="Disable the import allowlist — any Python module can be imported. "
        "Use only in trusted environments. Overrides BUILD123D_ALLOW_ALL_IMPORTS env var.",
    )
    parser.add_argument(
        "--allow-imports",
        metavar="MODULES",
        default=os.environ.get("BUILD123D_ALLOW_IMPORTS", ""),
        help="Comma-separated extra modules added to the import allowlist on top of "
        "the defaults (e.g. --allow-imports scipy,pandas). Each entry permits the "
        "named module and all its submodules. Use this for CAD scripts that need "
        "extra packages without disabling the sandbox via --allow-all-imports. "
        "Overrides BUILD123D_ALLOW_IMPORTS env var.",
    )
    parser.add_argument(
        "--exec-timeout",
        metavar="SECONDS",
        type=int,
        default=int(os.environ.get("BUILD123D_EXEC_TIMEOUT", "120")),
        help="Execution time limit in seconds for user code (default: 120). "
        "Overrides BUILD123D_EXEC_TIMEOUT env var.",
    )
    args = parser.parse_args()

    if args.library and not os.path.isdir(args.library):
        parser.error(f"Library path is not a directory: {args.library}")

    extra_imports = tuple(m.strip() for m in args.allow_imports.split(",") if m.strip())

    if args.allow_all_imports or extra_imports:
        import build123d_mcp.security as _sec

        if args.allow_all_imports:
            _sec.ALLOW_ALL_IMPORTS = True
        if extra_imports:
            _sec.EXTRA_ALLOWED_IMPORTS.update(extra_imports)

    server.configure(
        WorkerSession(
            library_path=args.library,
            allow_all_imports=args.allow_all_imports,
            extra_allowed_imports=extra_imports,
            exec_timeout=args.exec_timeout,
        )
    )

    server.mcp.run()


if __name__ == "__main__":
    main()
