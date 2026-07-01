# build123d-mcp — Specification

> **Historical document — not maintained.** This spec was written before the initial implementation and is kept only as a record of the original design intent. The codebase has diverged far beyond it: there are now **37 tools** (not five), rendering uses pyvista/VTK not SVG, and the server has grown validity gating, defect location, feature recognition, printability analysis, engineering-drawing/GD&T support, a live viewer, session snapshots, and a design-state (`design_audit`) tool. **For anything current, `llms.md` is the authoritative tool reference and `README.md` the setup guide** — do not treat the tool list or behaviour below as accurate.

## Purpose

An MCP server that wraps build123d, enabling an AI assistant to build, inspect,
and iterate on 3D CAD geometry interactively. The AI calls tools to create
geometry, render views, check dimensions, and export files — closing the feedback
loop that is missing when the AI writes scripts blind.

## Architecture

### Transport
stdio — the MCP client launches the server as a subprocess and communicates over
stdin/stdout. Simple, no network required, suitable for local use.

### State
The server maintains a persistent build123d session in memory across tool calls.
A single global `BuildPart` context (or equivalent) holds the current model.
Each `execute` call runs in that context, allowing incremental construction.

A `reset` tool clears the session back to empty.

### Python SDK
Use the official `mcp` Python SDK (PyPI: `mcp`).

---

## Tools (v1)

### `execute`
Run arbitrary build123d Python code in the persistent session.

- Input: `code` (string) — Python source to execute
- Returns: stdout/stderr output, or error message if execution fails
- The session namespace persists between calls

### `render_view`
Render the current model and return an image.

- Input: `direction` (string) — one of `top`, `front`, `side`, `iso`
- Returns: PNG image (MCP image content type)
- Uses build123d's export or OCP viewer to generate the image

### `measure`
Query geometry of the current model.

- Input: `query` (string) — one of `bounding_box`
- Returns: JSON with dimensions (x, y, z extents and centre)

### `export`
Export the current model to a file.

- Input: `filename` (string), `format` (string) — `step` or `stl`
- Returns: confirmation message with file path

### `reset`
Clear the current session back to an empty state.

- No inputs
- Returns: confirmation

---

## Rendering

build123d does not have a built-in headless renderer. Options:
- Use `export_svg` for a quick 2D projection (simple, no dependencies)
- Use pythonOCC/VTK offscreen rendering for a proper 3D view (more complex)
- Use `export_stl` then render with a headless tool (e.g. Blender CLI, trimesh)

Start with SVG projection for v1 — simple and dependency-free. Upgrade to
3D rendering in v2.

---

## File Structure

```
build123d-mcp/
  server.py        # MCP server entry point
  session.py       # persistent build123d session management
  tools/
    execute.py
    render.py
    measure.py
    export.py
  requirements.txt
  README.md
```

---

## Requirements

- Python 3.10+
- `mcp` (official MCP Python SDK)
- `build123d`

## Non-goals (v1)

- Multi-session support
- Remote/HTTP transport
- History/undo
- Full rendering (3D shaded view) — SVG projection only for now
