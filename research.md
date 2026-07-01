# build123d-mcp Codebase Research

Version: 0.3.10 | Python 3.10–3.12 | Tests: 198 passing (74% coverage)

---

## Architecture

### Component Map

```
server.py (FastMCP, 299 lines)
  - 20 MCP tools registered via @mcp.tool()
  - Holds module-level _session singleton (WorkerSession)
  - CLI: --library, --allow-all-imports, --version

WorkerSession (worker.py, 307 lines)
  - Parent-side proxy; manages subprocess lifecycle
  - Communicates via multiprocessing.Pipe (spawn context)
  - Kills & restarts worker on crash or timeout
  - Per-operation timeouts: execute=30s, render=120s, export=60s, measure=10s

worker_main() (worker.py)
  - Child process entry point
  - Owns Session instance + all tool implementations
  - Single-threaded dispatch loop: receives op dict, calls tool, sends result

Session (session.py, 230 lines)
  - In-process state: namespace, current_shape, objects, snapshots, last_error_detail
  - Executes user code with three-layer security
  - SIGALRM timeout (exec_timeout - 2s margin for early error before parent kills)

security.py (183 lines)
  - Layer 1: AST check before exec
  - Layer 2: Restricted builtins dict in exec namespace
  - Layer 3: Timeout (SIGALRM on Unix, parent-kill on Windows)

tools/
  execute.py     — thin wrapper → session.execute()
  render.py      — VTK PNG + build123d SVG; 430 lines
  measure.py     — geometry queries → JSON; 100 lines
  export.py      — STEP/STL; 78 lines
  interference.py — boolean intersection check; 41 lines
  library.py     — part scan/search/load; mtime-cached index
  diff.py        — snapshot comparison
  authoring.py   — validate_code, shape_compare, repair_hints
  hints.py       — workflow_hints
```

### Why subprocess isolation?

Parent never loads OCCT, VTK, or OCC geometry. Worker can crash without killing the server. Enables future multi-client concurrency. The cost is IPC overhead on every call.

---

## Session Model

### What persists

| State | Persists across execute() | Persists across restore_snapshot() |
|-------|--------------------------|-------------------------------------|
| namespace (variables) | Yes | No — snapshot does NOT restore namespace |
| current_shape | Yes (auto-detected) | Yes |
| objects dict | Yes | Yes |
| snapshots | Yes | — |
| last_error_detail | Until next success | — |

### Auto-detection of current_shape

After each execute(), scans new variables in this priority order:

1. Variable named `result` that is a Shape
2. First new variable that is a BuildPart → extracts `.part`
3. First new variable that is a Shape

Skips names starting with `_`. `show(shape, name)` also sets current_shape as a side effect.

### Snapshot gotcha

`restore_snapshot()` restores geometry only. Python variables defined after the snapshot point remain in scope. This is documented in CLAUDE.md but not in the code itself.

```python
execute("x = 10; result = Box(x, x, x)")
save_snapshot("v1")
execute("x = 20")
restore_snapshot("v1")
# current_shape is Box(10,10,10), but x is still 20
```

### Error rollback

On any exception during execute(), the session atomically rolls back:
- namespace restored to pre-execute state (new keys deleted, overwritten values restored)
- current_shape restored
- objects restored

Exceptions: `AssertionError` gets a special "Constraint failed" message. Timeout leaves namespace dirty — caller must reset() or restore_snapshot().

---

## Security Model

### Layer 1: AST inspection (check_ast)

Runs before exec. Rejects:

- `import X` or `from X import` where X root not in IMPORT_ALLOWLIST
- Bare calls to: `__import__`, `eval`, `exec`, `compile`, `open`, `breakpoint`, `input`
- Introspection calls: `getattr`, `vars`, `dir`, `hasattr`
- Any attribute access where the attr name starts and ends with `__`

