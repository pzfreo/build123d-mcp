# build123d-mcp

[![PyPI version](https://img.shields.io/pypi/v/build123d-mcp)](https://pypi.org/project/build123d-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/build123d-mcp)](https://pypi.org/project/build123d-mcp/)
[![CI](https://github.com/pzfreo/build123d-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/pzfreo/build123d-mcp/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![build123d-mcp MCP server](https://glama.ai/mcp/servers/pzfreo/build123d-mcp/badges/score.svg)](https://glama.ai/mcp/servers/pzfreo/build123d-mcp)

An MCP (Model Context Protocol) server that exposes build123d CAD operations as tools, enabling AI assistants to build, inspect, and iterate on 3D geometry interactively.

## Why

When using an AI to write build123d scripts, the AI writes blind — it cannot see the geometry it produces. This server closes the feedback loop: the AI can create geometry, render views, query dimensions, and catch errors incrementally rather than writing complete scripts and hoping they are correct.

## Tools

**Core**
- `execute` — run build123d Python code in a persistent session; use `show(shape, name)` to register named parts
- `reset` — clear session back to empty state (namespace, shapes, snapshots)

**Geometry inspection**
- `measure` — full geometric summary: volume, area, topology, bounding box, centre of mass, inertia tensor, face-type inventory
- `clearance` — minimum distance (mm) between two named shapes
- `cross_sections` — cross-sectional areas at evenly spaced planes along X/Y/Z; useful for detecting voids and wall-thickness variation
- `session_state` — full JSON snapshot of active shapes, named objects, snapshot names, and Python namespace variables
- `last_error` — details of the last failed `execute()`: type, message, line number, and code excerpt

**Viewing**
- `render_view` — render one or more shapes as PNG / SVG / DXF; auto-detects 3D vs 2D inputs (composed dimensioned drawings via `build123d.drafting` rasterise via ezdxf+matplotlib); supports assembly compositing, high-quality tessellation, cross-section clip planes, and optional labels for named shapes or specific faces/edges

**Import / export**
- `export` — export as STEP / STL / DXF / SVG (or comma-separated like `step,stl`); auto-detects 2D vs 3D shape and routes to the appropriate format; targets a named object, the current shape, or `*` for all objects as an assembly
- `import_cad_file` — load a STEP or STL file as a named object for comparison

**Comparison**
- `shape_compare` — compare two named shapes by volume, bbox, topology, and centre offset
- `interference` — check intersection volume between two named shapes

**Session checkpoints**
- `save_snapshot` / `restore_snapshot` / `diff_snapshot` — checkpoint, recover, and compare geometric state

**Part library** *(requires `--library` flag)*
- `search_library` — search the part library by keyword; returns full parameter specs
- `load_part` — load a named part with optional parameter overrides

**Utility**
- `version` — return the server version
- `health_check` — verify VTK/SVG/STEP/STL dependencies work end-to-end
- `repair_hints` — get targeted fix suggestions for a given `execute()` error message
- `workflow_hints` — guidance on using the tools effectively

## Resources

Read-only MCP resources available to LLM clients:

- `build123d://quickref` — build123d API quick reference (primitives, booleans, positioning, selectors, fillets)
- `build123d://selectors` — task-indexed selector cookbook (get the top face, find circular edges, filter by area/length/radius, `Select.LAST` in builder context, fillet detection)
- `build123d://drafting` — code-first 2D engineering drawings cookbook (project a 3D part, dimension with ExtensionLine/DimensionLine, tolerances, hole-table pattern, multi-view sheet, title block, export to DXF)
- `build123d://session` — live session state as JSON (current shape, named objects, snapshots, variables)
- `build123d://bd_warehouse` — catalogue of pre-built parametric parts from bd_warehouse (bearings, fasteners, gears, pipes, threads, and more)

> **build123d version**: examples in `quickref` and `selectors` are tested against build123d 0.10.x (soft-pinned in `pyproject.toml` as `>=0.10,<0.11`). The exact installed version is reported at the top of each resource. If you need a different build123d version, override the dependency and verify the examples still match the API.

## Prompts

- `start-cad-session` — primes a new CAD design session with the task description and step-by-step workflow reminders

See [llms.md](llms.md) for full tool reference and usage patterns.

## Recommended workflow

Build complexity falls into two tiers and the right approach differs between them.

**Simple shapes** (a few primitives, up to ~5 booleans): build entirely in `execute()`.

**Complex shapes** (IsoThread, multi-body fillets, high face counts): the `execute()` timeout (default 120 s) is a hard ceiling. The efficient pattern is:

1. **Probe** in the MCP — small `execute()` calls to discover API signatures, size strings, and face counts. Use `dir()` and `import inspect; inspect.signature(ClassName)` freely.
2. **Build** in a Python script — run it with Bash (or your shell). No timeout, full Python.
3. **Import and verify** in the MCP:
   ```
   import_cad_file("/path/to/part.step", "part")
   measure("part")          # verify volume, topology, bounding box
   render_view(objects="part")  # visualise
   ```

> **Timeout note:** the default is 120 s. Raise it with `--exec-timeout N` or `BUILD123D_EXEC_TIMEOUT=N`. When a timeout fires, all session state is lost (worker is restarted) — you must re-run any setup code.

> **Import note:** after `import_cad_file()` the shape is a named session object. Always render it by name (`objects="part"`) when other shapes from the same build are also in session — two co-located shapes cause Z-fighting (striped colour artifacts). STL imports produce a shell (volume = 0); `render_view` and `measure` work, but `interference()` and boolean operations require a solid.

## bd_warehouse fasteners

bd_warehouse is a full fastener system, not just a thread library. Always:

1. **Probe sizes first** (correct string format is `"M6-1"` not `"M6-1.0"`):
   ```python
   from bd_warehouse.fastener import CounterSunkScrew
   print(CounterSunkScrew.sizes("iso10642"))
   ```
2. **Instantiate the fastener object**, then pass it to the hole operation — never compute head geometry or tap-drill diameters manually:
   ```python
   from bd_warehouse.fastener import CounterSunkScrew, CounterSinkHole, TapHole
   screw = CounterSunkScrew(size="M6-1", fastener_type="iso10642", length=10)

   with BuildPart() as wheel:
       Cylinder(radius=20, height=10)
       CounterSinkHole(fastener=screw, depth=10)   # countersunk through-hole
       TapHole(fastener=screw, depth=8)             # tapped bore
   ```

See `build123d://bd_warehouse` (MCP resource) for the full catalogue and usage patterns.

## Security

Unlike CAD MCP servers that simply `exec()` user code, build123d-mcp ships with **defence-in-depth sandboxing** so the server is reasonable to expose to LLM-generated and untrusted prompts. Three layers, all applied before user code runs:

1. **AST inspection** — rejects imports of anything outside the allowlist (`build123d`, `bd_warehouse`, `math`, `numpy`, `inspect`, plus the rest of the safe stdlib subset and a curated set of geometric OCP submodules), blocks `eval`/`exec`/`compile`/`open`, and refuses dunder attribute access (the most common Python sandbox-escape route).
2. **Restricted builtins** — the `__builtins__` exposed to user code has the dangerous functions removed and `__import__` rewrapped to enforce the same allowlist at runtime, so a payload that bypasses the AST check still hits the wall on import.
3. **Execution timeout** — wall-clock limit (default 120 s, `--exec-timeout N` to override) enforced via SIGALRM, with the worker process restarted on breach so a hung script can't hold the session forever.

Filesystem I/O modules (`os`, `pathlib`, `shutil`), networking (`socket`, `urllib`, `requests`), shell access (`subprocess`), and the OCP file-I/O submodules (`STEPControl`, `IGESControl`, `OSD`, …) are **all blocked**. Path traversal is rejected for `export()` and `render_view(save_to=)`.

This is not a perfect sandbox — memory exhaustion isn't bounded, and Python introspection chains via build123d internals could in principle escape — but it raises the bar significantly against realistic prompt-injection payloads.

**The part library is trusted input.** Files under `--library` run with the same restricted builtins as user code, but the AST check inspects only each file's own top-level imports — it is a guard against accidents, not sandbox-equivalent isolation. Point `--library` only at directories you control, never at untrusted downloads.

### Extending or relaxing the sandbox

Two CLI flags let you adjust the import policy without giving up the rest of the layers:

- `--allow-imports scipy,pandas` — extend the allowlist with named modules. Each entry permits the named root and all its submodules. Use for CAD scripts that need extra packages.
- `--allow-all-imports` — disable the import allowlist entirely. The other layers (restricted builtins for `open`/`eval`/etc, exec timeout, dunder-attribute block) still apply. **Use only in trusted environments or under OS-level isolation** (see below).

Both flags also accept their values via env var (`BUILD123D_ALLOW_IMPORTS`, `BUILD123D_ALLOW_ALL_IMPORTS`).

### Stronger isolation: OS-level sandboxing

For deployments that need stronger guarantees than Python-level checks (e.g. exposing the server to truly untrusted input, or running with `--allow-all-imports`), wrap the whole MCP server in an OS-level sandbox:

- **[`@anthropic-ai/sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime)** — Anthropic's official sandbox runtime, designed exactly for this. The Claude Code docs explicitly call out wrapping MCP servers: `npx @anthropic-ai/sandbox-runtime <command-to-sandbox>`.
- **Docker / containers** — generic approach; many community MCP-sandbox wrappers exist (e.g. [`pottekkat/sandbox-mcp`](https://github.com/pottekkat/sandbox-mcp), [`Automata-Labs-team/code-sandbox-mcp`](https://github.com/Automata-Labs-team/code-sandbox-mcp)). Run build123d-mcp inside a minimal container with no host filesystem mounts and no network egress.
- **Claude Code's sandbox** (`/sandbox` command, macOS Seatbelt or Linux bubblewrap) — if you're running build123d-mcp under Claude Code, the host's sandbox already restricts what subprocesses can touch.
- **Cursor / IDE dev containers** — Cursor doesn't ship MCP-specific sandboxing, but you can run the server inside a dev container that the IDE attaches to.

Inside any of these, **`--allow-all-imports` becomes a reasonable default**: the OS-level isolation handles the security, and the Python-level allowlist becomes redundant friction. The recommended high-security recipe is `sandbox-runtime` (or a container) + `--allow-all-imports` + a strict exec timeout.

## Requirements

- [uv](https://github.com/astral-sh/uv)
- An MCP-compatible client (Claude Code, Claude Desktop, Cursor, etc.)

All Python dependencies (build123d, vtk, etc.) are installed automatically by uv.

## Installation

No clone needed. Install directly from PyPI:

```bash
pip install build123d-mcp
```

Or just use `uv tool run` — it fetches and runs the package in one step with no prior install required (see below).

---

## Adding to MCP clients

The server runs over stdio — the client launches it as a subprocess using `uv tool run build123d-mcp`.

> **Note on Python version.** All examples below pass `--python 3.12`. VTK and cadquery-ocp do not yet ship wheels for Python 3.13+, so pinning to 3.12 is required. uv will auto-download a managed Python 3.12 if you don't already have one.

> **Note on `@latest`.** The examples request `build123d-mcp@latest` so each launch re-resolves to the latest published release instead of reusing uv's cached environment — without it, the client can stay pinned to whatever version uv first cached and silently miss releases. The trade-off is a short dependency-resolution step at every startup (and it needs network access to check for updates). Use plain `build123d-mcp` if you prefer faster, offline-capable starts and update manually with `uv tool upgrade build123d-mcp`. (Older versions of this README passed `--upgrade` instead; recent uv ignores that flag in `uv tool run` and warns on every launch — swap to `@latest` if you have the old config.)

### Claude Code

Add to your project's `.mcp.json` (or `~/.claude/mcp.json` for global use):

```json
{
  "mcpServers": {
    "build123d-mcp": {
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

Restart Claude Code after editing. The tools appear automatically once connected.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "build123d-mcp": {
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

Restart Claude Desktop after saving.

### Cursor

Open **Settings → MCP** and add a new server entry, or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "build123d-mcp": {
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

### VS Code (GitHub Copilot / Continue)

For **Continue** extension, add to `.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "build123d-mcp",
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  ]
}
```

For **GitHub Copilot** with MCP support, add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "build123d-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

---

## System prompt

For best results, paste the contents of [default_prompt.md](default_prompt.md) as a system prompt in your AI client. This tells the assistant to work incrementally, verify geometry after each step, and use the tools in the right order.

---

## Status

Active development (v0.3.14).

<!-- mcp-name: io.github.pzfreo/build123d-mcp -->
