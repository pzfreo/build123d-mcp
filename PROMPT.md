# Session Prompt — build123d-mcp

> **Historical document — not maintained.** This is the original kickoff prompt used to bootstrap the project (implement per `SPEC.md`, five tools, SVG rendering). It is kept only as a record of how the project started; it does not describe the current server, which now has 37 tools and a pyvista/VTK render path. For the current system, read `README.md` (setup), `llms.md` (authoritative tool reference), and `default_prompt.md` (the system prompt for driving the live server). For the design-partner guidelines that still apply, see `CLAUDE.md`.

## What we are building

An MCP (Model Context Protocol) server that wraps build123d, a Python CAD
library. The goal is to let an AI assistant build 3D geometry interactively —
calling tools to create geometry, render views, measure dimensions, and export
files — rather than writing complete scripts blind.

## Repo

https://github.com/pzfreo/build123d-mcp

The repo already contains:
- `README.md` — project overview
- `SPEC.md` — full specification (read this first)

## Your task

Implement the server according to SPEC.md. Key points:

- Use the official `mcp` Python SDK (PyPI: `mcp`)
- stdio transport (not HTTP)
- Stateful session — persistent build123d namespace across tool calls
- Five tools: `execute`, `render_view`, `measure`, `export`, `reset`
- For `render_view`, use build123d's `export_svg` for v1 — return the SVG
  content as text (MCP image/svg or plain text). Do not attempt 3D rendering.
- Follow the file structure in SPEC.md

## Constraints

- Follow the CLAUDE.md guidelines in this repo (simplicity first, no
  speculative features, surgical changes)
- Write tests for each tool
- After implementing, verify the server starts and tools are callable before
  reporting done

## Definition of done

- Server starts without error
- All five tools are registered and callable
- `execute` correctly persists state between calls
- `render_view` returns an SVG for a simple test shape
- `measure` returns correct bounding box for a known shape
- `export` writes a valid STEP file
- `reset` clears session state
- Tests pass