**Import allowlist (26 modules):** build123d, bd_warehouse, math, numpy, decimal, fractions, statistics, numbers, random, collections, itertools, functools, copy, operator, struct, typing, abc, dataclasses, enum, re, string, textwrap, pprint, json, base64, hashlib, io, warnings, contextlib

### Layer 2: Restricted builtins

The exec namespace gets a filtered `__builtins__` dict. Removed: `open`, `eval`, `exec`, `compile`, `breakpoint`, `input`, `getattr`, `vars`, `dir`, `hasattr`. `__import__` is replaced with a wrapper that enforces the same allowlist at runtime, providing defence-in-depth independent of the AST check.

### Layer 3: Timeout

- Unix/main thread: SIGALRM fires at `exec_timeout - 2` seconds, raising ExecutionTimeout cleanly inside the worker
- Parent-side: polls pipe; sends SIGKILL if no response within operation timeout
- After kill: worker is restarted (fresh session, all geometry lost)

### Known gaps

| Gap | Severity | Mitigation |
|-----|----------|------------|
| Memory exhaustion (`[0]*10**10`) | Medium | OS OOM killer |
| Timeout leaves namespace dirty | Low | Caller must reset() |
| build123d internals can call open/subprocess | Low | Import of os/subprocess blocked; attacker needs to know API internals |
| No seccomp/container isolation | Low for dev, Medium for untrusted | Add at deployment layer |

---

## Tools

### execute

Three-layer security → compile → capture stdout/stderr → exec → auto-detect shape → return output + auto-diagnostics (volume, faces, bbox) if no stdout and a new shape was created.

### render_view

Two backends:

- **PNG**: VTK raster (800×600). Xvfb spawned on headless Linux. Tessellates shapes, builds VTK polydata, applies optional clip plane at mesh bbox midpoint, sets camera from direction preset + azimuth/elevation, Phong shading.
- **SVG**: build123d ExportSVG (HLR projection). Visible edges solid, hidden edges dashed. Clip plane splits shape before projection.

If PNG fails, auto-falls back to SVG with an error key in the response. Object colors: user-specified as `"name:color"` or auto-assigned from palette.

**Path safety:** `save_to` is validated by `safe_output_path()` — blocks symlink escapes and `..` traversal, restricts to cwd / tempdir / /tmp.

### measure

Queries: `bounding_box`, `volume`, `area`, `topology`, `min_wall_thickness`, `clearance`, `summary`.

`min_wall_thickness` ray-casts inward from each face center along its inward normal; returns minimum distance. `clearance` uses OCCT `distance_to()`. Both require named objects or fall back to current_shape.

### export

Formats: `step`, `stl`, `step,stl`. `object_name=""` → current_shape; `object_name="*"` → Compound of all named objects (assembly export).

STL uses a custom binary writer (tessellate + struct.pack) rather than build123d's Mesher — a deliberate workaround for Lib3MF validation failures on complex shapes.

### interference

Boolean intersection of two named shapes. Volume threshold 1×10⁻⁶ mm³ to suppress floating-point noise. Returns `{interferes, volume, bounds}`.

### library tools

`search_library`: scans .py files for a `PART_INFO` dict using AST `literal_eval` (no exec). Caches with mtime tracking; auto-rescans if any file or directory changes.

`load_part`: executes the part file in an isolated namespace through the same security checks as execute(), calls `make(**params)`, registers result via show().

### authoring tools

`validate_code`: syntax + security check without executing. Returns blocked imports, dangerous calls, and warnings (no build123d import, no result/show call).

`repair_hints`: regex pattern-matching on error messages. Covers NoneType, Location syntax, fillet edge selection, missing imports, degenerate shapes, ExecutionTimeout. Advisory only.

`shape_compare`: compares two named objects by volume, topology, bbox, center offset distance, plus a localized surface-deviation diff (where the geometry changed, with an exact-boolean magnitude — surface displacement + added/removed volume — bounded in a subprocess).

`diff_snapshot`: compares two snapshots (or one snapshot vs current state) by volume delta, topology delta, added/removed objects. Returns text or JSON.

