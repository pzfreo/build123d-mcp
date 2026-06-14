"""Command-line entry point for the build123d MCP server.

Parses CLI arguments / environment variables, wires up the ``WorkerSession``,
and starts the FastMCP server defined in ``server.py``. Kept separate so
``server.py`` stays focused on tool/resource/prompt registration.
"""


def _cmd_install_skill(argv: list) -> None:
    import argparse
    import sys

    from build123d_mcp.tools.install_skill import SKILLS, TARGETS
    from build123d_mcp.tools.install_skill import install_skill as _install

    p = argparse.ArgumentParser(
        prog="build123d-mcp install-skill",
        description="Copy a b123d workflow skill into the current project for the specified agent.",
    )
    p.add_argument(
        "--target",
        choices=TARGETS,
        default="claude",
        help="Agent to install for (default: claude)",
    )
    p.add_argument(
        "--skill",
        choices=tuple(SKILLS),
        default="drawing",
        help="Workflow to install: drawing or modeling (default: drawing)",
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing installation")
    args = p.parse_args(argv)

    from build123d_mcp.tools.install_skill import _dest_exists

    if not args.force and _dest_exists(args.target, skill=args.skill):
        print(
            f"Skill already installed for '{args.target}' — use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(_install(target=args.target, force=args.force, skill=args.skill))


def main():
    import argparse
    import os
    import sys
    from importlib.metadata import version

    from build123d_mcp import server
    from build123d_mcp.worker import InProcessSession, WorkerSession

    if len(sys.argv) > 1 and sys.argv[1] == "install-skill":
        _cmd_install_skill(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="build123d-mcp",
        description="MCP server for interactive 3D CAD via build123d. Communicates over stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Subcommands:
  install-skill     Copy a b123d workflow skill (drawing or modeling) into .claude/skills/ of the current project
                    Usage: build123d-mcp install-skill [--skill drawing|modeling] [--force]

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
        default=int(os.environ.get("BUILD123D_EXEC_TIMEOUT") or "120"),
        help="Execution time limit in seconds for user code (default: 120). "
        "Overrides BUILD123D_EXEC_TIMEOUT env var.",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        default=os.environ.get("BUILD123D_IN_PROCESS", "").lower() in ("1", "true", "yes"),
        help="Run the CAD session in the server process instead of a worker "
        "subprocess. Degraded mode for MCP hosts that block subprocess creation "
        "(seen with sandboxed hosts on Windows): no crash containment, no "
        "operation timeouts. Use only if the server reports 'Worker process "
        "failed to start'. Overrides BUILD123D_IN_PROCESS env var.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("BUILD123D_TRANSPORT", "stdio"),
        help="Transport protocol: 'stdio' (default, for MCP clients) or 'http' "
        "(streamable HTTP/ASGI, for web deployments). Overrides BUILD123D_TRANSPORT env var.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("BUILD123D_HOST", "127.0.0.1"),
        help="Host to bind when --transport http (default: 127.0.0.1). "
        "Overrides BUILD123D_HOST env var.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BUILD123D_PORT") or "8000"),
        help="Port to bind when --transport http (default: 8000). "
        "Overrides BUILD123D_PORT env var.",
    )
    parser.add_argument(
        "--memory-limit-mb",
        metavar="MB",
        type=int,
        default=int(os.environ.get("BUILD123D_MEMORY_LIMIT_MB") or "0") or None,
        help="Cap the worker's heap/data segment in MB via RLIMIT_DATA (POSIX only; "
        "ignored on Windows). Note: mmap-backed allocations (large OCC buffers) are "
        "not covered — use container cgroup limits for comprehensive memory control. "
        "Overrides BUILD123D_MEMORY_LIMIT_MB env var.",
    )
    parser.add_argument(
        "--cpu-limit-s",
        metavar="SECONDS",
        type=int,
        default=int(os.environ.get("BUILD123D_CPU_LIMIT_S") or "0") or None,
        help="Cap total CPU time for the worker subprocess in seconds via RLIMIT_CPU "
        "(POSIX only; ignored on Windows). The worker receives SIGXCPU when the soft "
        "limit is reached and is killed at the hard limit. "
        "Overrides BUILD123D_CPU_LIMIT_S env var.",
    )
    args = parser.parse_args()

    if args.library and not os.path.isdir(args.library):
        parser.error(f"Library path is not a directory: {args.library}")

    if args.transport not in ("stdio", "http"):
        parser.error(
            f"invalid BUILD123D_TRANSPORT value '{args.transport}'; must be 'stdio' or 'http'"
        )

    if args.memory_limit_mb is not None and args.memory_limit_mb <= 0:
        parser.error(f"--memory-limit-mb must be a positive integer, got {args.memory_limit_mb}")
    if args.cpu_limit_s is not None and args.cpu_limit_s <= 0:
        parser.error(f"--cpu-limit-s must be a positive integer, got {args.cpu_limit_s}")

    extra_imports = tuple(m.strip() for m in args.allow_imports.split(",") if m.strip())

    if args.allow_all_imports or extra_imports:
        import build123d_mcp.security as _sec

        if args.allow_all_imports:
            _sec.ALLOW_ALL_IMPORTS = True
        if extra_imports:
            _sec.EXTRA_ALLOWED_IMPORTS.update(extra_imports)

    session_cls = InProcessSession if args.in_process else WorkerSession
    session_kwargs: dict = {
        "library_path": args.library,
        "allow_all_imports": args.allow_all_imports,
        "extra_allowed_imports": extra_imports,
        "exec_timeout": args.exec_timeout,
    }
    if not args.in_process:
        if args.memory_limit_mb is not None:
            session_kwargs["memory_limit_mb"] = args.memory_limit_mb
        if args.cpu_limit_s is not None:
            session_kwargs["cpu_limit_s"] = args.cpu_limit_s
    elif args.memory_limit_mb is not None or args.cpu_limit_s is not None:
        print(
            "WARNING: --memory-limit-mb and --cpu-limit-s are ignored in --in-process mode "
            "(no worker subprocess to limit).",
            file=sys.stderr,
        )
    server.configure(session_cls(**session_kwargs))

    if args.transport == "http":
        import uvicorn

        print(
            "WARNING: HTTP transport runs a single shared CAD session. "
            "Concurrent clients will share the same build123d namespace — "
            "suitable for single-user deployments only.",
            file=sys.stderr,
        )
        uvicorn.run(server.http_app(), host=args.host, port=args.port)
    else:
        server.mcp.run()


if __name__ == "__main__":
    main()
