"""Command-line entry point for the build123d MCP server.

Parses CLI arguments / environment variables, wires up the ``WorkerSession``,
and starts the MCP server defined in ``server.py``. Kept separate so
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
        help=f"Workflow to install: {'/'.join(SKILLS)} (default: drawing)",
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
    from build123d_mcp.tools.install_skill import SKILLS
    from build123d_mcp.viewer import DEFAULT_MAX_CLIENT_BYTES as DEFAULT_VIEWER_MAX_CLIENT_BYTES
    from build123d_mcp.worker import InProcessSession, WorkerSession

    if len(sys.argv) > 1 and sys.argv[1] == "install-skill":
        _cmd_install_skill(sys.argv[2:])
        return

    _skill_list = "/".join(SKILLS)
    _skill_epilog = f"""\
Subcommands:
  install-skill     Copy a b123d workflow skill ({_skill_list}) into .claude/skills/ of the current project
                    Usage: build123d-mcp install-skill [--skill {_skill_list}] [--force]
"""
    parser = argparse.ArgumentParser(
        prog="build123d-mcp",
        description="MCP server for interactive 3D CAD via build123d. Communicates over stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_skill_epilog
        + """
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
        "--no-sandbox",
        action="store_true",
        default=os.environ.get("BUILD123D_NO_SANDBOX", "").lower() in ("1", "true", "yes"),
        help="Disable ALL execute() sandbox layers: the AST check is skipped and user "
        "code runs with unrestricted builtins (open/eval/exec/__import__ available). "
        "DANGEROUS — for trusted, isolated environments only (e.g. an isolated batch pipeline). "
        "Implies --allow-all-imports. Overrides BUILD123D_NO_SANDBOX env var.",
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
        "--allow-all",
        "--disable-dns-rebinding-protection",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("BUILD123D_ALLOW_ALL", "").lower() in ("1", "true", "yes"),
        help="Accept every Host and Origin header on --transport http, i.e. turn off the "
        "SDK's DNS-rebinding protection (the SDK check is all-or-nothing). Use when a reverse "
        "proxy / gateway terminates the real host. Conflicts with --allowed-hosts / "
        "--allowed-origins. --no-allow-all overrides BUILD123D_ALLOW_ALL=1.",
    )
    parser.add_argument(
        "--allowed-hosts",
        metavar="HOSTS",
        default=os.environ.get("BUILD123D_ALLOWED_HOSTS", ""),
        help="Comma-separated Host header values accepted on --transport http in addition to "
        "the localhost defaults, e.g. 'cad.example.com,cad.example.com:*' (trailing :* accepts "
        "any port). Overrides BUILD123D_ALLOWED_HOSTS env var.",
    )
    parser.add_argument(
        "--allowed-origins",
        metavar="ORIGINS",
        default=os.environ.get("BUILD123D_ALLOWED_ORIGINS", ""),
        help="Comma-separated Origin header values accepted on --transport http in addition to "
        "the localhost defaults, e.g. 'https://cad.example.com,http://cad.example.com' (scheme "
        "is part of the entry; a trailing :* accepts any port, but not the default port). Only "
        "relevant to browser-based clients. Overrides BUILD123D_ALLOWED_ORIGINS env var.",
    )
    parser.add_argument(
        "--max-sessions",
        metavar="N",
        type=int,
        default=int(os.environ.get("BUILD123D_MAX_SESSIONS") or "1"),
        help="Number of isolated CAD sessions to allow when --transport http (default: 1, "
        "meaning every request shares one session, as before). Above 1, a request carrying "
        "an Mcp-Cad-Session header gets that handle's own session and worker subprocess; "
        "requests without the header still share the default one. Each session costs a "
        "subprocess with the OCC kernel loaded (hundreds of MB), so size this against "
        "available memory. Overrides BUILD123D_MAX_SESSIONS env var.",
    )
    parser.add_argument(
        "--session-idle-timeout",
        metavar="SECONDS",
        type=int,
        default=int(os.environ.get("BUILD123D_SESSION_IDLE_TIMEOUT") or "3600"),
        help="Close a per-handle CAD session after this many seconds without a request, "
        "reclaiming its subprocess (default: 3600; 0 disables reaping). A client returning "
        "after eviction transparently gets a fresh, empty session under the same handle. "
        "Overrides BUILD123D_SESSION_IDLE_TIMEOUT env var.",
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
    parser.add_argument(
        "--disable-tool-groups",
        default=os.environ.get("BUILD123D_DISABLE_TOOL_GROUPS", ""),
        help="Comma-separated optional tool groups to NOT register, to slim the tool "
        "surface for context-sensitive deployments (fleets, automated pipelines). "
        "Currently: 'drawing' (the 2D drawing-authoring suite). The part-library tools "
        "auto-hide when no --library is set. Overrides BUILD123D_DISABLE_TOOL_GROUPS.",
    )
    parser.add_argument(
        "--tools",
        default=os.environ.get("BUILD123D_TOOLS", ""),
        help="Comma-separated exact tool names to expose. When set, every other tool is "
        "removed from the advertised MCP surface. Intended for context-sensitive agents "
        "and controlled evaluations. Overrides BUILD123D_TOOLS.",
    )
    parser.add_argument(
        "--experimental",
        action="store_true",
        default=os.environ.get("BUILD123D_EXPERIMENTAL", "").lower() in ("1", "true", "yes"),
        help="Enable experimental, not-yet-production-ready tools (currently verify_spec "
        "and suggest_spec). Off by default. Overrides BUILD123D_EXPERIMENTAL env var.",
    )
    parser.add_argument(
        "--viewer-socket",
        metavar="PATH",
        default=os.environ.get("BUILD123D_VIEWER_SOCKET", ""),
        help="Bind a Unix domain socket at PATH and stream live mesh updates (glb "
        "over a length-prefixed protocol) to connected viewer clients, so a human "
        "can watch and rotate the model as the session changes. Off by default; "
        "adds no cost when no viewer is attached. POSIX only. "
        "Overrides BUILD123D_VIEWER_SOCKET env var. See docs/live-viewer.md.",
    )
    parser.add_argument(
        "--viewer-max-client-bytes",
        metavar="BYTES",
        type=int,
        # A string default goes through type=int, so a bad env value is a usage error.
        default=os.environ.get("BUILD123D_VIEWER_MAX_CLIENT_BYTES")
        or str(DEFAULT_VIEWER_MAX_CLIENT_BYTES),
        help="Cap the output a single live viewer may have queued in the server, in bytes "
        f"(default: {DEFAULT_VIEWER_MAX_CLIENT_BYTES}, i.e. 256 MiB). A slow viewer loses its "
        "oldest updates past the cap; a viewer whose on-connect scene or single update does "
        "not fit is disconnected and the reason is logged to stderr. Raise it for very large "
        "scenes. Overrides BUILD123D_VIEWER_MAX_CLIENT_BYTES env var.",
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
    if args.max_sessions < 1:
        parser.error(f"--max-sessions must be at least 1, got {args.max_sessions}")
    if args.session_idle_timeout < 0:
        parser.error(
            "--session-idle-timeout must be 0 (never reap) or a positive number of seconds, "
            f"got {args.session_idle_timeout}"
        )

    allowed_hosts = _csv(args.allowed_hosts)
    allowed_origins = _csv(args.allowed_origins)
    for flag, entries in (
        ("--allowed-hosts", allowed_hosts),
        ("--allowed-origins", allowed_origins),
    ):
        problem = _entry_problem(flag, entries)
        if problem:
            parser.error(problem)
    if args.transport == "http" and args.allow_all and (allowed_hosts or allowed_origins):
        parser.error(
            "--allow-all is incompatible with --allowed-hosts/--allowed-origins (the allowlists "
            "may come from BUILD123D_ALLOWED_HOSTS/BUILD123D_ALLOWED_ORIGINS; pass "
            "--allowed-hosts '' / --allowed-origins '' to clear them, or --no-allow-all to "
            "clear BUILD123D_ALLOW_ALL)"
        )

    if args.viewer_max_client_bytes <= 0:
        parser.error(
            f"--viewer-max-client-bytes must be a positive integer, got {args.viewer_max_client_bytes}"
        )

    if args.viewer_socket:
        if sys.platform == "win32":
            parser.error("--viewer-socket requires a POSIX platform (AF_UNIX sockets)")
        parent = os.path.dirname(os.path.abspath(args.viewer_socket))
        if not os.path.isdir(parent):
            parser.error(f"--viewer-socket parent directory does not exist: {parent}")

    extra_imports = tuple(m.strip() for m in args.allow_imports.split(",") if m.strip())

    if args.allow_all_imports or extra_imports or args.no_sandbox:
        import build123d_mcp.security as _sec

        if args.no_sandbox:
            _sec.DISABLE_SANDBOX = True
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
        "no_sandbox": args.no_sandbox,
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

    if args.experimental:
        server.register_experimental_tools()

    disabled_groups = tuple(g.strip() for g in args.disable_tool_groups.split(",") if g.strip())
    enabled_tools = tuple(t.strip() for t in args.tools.split(",") if t.strip())
    server.apply_tool_visibility(
        disabled_groups, has_library=bool(args.library), enabled_tools=enabled_tools
    )

    if args.viewer_socket:
        server.start_viewer(args.viewer_socket, max_client_bytes=args.viewer_max_client_bytes)

    if args.transport == "http":
        import uvicorn

        settings = _build_transport_security_settings(
            args.allow_all, allowed_hosts, allowed_origins
        )
        if settings is not None:
            server.configure_transport_security(settings)

        if args.max_sessions > 1:
            from build123d_mcp.session_registry import SessionRegistry

            server.configure_registry(
                SessionRegistry(
                    factory=lambda: session_cls(**session_kwargs),
                    max_sessions=args.max_sessions,
                    idle_timeout_s=args.session_idle_timeout,
                )
            )
            print(
                f"HTTP transport: up to {args.max_sessions} isolated CAD sessions, keyed on the "
                "Mcp-Cad-Session header. Requests without that header share one default session. "
                "This server does NOT authenticate handles — put an auth gateway in front of it, "
                "and a session-aware load balancer if you run more than one process.",
                file=sys.stderr,
            )
        else:
            print(
                "WARNING: HTTP transport runs a single shared CAD session. "
                "Concurrent clients will share the same build123d namespace — "
                "suitable for single-user deployments only. Pass --max-sessions "
                "to isolate clients by Mcp-Cad-Session header.",
                file=sys.stderr,
            )
        uvicorn.run(server.http_app(), host=args.host, port=args.port)
    else:
        server.mcp.run()


def _csv(value: str) -> list[str]:
    """Split a comma-separated CLI/env value, dropping blanks."""
    return [v.strip() for v in value.split(",") if v.strip()]


def _entry_problem(flag: str, entries: list[str]) -> str | None:
    """Reject entries the SDK would accept but can never match (it has no globs)."""
    for entry in entries:
        base = entry[:-2] if entry.endswith(":*") else entry
        if "*" in base:
            return (
                f"{flag}: {entry!r} can never match; the only wildcard is a trailing ':*' "
                "(any port), e.g. 'cad.example.com:*'"
            )
        if flag == "--allowed-origins" and "://" not in entry:
            return f"{flag}: {entry!r} needs a scheme, e.g. 'https://{entry}'"
        if flag == "--allowed-hosts" and base.count(":") > 1 and not base.startswith("["):
            return f"{flag}: IPv6 host {entry!r} must be bracketed, e.g. '[::1]:*'"
    return None


_LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _build_transport_security_settings(
    allow_all: bool, allowed_hosts: list[str], allowed_origins: list[str]
):
    """Map the transport-security flags to a TransportSecuritySettings, or None
    when nothing is set (the SDK default, localhost-only, stays in place).

    Operator entries are added to the SDK's localhost defaults, never replace
    them, so a health check on 127.0.0.1 keeps working. ``allow_all`` is protection
    off: the SDK has no match-everything entry."""
    if not (allow_all or allowed_hosts or allowed_origins):
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    if allow_all:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_LOCAL_HOSTS + allowed_hosts,
        allowed_origins=_LOCAL_ORIGINS + allowed_origins,
    )


if __name__ == "__main__":
    main()