`design_audit`: surfaces the session program's top-level numeric parameters (Θ) and perturbs each ±ε, re-running the validity gate to flag *brittle* parameters where a small edit collapses the solid (Arko-T design-state robustness). Rebuild+gate loop runs in a hard-bounded subprocess.

---

## Test Coverage

```
tests/test_tools.py    — 116 tests; core tools, security, state, edge cases
tests/test_outcomes.py —  65 tests; multi-step workflows, MCP wire protocol (stdio)
tests/test_library.py  —  17 tests; scan, search, load_part, mtime, security
```

All 198 pass. 74% line coverage. Tests use fresh Session fixtures for isolation. Outcome-focused: geometry values verified, not just "no exception". End-to-end MCP stdio round-trips tested in test_outcomes.py.

**Coverage gaps (~26%):** rare exception paths in tools, some render edge cases (coordinate-based clip), part library malformed PART_INFO, Windows-specific paths.

---

## Configuration

CLI flags and equivalent env vars:

| Flag | Env var | Effect |
|------|---------|--------|
| `--library PATH` | `BUILD123D_PART_LIBRARY` | Enable part library |
| `--allow-all-imports` | `BUILD123D_ALLOW_ALL_IMPORTS=1` | Disable import allowlist |

Timeouts are hardcoded in WorkerSession. exec_timeout is passed to Session (default 30s); render/export/measure timeouts are constants in worker.py.

---

## Code Quality Notes

### Issues worth addressing

1. ~~**Snapshot stores references, not deep copies.**~~ **Fixed in PR #61.** `save_snapshot()` now calls `copy.copy()` on each shape before storing it.

2. ~~**Timeout thread cleanup.**~~ **Fixed in PR #61.** SIGALRM is cancelled immediately after `exec()` succeeds (before post-exec shape detection); `TemporaryDirectory(ignore_cleanup_errors=True)` prevents file-lock exceptions from propagating in both PNG and SVG render paths.

3. **Type hints are sparse.** `session` parameters are untyped across tools. A `SessionLike` Protocol would allow mypy to verify all tools against the interface without coupling to the concrete class. Tracked in issue #60.

4. ~~**`health_check` reports `"bytes": len(png)`**~~ **Fixed in PR #61.** Tuple unpacked to `img_bytes, _warnings`; byte count now correctly reflects PNG size.

5. ~~**Render fallback UX.**~~ **Fixed in PR #61.** Added `"format": "svg"` key to the PNG→SVG fallback response so clients know what content type was returned.

### Non-issues

- Dict insertion order is stable (Python 3.7+), so object rendering order is deterministic.
- `export(..., object_name="*")` Compound creation works in practice; type-unsafe but not a practical risk.
- Custom STL writer is documented and intentional.

---

## Dependencies

| Package | Role |
|---------|------|
| `mcp` | FastMCP protocol framework |
| `build123d` | CAD geometry (OCCT-based) |
| `pyvista` | VTK wrapper for PNG rendering |
| `bd_warehouse` | build123d standard parts |
| `OCP` (via build123d) | OpenCascade Python bindings |

Python 3.10–3.12 required (3.13+ not yet supported by VTK/OCP wheels). On headless Linux, Xvfb is spawned automatically for PNG rendering.

---

## Summary

build123d-mcp is a well-engineered, production-ready MCP server. The three-layer security model, transactional state rollback, and subprocess isolation are the most notable architectural strengths. The test suite is comprehensive and outcome-focused rather than mock-heavy.

The main practical limitations are: memory exhaustion is unbounded, timeout leaves namespace dirty (requiring caller discipline), and Windows loses the SIGALRM early-timeout behaviour. Snapshots now deep-copy shapes (PR #61). None of these are blockers for trusted development environments. For untrusted input, add container or seccomp isolation at the deployment layer.

The one remaining code quality item is sparse type hints across tool functions (issue #60, item 3): adding a `SessionLike` Protocol would enable mypy to verify all tool implementations against the session interface.
