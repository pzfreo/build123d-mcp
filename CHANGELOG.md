# Changelog

## v0.3.61

### Added

- **`verify_spec()` — "did I build what was requested?" conformance gate.** `validate()` proves a solid is *valid*; `verify_spec()` proves it matches a declared **design-intent spec** — the loop nothing else closes (a part can pass every existing check and still be missing a hole or have the bolt circle at the wrong PCD; geometric-agreement scores mask exactly this, Arko-T §5.5). Given a spec (inline JSON or a `.json` path) with `envelope_mm`, `solid {count, valid}`, `volume_mm3`, `features` (`hole_pattern`/`hole`/`boss`), and `parameters` (top-level numeric ranges), it checks each requirement against the actual geometry using the existing validity gate / `measure` / feature recognition / parameter extraction, and returns a report where **every line carries its evidence tier** (`measured` > `structural` > `recognised`) and a `PASS`/`FAIL`/`UNVERIFIED` status. `conforms` = no FAILs **and at least one requirement was actually checked** (a spec that verifies nothing — every key unrecognised, deferred, or unverifiable — reports `conforms:false` with a warning, never a vacuous true); the summary carries a `checked` count. **UNVERIFIED requirements are never counted as met** — a requested feature type we don't recognise, a deferred check (`min_wall_mm`), or a declared-unverifiable `target` (e.g. fatigue, needing a solver) is surfaced honestly rather than silently passed or falsely failed. A malformed spec (e.g. an envelope axis as a scalar, `features` as an object) returns a clean structured error naming the bad field rather than crashing. Dimensions match within `max(0.1 mm, 1%)`; counts exact. Because a spec is a reusable contract, re-running `verify_spec()` after any edit is a **regression/acceptance gate** that catches collateral breakage (a boolean that dropped a hole, an edit that pierced a wall). The tool structurally cannot report "the design is correct" — it hands back tiered, bounded evidence and states it is not a certification. Composes existing checkers only (no new geometry code, no subprocess, no new dependency). Synthesises Arko-T's design-state (feature-realization, rec #3) with an evidence-honesty discipline, on build123d-mcp's B-rep validity depth. (#335, see `docs/design-conformance-proposal.md`)

## v0.3.60

### Added

- **`design_audit()` — audit the session program as a *design*, not just a shape.** Every other tool inspects the executed geometry `g` (`validate` proves it's a valid shape, `shape_compare` measures a change); none inspects the *design* `z` — the named parameters — that produced it. Following Arko-T (arXiv:2606.30429, "a design to edit, not a shape to render"), this surfaces the program's top-level numeric parameters (Θ) and, for each, rebuilds the whole program with the value nudged ±ε (default ±10%) and re-runs the existing validity gate. A parameter is flagged **brittle** if a small change fails to rebuild or drops the solid below the gate — the thin-wall / coordinate-reasoning failure mode where a valid *shape* is not a robust *design* (Arko-T §5.5/§6), caught structurally rather than by luck. Returns per-parameter perturbation results (`delta_pct`, `passes_gate`, `volume_delta_pct`, failure `reasons`) plus a baseline and summary; a program with no named parameters gets an advisory to hoist its inline magic constants into a parameter block (§4.3), and a parameter reassigned at the top level is flagged inconclusive rather than falsely passed. Brittleness is judged purely by the validity gate, so a large but still-valid change (e.g. a *count* parameter that adds holes) is not flagged — its volume delta is reported as information; the live session is read-only throughout. Because a rebuild — or the gate's mesh tessellation — can enter an un-interruptible native OCC call that no in-worker SIGALRM can stop, the whole rebuild+gate loop runs **out of process, hard-bounded by the op budget** (matching `export`/`shape_compare`/`render_view`, so it can never SIGKILL the worker), persisting results incrementally so a kill still yields a salvaged partial report; on hosts that block child processes it degrades to an in-process (SIGALRM-bounded) run, and a `max_params` cap bounds the parameter count. Shipped with companion **design-state authoring guidance** (Arko-T §4.3): an "Author for editability" section in the b123d-modeling skill and `default_prompt.md`, and a runnable **design-state authoring** exemplar in the `build123d://quickref` resource (named parameter block with units, base → secondary → finishing construction order) — previously the quickref taught only inline-literal snippets. (#330)
- **Live session viewer over a Unix domain socket (`--viewer-socket PATH`).** Optionally stream the session's geometry to an interactive 3D viewer so a human can watch and rotate the model while an agent drives the MCP, instead of only seeing fixed-viewpoint `render_view` PNGs. When the flag (or `BUILD123D_VIEWER_SOCKET`) is set, the server binds a UDS and, after each geometry-mutating tool (`execute`, `reset`, `restore_snapshot`, `import_cad_file`, `load_part`), broadcasts the **changed** shapes as glTF-binary (glb) to connected viewer clients. The publisher runs on a background daemon thread in the server process and never blocks the agent path: it pays nothing while no viewer is attached (no tessellation, no worker round-trip), tessellates only identity-changed shapes via the existing hard-bounded out-of-process path, and a slow client is throttled (bounded per-client buffers, drop-oldest) rather than the producer. A viewer attaching mid-session gets `HELLO` + a full-scene dump; a worker restart emits `RESET`. The glb encoder is a small self-contained glTF 2.0 writer, so there is **no new dependency** and no OCC/VTK in the encode step. The socket is created mode 0600 and lives outside the sandboxed `execute()` worker. Wire protocol, usage, and a dependency-free reference consumer (`examples/live_viewer_client.py`) are documented in `docs/live-viewer.md`. POSIX only.

## v0.3.59

### Added

- **`shape_compare()` now localizes WHERE the geometry changed, with an exact magnitude.** It kept volume/bbox/topology/center deltas — useful totals, but they never said *where* an edit landed or by how much the surface actually moved, so verifying "raise this block 10 mm" or "chamfer→fillet r3" against an imported reference was guesswork. `shape_compare` now adds a bounded surface diff: a mesh nearest-neighbour pass (both shapes tessellated at a **shared deflection**, with an auto-scaled noise floor so a same-geometry re-export reads ~0 instead of a fabricated multi-mm "change") **locates** the changed region(s), then an **exact B-rep boolean clipped to that region** reports the true surface displacement and exact added/removed **volume** — no flat-face vertex-NN inflation. Output gains `max_deviation` (largest real change), `changed`/`regions` (per-region centroid/bbox + `added_volume`/`removed_volume`), `magnitude_method` (`exact_boolean` = exact displacement *and* volumes; `exact_volume_mesh_displacement` = exact volumes with a mesh-estimated displacement, e.g. a cut/flush-fill whose surface barely moves; `mesh_estimate` = boolean skipped/failed), `unchanged_elsewhere`, and `warnings`. For editing this is **model↔input verification, not a score**. (#313)
- The exact boolean is **hard-bounded**: it tessellates and runs out of process under a budget derived from the exec timeout (matching the worker op budget, so it can't outlive — and SIGKILL — the worker), with an in-process mesh-only fallback on hosts that block child processes. Clipping to the located region keeps it in budget even on a 13 MB part (full boolean >2 min → located+clipped ≈ 88 s); a large/spread edit (clip box >150 mm) skips straight to the flagged mesh estimate, and a mid-run timeout **salvages** the already-computed mesh result rather than discarding everything. A genuinely blind case — a **tangential** move (sliding a hole) or a sub-resolution edit on a very large part — detects no region and emits a warning, so `unchanged_elsewhere` is never mistaken for a guarantee.
- Adds `scipy` as an explicit runtime dependency (`cKDTree` for the nearest-neighbour surface pass).

## v0.3.58

### Added

- **`locate_gate_defects()` — report WHERE a solid fails the validity gate, with 3D coordinates.** `validate()`/`export()` say *what* is wrong ("1 non-manifold edge", "BRepCheck failed") but never *where*, so an agent repairs blind — chamfer here, sew there — burning 50-70 `execute()` calls per fixture (it hit ~29/81 benchmark runs). This returns a per-defect list with real model coordinates and B-rep identity: `brep_invalid_face` (face index + center + BRepCheck status, e.g. an unorientable BSpline), `open_edge` / `nonmanifold_edge` (B-rep edge midpoint + incident-face count), and the mesh self-touches a CAD scorer rejects — `mesh_nonmanifold_edge` (edge midpoint) and `mesh_nonmanifold_vertex` (corner-to-corner touch point, #298) — each with a generic repair hint. An empty list means the part passes the structural checks. The B-rep checks cover every solid of a multi-solid compound (not just the first), matching the gate, so a defect on a later body is never reported as a false "clean". The mesh check tessellates (un-interruptible `BRepMesh`), so — like the export gate and `render_view` — it runs **out of process, hard-bounded by the op budget** (with an in-process fallback on hosts that block child-process creation), and a part too large to mesh-check in budget returns a clean error rather than SIGKILLing the session.
- **Quick reference now advertises build123d 0.11 features (version-gated).** The `build123d://quickref` resource gains a "New in 0.11" section — `ConvexPolyhedron` (solid convex hull), `BSpline` (exact spline edge from control points + knots), and `ConstrainedArcs`/`ConstrainedLines` (constraint-solved sketch geometry), plus pointers to single-line engraving fonts, conic-section arcs, and broader intersection support. The section (and its runnable examples) is shown and tested **only when the installed build123d is ≥ 0.11**, so 0.10 sessions never see APIs they can't call. Gating keys off the version already reported in the resource banner.

### Fixed

- **`export()` no longer fails to write STEP on build123d 0.11.0.** build123d 0.11.0's high-level `export_step` (the `STEPCAFControl_Writer` path) raises `RuntimeError: Failed to write STEP file` on many imported-STEP-derived solids that 0.10.0 wrote fine — `uvx build123d-mcp@latest` picked up 0.11.0 and the failure hit ~38% of editing-fixture benchmark runs (where the agent imports a STEP and exports the valid edited solid), wasting large amounts of the refinement budget and sometimes leaving no output on disk. `export()` now falls back to the basic `STEPControl_Writer`, which writes the same geometry (it only drops CAF labels/colours, which don't affect validity, downstream booleans, or CAD scoring); the geometry round-trips identically. A clear combined error is raised only if both writers fail.
- **`find_hole_patterns()` no longer crashes on unrecognised pattern types.** The wrapper special-cased `BoltCircle` and assumed every other pattern was a `LinearArray`, reaching for `.pitch`/`.direction`. A `build123d_drafting` that returns a `RectGrid` (rectangular hole grid) tripped `AttributeError: 'RectGrid' object has no attribute 'pitch'`, so the agent got no bolt-pattern confirmation at all (seen across benchmark runs). `LinearArray` is now matched explicitly and any other pattern type is tagged by its (snake-cased) class name and serialised generically via its dataclass fields (with `default=str` so a non-JSON field can't crash it) — forward-compatible with new pattern types.
- **`render_view()` can no longer SIGKILL the session by timing out in tessellation.** `render_view` tessellated the shape in-worker via OCC `BRepMesh` — an un-interruptible native call that on a complex part (e.g. a 243 mm / 879-face benchmark solid took >90 s) blows the 120 s op-timeout, so the parent kills the worker and destroys all session state (variables, named objects, snapshots). It hit ~17/81 benchmark runs, wiping the agent's in-progress work. Tessellation now runs **out of process** (`build123d_mcp._tessellate_subprocess` via `subprocess.run` — the worker is a daemon, so `multiprocessing` isolation is unavailable), hard-bounded by a 75 s budget safely under the op-timeout: on overrun the child is killed and a clean error is returned (*"too complex at this quality; try standard / fewer objects"*) with the worker and session intact. Normal renders are unchanged.

## v0.3.57

### Changed

- **Support build123d 0.11 (in addition to 0.10).** The dependency range is now `build123d>=0.10,<0.12`, and CI runs the full test suite against both 0.10 and 0.11 on Linux/macOS/Windows. 0.11 switched build123d's OCP backend to `cadquery-ocp-novtk`, which no longer pulls VTK transitively; since `render_view` drives VTK directly, `vtk` is now declared as an explicit dependency (harmless on 0.10, where `cadquery-ocp` already provides it). Also bumps the floor of the bundled `augura` printability analyzer to `>=0.1.5`, the first release that allows build123d 0.11.
- **Support Python 3.13 and 3.14.** `requires-python` is now `>=3.11,<3.15`. The previous 3.12 cap was a stale assumption that VTK shipped no cp313 wheels — current `vtk` (9.6.2) ships cp313 and cp314 wheels, so the lock now uses it. CI exercises the full suite (incl. the VTK render path) on 3.12/3.13/3.14 for build123d 0.11; build123d 0.10 stays tested at 3.12 (it caps at <3.14). `--python 3.12` remains the recommended default in the README launch examples.

### Fixed

- **Export validity gate now detects non-manifold *vertices*.** The mesh gate checked non-manifold *edges* (shared by >2 faces) and open edges, but missed non-manifold *vertices* — a point where ≥2 surface sheets meet (e.g. two bodies touching corner-to-corner). Such a part is edge-manifold and watertight yet not a 2-manifold surface, which a CAD scorer rejects, so the gate gave a false PASS. The exact check now reports `mesh_nonmanifold_vertices`, computed on a coordinate-welded mesh (seam-safe — so poles/seams of curved solids don't false-positive) by verifying each vertex's incident triangles form a single connected fan. Runs both in-process and in the export subprocess. Caught a real defect (a benchmark cover scored zero by the official gate for exactly this) that previously shipped. (#298)

## v0.3.56

### Fixed

- **Export gate now mesh-checks large/complex parts instead of skipping them.** v0.3.55's mesh check is wall-clock-bounded and returns `mesh_check="skipped"` (B-rep checks only) when a part is too large to tessellate-and-stitch in-process within the worker timeout — so a big invalid part could ship its mesh defect undetected. The dominant cost is OCC `BRepMesh`, an un-interruptible native call an in-process budget can't stop. `export()` now, *only when the in-process check skips*, retries the mesh check in a **separate process bounded by a hard `subprocess` timeout** (sized from `--exec-timeout`): a large part gets a generous budget and is actually checked, an over-budget part is killed cleanly (the worker is never blocked), and small exports pay no subprocess cost. Catches real defects on parts the in-process gate had skipped. (#294)
- **Conformal-stitch mesh check is now fast enough for the largest parts.** The stitch's dominant cost was not the union-find (as expected) but the Python-side iteration of OCC's edge→face ancestor lists (`TopTools_ListOfShape`), which is pathologically slow — ~23 s per rung for a few thousand edges on an 18 MB part, and roughly constant across deflection-ladder rungs. The edge→face adjacency is now built by walking faces' edges with `TopExp_Explorer` (identical adjacency, C-speed), cutting the full exact check on that part from ~150 s to ~18 s. With the stitch fast, the open-edge ladder's triangle ceiling is lifted in the no-time-deadline (export subprocess) path so the finest (`/32`, 500k+ triangle) rung actually runs — letting very large parts that previously timed out be mesh-checked and their open-boundary defects caught out-of-process. Speed-only, behavior-preserving: byte-identical verdicts on the corpus and clean solids; zero false positives across the 76 valid v0352 outputs. (#294)

## v0.3.55

### Fixed

- **Export validity gate now detects mesh non-closure (open edges) and faces that fail tessellation.** The gate previously verified well-formedness, the B-rep edge-face map, and mesh non-manifold edges, but a solid can be a valid B-rep with a matched edge map yet still tessellate to a boundary that is not watertight — a non-conformal face junction the edge map does not see, or a face OCC cannot mesh at all — which a CAD scorer rejects. The exact (export) mesh check now also reports `mesh_open_edges` (tessellated-boundary open edges) and `untriangulated_faces` (faces that failed to mesh), computed by a seam-aware conformal stitch — per-face triangulations merged by topology across shared edges, periodic seams, and B-rep vertices — run over a deflection ladder (a valid periodic/curved seam that reads open at one tessellation density closes at a finer one, while a genuine gap stays open at every density). The ladder is triangle-count-budgeted with the same fast-check fallback as the non-manifold check. The exact check and the fast fallback share one wall-clock budget (kept under the minimum export op timeout), and `export()` now sizes its worker op-budget from `--exec-timeout`, so the strengthened gate can never run past the timeout and kill the session — a part too large to analyse in budget degrades to the cheaper fast / B-rep checks (reported via `mesh_check` and a warning), never to a false FAIL of a valid part.

## v0.3.54 — 2026-06-22

### Features

- **`--no-sandbox` flag (`BUILD123D_NO_SANDBOX`).** Disables all `execute()` sandbox layers — the AST check is skipped and user code runs with unrestricted builtins (`open`/`eval`/`exec`/`__import__`). For trusted, isolated environments only (e.g. a benchmark harness); never expose to untrusted input. The exec timeout is unaffected (use `--exec-timeout`).

### Fixed

- **`hasattr()` is no longer blocked in `execute()`.** It returns only a `bool` and cannot *return* an object, so — unlike `getattr`/`vars` — it can't reach `__class__`/`__subclasses__` to escape the sandbox. Blocking it forced agents into try/except rewrites for ordinary build123d introspection (e.g. `hasattr(part, 'solids')`). `getattr`/`vars` stay blocked (they remain genuine dunder-bypass vectors; use `--no-sandbox` if you need them). (#265)
- **Blocked-call errors no longer emit a misleading "Import blocked" hint.** A `Call to 'x' is not allowed` / dunder-access rejection matched the import-error hint rule, telling the agent to fix a nonexistent import. Call/attribute blocks now get a call-specific hint and a distinct `call_blocked` classification. (#265)

## v0.3.53 — 2026-06-22

### Fixed

- **Export gate validates the written-and-reimported STEP, not the in-memory shape.** `export()`'s validity gate ran on the in-memory shape, but a CAD scorer re-imports the written file — and serialization can degrade a shape that passed in memory (drop a solid, break BRep validity), giving a false PASS while shipping an invalid file. Verified on the sweep corpus: a shape with a solid that passed the gate re-imported as zero solids; another valid-in-memory BRep was invalid in the file — both shipped with a clean gate. The gate now re-imports the just-written STEP and validates that artifact, warning if it can't be re-imported. (#284)

## v0.3.52 — 2026-06-21

### Fixed

- **Validity gate ignores free wire edges (PMI annotation curves).** Edges with no incident face — leader/dimension curves carried by an imported STEP, or stray construction geometry — were counted as open boundaries, false-FAILing clean watertight solids imported from PMI-annotated CAD. Only one-face edges are genuine open boundaries now. (#279)
- **Accurate topology-stitch mesh non-manifold check at export.** The mesh check welded per-face tessellation samples by rounded coordinate; at that tolerance's rounding boundary it could both miss real non-manifold defects and false-flag valid solids (verified against a CAD-scorer corpus: 2 missed, 1 false-flagged). `export()` now uses a tolerance-free check that stitches the per-face triangulations into one conformal mesh by topology (globally consistent winding, shared-edge node-index union-find, degenerate-edge BREP-vertex merge, opposite-winding flap cancellation), matching the scorer's gate exactly. It is triangle-count-budgeted with fallback to the fast check, so interactive `validate()` stays fast; a `mesh_check` field records which ran. (#281, #282)

## v0.3.51 — 2026-06-19

### Features

- **New `validate()` tool — pre-export validity gate.** Reports a `PASS`/`FAIL` verdict plus JSON (`passes_gate`, `n_solids`, `volume`, `watertight_manifold`, `open_edges`, `nonmanifold_edges`, `mesh_nonmanifold_edges`, `brep_valid`, `reasons`, `warnings`) for whether a shape would pass a CAD validity gate. CAD scorers (e.g. CADGenBench) zero any submission that isn't a well-formed, watertight, manifold solid regardless of how close the geometry is, and `measure()` only reports counts/volume — so an agent could build a non-manifold or open solid, "verify" it with `measure()`, and ship a zero with full confidence. `validate()` closes that gap with actionable `reasons` (leftover 2D sketch, open shell, un-fused compound, self-touching faces). The modeling skill now mandates it before export, and `export()` re-runs the gate on 3D output and warns when the written STEP/STL would be rejected. (#276)

### Fixed

- **Validity gate no longer relies on build123d's `is_manifold`**, which false-negates on closed solids imported from STEP (verified on NIST CAD models — a single closed shell with zero open edges still reported `is_manifold=False`). Watertightness/manifoldness is now judged by the edge→face map (every non-degenerate edge shared by exactly two faces) — a gate that false-FAILed valid imported solids would train agents to ignore it. (#277)
- **Mesh-level non-manifold detection** catches self-touching / coincident-face solids that are watertight and BRepCheck-valid but whose tessellated mesh has an edge shared by more than two triangles — the dominant invalid-but-watertight failure mode. Welds per-face tessellation samples first; verified zero false positives on curved and real CAD geometry. (#278)

## v0.3.50 — 2026-06-14

### Features

- **HTTP / ASGI streamable-http transport.** `--transport http` (with `--host` / `--port`, and `BUILD123D_TRANSPORT` / `BUILD123D_HOST` / `BUILD123D_PORT` env overrides) serves the MCP server over HTTP; `http_app()` exposes the FastMCP ASGI app for embedding in an external ASGI application. Per-request session isolation via a `contextvars`-based resolver lets a host run a separate `WorkerSession` per `(user, project)`. Default stdio behaviour is unchanged. (#268, #272)
- **Per-worker resource limits.** `--memory-limit-mb` (`RLIMIT_DATA`) and `--cpu-limit-s` (`RLIMIT_CPU`) bound a worker's memory and CPU (POSIX; no-op with a warning on macOS/Windows). Plus a configurable per-tenant filesystem root for multi-tenant hosting. (#268, #273)

### Dependencies

- `mcp>=1.9` (for `streamable_http_app()`); `uvicorn` added as the `[http]` optional extra. `build123d-drafting-helpers>=0.10.0`. `draftwright` added to the sandbox import allowlist (bring-your-own AGPL drawing engine). (#270, #271)

## v0.3.49 — 2026-06-12

### Features

- **Guidance refresh for build123d-drafting-helpers v0.7.0** (#264 follow-up):
  the `b123d-drawing` skill and drafting cookbook no longer tell the agent to
  clear prismatic auto-annotations and hand-place dimensions — `make_drawing`
  now emits grouped hole callouts, bolt-circle/array patterns, centre marks,
  baseline location dims, and automatic sections itself; the hole-table recipe
  demonstrates `find_holes` instead of hand-rolled face scans. Dependency
  floor raised to `build123d-drafting-helpers>=0.7.0`.
- **`find_hole_patterns` tool** — bolt-circle / linear-array recognition over
  the hole records (`{type, holes, center/diameter | pitch/direction}`).
- **`find_holes` / `find_bosses` tools** — feature recognition on session objects via build123d-drafting-helpers ≥ 0.6.0 (#264). Coaxial internal cylinders are grouped into one record per drilled hole (drill + counterbore + spotface; keyway-split and crossing-hole-interrupted bores count once) with axis, opening location, diameter, depth, and bottom classification (`through` / `flat` / `drill_point` / `unknown`); `find_bosses` reports external segments with height. Replaces the hand-rolled `BRepAdaptor_Surface` hole detection that dominated the NIST CTC-02 benchmark session.

## v0.3.48 — 2026-06-11

### Features

- **`save_json(name, obj)` sandbox helper** — the sanctioned structured-output channel for `execute()` (#259). JSON-serializes analysis data (face inventories, hole tables, section data) to a per-process server scratch dir with a validated stem and 10 MB cap, returning the path for the caller to read back. `open()`/`os` remain blocked.
- **`build123d://drafting-api` MCP resource** — API reference for build123d-drafting-helpers generated from the installed library with `inspect`, so signatures never drift across releases (#260). Pointed to from the drawing skill.
- **`suggest_view_layout` now reports per-view `free_space` bands** — the empty rectangle outside each view edge, clipped against neighbouring views, the title block, and the page margins, so agents can budget dimension tiers before placing annotations (#261).

### Fixed

- **`save_drawing_annotations` no longer writes an empty sidecar** when the session has no registered annotations (e.g. the drawing was built by a standalone regeneration script) — it returns a warning explaining where the annotations live instead (#258). Path validation still runs first, so invalid destinations fail loudly regardless.

### Documentation

- Drawing skill: sidecar limitation for script-built drawings, prismatic-part caveat on `make_drawing`'s automatic annotations, and a pointer to `build123d://drafting-api`.

---

## v0.3.47 — 2026-06-11

### Fixed

- **`render_view` azimuth/elevation now respect the view's up vector.** The camera-orbit math previously assumed Z-up for every direction preset, so `azimuth`/`elevation` produced wrong rotations on the top view. Both rotations now use Rodrigues' formula about the correct axes (up for azimuth, camera-right for elevation, mirroring VTK's `Azimuth()`/`Elevation()`), with the up vector carried along. Z-up views (front/side/iso) are bit-for-bit unchanged. (#222, #249)

### Changed

- **Removed the deprecated `interference` tool.** `clearance()` is the replacement and reports strictly more (status, containment, overlap volumes). Breaking only for clients still calling `interference`. (#217, #251, #253)
- **`render_view`'s docstring trimmed ~220 words** to cut per-request token cost; all parameter documentation retained. (#217, #251)

### Internal

- **Worker ops are now a single table.** The dispatch if-chain and ~28 hand-written `WorkerSession` proxy methods collapsed into one `_OPS` table populated by `@_op`-decorated typed stub methods — one definition site per op (signature, timeout, worker handler together) with full mypy/IDE signatures preserved. New tests pin that every op is reachable and that stub defaults match the tool-function defaults. (#220, #252)
- Deduplicated the syntax-excerpt and clip-plane-split logic. (#221, #250)

### Documentation

- README's Tools section now lists all 31 registered tools (the drawing-tooling and printability families were missing); the drawing skill's Verify step covers `save_drawing_annotations` + `inspect_drawing`. (#255)

---

## v0.3.46 — 2026-06-10

### Features

- **`--in-process` mode for MCP hosts that block subprocess creation.** Under some sandboxed hosts (reported with Codex desktop on Windows, #143) the worker subprocess never starts and every `execute()` fails. `--in-process` / `BUILD123D_IN_PROCESS=1` runs the CAD session inside the server process with the full tool surface. Trade-offs, stated plainly: no crash containment and no execution timeouts on Windows. The worker-startup failure message now reports the worker's exit code and points at this flag. (#143, #248)

---

## v0.3.45 — 2026-06-10

### Features

- **New `b123d-modeling` skill** — the full build-from-drawing/spec workflow (spec extraction, incremental build, numeric-then-visual verification, snapshots, heavy-build escape hatch, finish/export). Installable via `install_skill(skill="modeling")` for Claude Code, Cursor, Windsurf, or AGENTS.md, and readable as the `build123d://skill/modeling` resource. (#233)
- **Server now ships MCP `instructions`** telling clients to use these tools for any task that builds, modifies, measures, or renders 3D geometry — fixes agents ignoring the server when tool schemas are deferred and only the name + instructions are visible. (#233)
- **`measure()` reports mass and physical inertia.** Pass `density` (g/cm³) or a `material` preset (steel, stainless, aluminum/6061, brass, copper, titanium, abs, pla, petg, nylon) to get `mass_g` and the inertia tensor scaled to true mass moments; the new `inertia_units` field disambiguates g·mm² vs the volume-inertia default. (#237, #243)
- **`find_edges()` session helper** — `find_edges(shape, geom="circle", radius=4.25, at_z=10.2)` selects fillet/chamfer edges on turned parts without hand-rolled filtering, returns a ShapeList that feeds straight into `fillet()`, and prints the match count, radii, and Z levels. (#239, #245)
- **`export()` echoes a final sanity line** — volume/bbox/face count of the written solid (bbox/edge count for 2D), so the usually-last tool call confirms the right, non-degenerate object landed in the file. (#241, #244)
- **Companion packages are discoverable**: `version()` lists `bd_warehouse` and `augura`, and `workflow_hints()` states bd_warehouse ships with the server — agents stop hand-rolling threads, fasteners, and gears. (#240, #244)

### Fixed

- **False "volume ≈ 0 — degenerate" warning when a sketch was left over.** An explicit `show()`/`annotate()` registration now wins `current_shape` over the post-exec variable scan (which iterated an unordered set and could land on the revolve's leftover sketch), and also over a stale `result` variable from an earlier call. Live 2D/1D geometry gets a "no solid volume" note instead of the failed-boolean warning, and the remaining scan is deterministic. (#236, #242)
- **`measure()` face inventory cut to signal.** Identical faces collapse into one entry with a `count` (`4× Ø6.6 THRU` → one cylinder entry, count 4) and non-analytic sliver faces (thread fades) fold into a single summary line; analytic faces keep diameters/axes verbatim. An 82-face thread part drops from ~40 noise lines to a handful. (#238, #243)
- **Modeling skill no longer uses maintainer-specific `[SEND:]`/`[ASK:]` markers** — instructions are spelled out in plain English so the skill works on any client; a regression test keeps shipped skills marker-free. (#246)

### Documentation

- Modeling skill Step 6 hands off to estampo when the project slices with it (`estampo.toml` present): update the `[[parts]]` entry, seed overrides from the printability report, run `estampo run`. (#234)

### Dependencies

- augura floor raised to 0.1.3. (#235, #247)

---

## v0.3.44 — 2026-06-10

### Fixed

- **Heavy imports no longer hit a fixed 60 s session-fatal timeout.** `import_cad_file` and `load_part` now run under `max(default, exec_timeout)`, so the default budget rises to 120 s and the existing `--exec-timeout` / `BUILD123D_EXEC_TIMEOUT` knob covers threaded STEP files and heavy library parts (threads, gears). Previously one slow import SIGKILLed the worker and destroyed every shape, named object, and snapshot. (#229, #231)
- **`suggest_view_layout` works without a session object.** New optional `extents=[x, y, z]` (+ `centroid`) computes the layout from raw bounding-box sizes — the page math never needed live geometry, so a failed import no longer takes the layout tool down with it. The drawing skill's manual pipeline documents the fallback. (#229, #231)
- **Drawing skill no longer collides with an existing `scripts/drawings.py`.** Step 4 now tells the agent to pick a non-colliding path such as `scripts/<part>_drawing.py` when a conflicting name exists, and the Cursor rules glob covers the alternative. (#230, #231)

---

## v0.3.43 — 2026-06-10

### Dependencies

- **build123d-drafting-helpers floor raised to 0.4.2.** The drawing skill relies on `choose_scale()` for page/scale selection and on `view_annotation_overlap` lint results; 0.4.2 adds ISO 5455 enlargement scales (10:1, 5:1) so small precision parts get legible drawings, and stops the overlap lint false-positiving on centrelines and witness lines. (#232)

---

## v0.3.42 — 2026-06-09

### Documentation

- **MCP client config examples fixed for recent uv.** `uv tool run --upgrade` is ignored by recent uv (≥ ~0.9) and warns on every launch, leaving clients silently pinned to uv's cached version. All five client examples now use the `@latest` specifier (`uv tool run --python 3.12 build123d-mcp@latest`), which re-resolves to the latest release per launch. If your MCP JSON still passes `--upgrade`, swap it for `@latest`. This release also refreshes the PyPI project page, which carried the old advice. (#228)

---

## v0.3.41 — 2026-06-09

### Features

- **New `analyze_printability` tool** — FDM printability analysis via augura (BREP-exact): overhangs, manifold/watertight, tip-over risk, brim/raft need, minimum vertical feature (→ max layer height), thin walls, and optional bed-fit against a declared build volume. Returns a plain-text summary plus a JSON report with per-finding detail. (#213)

### Fixes

- **Slow geometry queries no longer destroy the session.** `measure`, `clearance`, `cross_sections`, `shape_compare`, `align_check`, `analyze_printability`, `save_snapshot`, `diff_snapshot`, `resolve`, `suggest_view_layout`, and `load_part` now get a 60 s budget (previously 10 s — a timeout SIGKILLs the worker and loses all session state). (#214, #226)
- **Worker restart errors now say that session state was lost** — every restart path (op timeout, dead-worker detection, mid-call crash) tells the client to re-run setup code instead of leaving it referencing dead object names. (#215, #226)
- **macOS VTK render subprocess guard lowered to 100 s** so it fires before the parent's 120 s poll where it applies (partial fix; #216 remains open for the in-worker path). (#226)
- **`session_state` filters the injected helpers (`annotate`, `named_face`, `set_page`, `register_centerline`) via the canonical `_INJECTED` set** instead of an accidental module-prefix match. (#219, #227)

### Documentation

- Security docs corrected: the `inspect` allowlist no longer claims it "cannot execute code" (introspection chains through `getmembers()` are an accepted known limit), and the README/CLAUDE.md now state explicitly that `--library` part files are trusted input. (#224, #225, #227)

---

## v0.3.40 — 2026-06-09

### Features

- **`execute()` now appends a session objects summary** to every successful result, e.g. `Session objects: part (Solid, 14 faces), sketch (Sketch)`. LLM agents no longer need a separate `session_state()` call to know what named shapes exist. (#206)

### Fixes

- **Suppress VTK Cocoa warning spam on macOS** — `GlobalWarningDisplayOff()` is now called before the first VTK object is created, silencing "Failed to get alpha color buffer size" noise that escaped stderr redirection. Scoped to macOS only; Linux retains full VTK diagnostic output. (#208)

### Refactoring

- **`server.py` split into focused modules** (#183):
  - CLI/startup logic (`main()`, `install-skill` subcommand) extracted into `build123d_mcp/cli.py`
  - Render result-marshalling (tempfile, `ImageContent`, `[SEND:]` markers) extracted into `tools/_marshal.py`
  - `server.py` is now registration-only (~650 lines, down from ~890)

---

## v0.3.39 — 2026-06-08

### Security

- **Hardening pass over the execution sandbox and file I/O (audit issues #179–#189).**
  - `resolve()` routes its selector through the `execute()` sandbox (AST allowlist +
    restricted builtins), closing an `eval` escape. (#186)
  - The dunder-attribute block stays active under `--allow-all-imports`. (#187)
  - State-dependent tools (`align_check`, `resolve`, `script`, `session_state`,
    `suggest_view_layout`) route through the worker so they see real session state,
    with a production-boundary coverage guard. (#179, #182)
  - File writes and reads — including `.dims.json` sidecars — are constrained to the
    allowed roots. (#180, #188)
  - Oversized SVG/CAD inputs and extreme raster widths are rejected before the
    expensive work; SVG parsing is hardened against XML entity-expansion
    ("billion laughs") via `defusedxml`. (#189)

### Changed

- **Adopted Ruff** for formatting and linting (`F`/`I`/`UP`/`C4`), enforced in CI;
  the codebase was reformatted to match. (#185)
- `server.json` registry version is kept in sync with the package version. (#181)

### Fixed

- **macOS:** `render_view` isolates VTK rendering in a subprocess to avoid a
  window-server freeze. (#198)

### Packaging

- Added per-version Python trove classifiers (3.10–3.12). (#178)
- Use the canonical Apache-2.0 LICENSE text for reliable license detection. (#177)

## v0.3.38 — 2026-06-07

### Changed

- **`b123d-drawing` skill now defaults to saving a standalone regeneration
  script.** After generating a drawing, the agent writes a clean, committable
  `scripts/drawings/<part>.py` (via `generate_script()` for STEP inputs, or a
  hand-written rebuild + `make_drawing` for in-session objects) so drawings live
  in version control as reproducible code, not only as output artifacts — unless
  the user opts out. Restores the `scripts/drawings/` convention dropped in the
  v0.3.37 rewrite. Closes #175.

## v0.3.37 — 2026-06-07

### Changed

- **`b123d-drawing` skill now leads with `make_drawing()` / `build_drawing()`.**
  The default path is the automatic one-call pipeline; the `build_drawing()`
  builder is documented for in-place customisation (add/remove dimensions, add
  section views); the hand-built projection pipeline is retained as a clearly
  labelled fallback for cases the builder cannot express. Requires
  `build123d-drafting-helpers >= 0.4.1`.

### Build

- Bumped `build123d-drafting-helpers` floor to `>=0.4.1` (adds `make_drawing`
  object input, `build_drawing` / `Drawing` builder, and the UTF-8 script fix).

## v0.3.36 — 2026-06-05

### Added

- **Transitive-safe import checking** — pure-Python packages installed on `sys.path`
  whose full import closure lies within the security allowlist are now importable without
  `--allow-imports`. The checker walks every `.py` source file in the transitive closure
  and blocks anything that reaches `os`, `subprocess`, `socket`, etc. Closes #170.

### Fixed

- **Relative import bypass closed** — `from . import X` inside a transitively-checked
  package was previously skipped, allowing a submodule that imports `os` to slip through.
  Relative imports are now resolved to absolute names and checked recursively.
- **Parent `__init__.py` now checked** — `from mypkg.utils import X` previously only
  verified `utils.py`, not `mypkg/__init__.py`. Since `__init__.py` runs at import time
  with real builtins, a malicious parent package could bypass the sandbox. Parent packages
  are now checked before their submodules.

## v0.3.35 — 2026-06-04

### Added

- **`suggest_view_layout`** — new MCP tool that auto-calculates `VIEW_X`/`VIEW_Y`
  positions for a standard four-view third-angle engineering drawing. Returns per-view
  page positions, `look_at`, camera/up vectors, fit warnings, and a scale/page
  suggestion if the layout doesn't fit. Front/plan/side positions are exact;
  iso is an approximation (caveat documented). Closes #162.
- **`view_axes`** now returns `look_at_offset` and `helper_snippet` — the look_at
  world component per page axis and ready-to-paste coordinate helpers that incorporate
  the centroid offset. Eliminates the systematic annotation shift caused by omitting
  the look_at term. Closes #158.

### Fixed

- **`execute`** now appends `# vars: key=val, ...` to each successful execution's
  output, listing new/changed scalar variables. Makes repeated similar calls produce
  distinct output, preventing Claude Code's context compression from collapsing
  stale results into indistinguishable `<<ccr:...>>` references. Closes #161.

## v0.3.34 — 2026-06-04

### Added

- **`install_skill` MCP tool** — any MCP-capable agent (Claude Code, Codex CLI,
  Antigravity, Cursor, Windsurf) can call `install_skill(target, force=False)` to
  write the b123d-drawing workflow into the current project. Supported targets:
  `claude` (`.claude/skills/`), `agents-md` (`AGENTS.md`), `cursor`
  (`.cursor/rules/b123d-drawing.mdc`), `windsurf` (`.windsurfrules`).
- **`build123d://skill/drawing` MCP resource** — exposes the full drawing workflow
  for agents to read without installing.
- **`build123d-mcp install-skill --target <agent>`** — CLI gains `--target` flag;
  defaults to `claude` (backward-compatible).
- **`workflow_hints()`** now mentions `install_skill` and the skill resource in the
  2D drawings section.

### Fixed

- Cursor `.mdc` `globs` field was emitting an invalid YAML block list; corrected to
  a quoted comma-separated string so path scoping actually works.
- CLI `install-skill` exit logic replaced fragile `"already" in message` string-match
  with a `_dest_exists()` pre-check.
- Claude Code-specific markers (`[SEND:]`, `[ASK:]`) are stripped when writing
  `agents-md`, `cursor`, and `windsurf` targets.

## v0.3.33 — 2026-06-03

### Added

- **`build123d-mcp install-skill`** — new CLI subcommand that copies the bundled
  `b123d-drawing` Claude Code skill into `.claude/skills/b123d-drawing/` of the
  user's current project. The skill ships inside the PyPI wheel via
  `importlib.resources`; use `--force` to overwrite an existing installation.
- **`b123d-drawing` Claude Code skill** — step-by-step workflow for creating
  engineering drawings from build123d geometry (views, scale/page-size heuristic,
  annotation, lint gate, SVG/DXF/PDF export).

### Fixed

- Skill: added adaptive scale and page-size heuristic (A4 2:1 → A3 1:2 based on
  bounding box); parameterised `PAGE_W`/`PAGE_H` throughout including the PDF
  `pdf_y` formula which was previously hardcoded to A4.
- Skill: added `ExportDXF` code example, clarified `lint_drawing` is a Python
  library call not an MCP tool, added empty-compound guard after
  `project_to_viewport`, and resolved isometric camera position ambiguity.

## v0.3.32 — 2026-06-02

### Added

- **`lint_drawing(drawing_scale=...)`** (#147) — the lint tool now accepts a
  drawing scale so N:1 drawings of small parts lint cleanly. When the geometry
  was scaled up before projecting (e.g. `part.scale(5)` for a 7.5 mm feature
  drawn at 5:1), pass the same factor and the label-vs-measured check divides
  each measured length by it, so labels carry the *real* dimension instead of
  every dim tripping a false axis-swap warning. Threaded through the tool, the
  worker IPC, and both the session and SVG-sidecar lint paths; defaults to 1.0.

### Changed

- **Bumped `build123d-drafting-helpers` pin to `>=0.3.0`**, which ships the
  `drawing_scale` support above plus `set_page`/`annotate` package exports and a
  page-bounds check (#148), the stacked-dimension `annotation_overlap` fix
  (#149), and title-block page-overflow detection (#151).

### Documentation

- Cookbook (`build123d://drafting`) documents `drawing_scale` for scaled
  drawings and the matching `TitleBlock(drawing_scale=...)` printed indicator.

## v0.3.31 — 2026-06-02

### Documentation

- **Drafting guidance hardened for first-pass correctness.** `default_prompt.md`'s 2D
  section and the `build123d://drafting` cookbook now lead with the v0.2.0 helper *classes*
  (`Dimension`, `Leader`, `Centerline`, GD&T frames, `TitleBlock`) instead of the removed
  0.1.x functions / raw `ExtensionLine`, and the canonical examples were converted to match.
- **Added an engineering-drawing *conventions* section** (which views, projection angle,
  dimensioning scheme — locate each feature once, baseline vs chain, dimension to functional
  faces, hole callouts, basic dims for position tolerances) — the judgement rules the cookbook
  previously lacked.
- **Codified the gate**: build → `view_axes` → project → dimension → `annotate` → `set_page`
  → `lint_drawing()` **== 0 violations** → render → export.
- Fixed stale advice: the GD&T recipe no longer references the removed `.lines`/`.text` split;
  the hole example uses a proper `⌀` Leader callout (read from the face) instead of a
  repurposed `DimensionLine`; `set_page` margin comment and `ø`→`⌀` corrected.

## v0.3.30 — 2026-06-01

### Added

- **`version` MCP tool** now reports the server *and* its render-path dependencies
  (`build123d`, `build123d-drafting-helpers`), not just the server — "is this current?"
  usually needs all three. Computed **in-process** (pure `importlib.metadata`, same venv
  as the worker), so it answers even when the build123d worker subprocess is down — the
  stale / broken-install case the tool exists to diagnose.

### Dependencies

- **`build123d-drafting-helpers` pin bumped `>=0.1.13` → `>=0.2.0`** — the helpers are now
  native build123d `BaseSketchObject`s (the builders are classes: `Dimension`, `Leader`,
  `FeatureControlFrame`, `DatumFeature`, `DatumTarget`, `SurfaceFinish`, `HoleCallout`,
  `CompositeFeatureControlFrame`, `TitleBlock`, `Centerline`, `SafeDimension`). The drafting
  cookbook and `inspect_drawing`/`lint_drawing` examples are updated to the class API; a
  drawing now exports on a single ink layer (no `.lines`/`.text` split).
- **`lint_drawing` session-mode rewired for helpers 0.2.0.** The `*Result` dataclasses are
  gone, so it now feeds the helpers' duck-typed linter lightweight stand-ins built from the
  stored annotation metadata (`label` / `label_bbox` / `segments` / `elbow` /
  `measured_length`), borrowing the live geometry's `bounding_box`. `annotate()` captures the
  objects' `.label` (renamed from `.label_str` upstream) and precomputed `.segments`, so the
  geometry-precise interference check stays fast (no live edge re-extraction).

## v0.3.29 — 2026-06-01

### Dependencies

- **`build123d-drafting-helpers` pin bumped `>=0.1.11` → `>=0.1.13`**, picking up the
  GD&T completions: basic (theoretically-exact) dimensions (`dim_linear(basic=True)`),
  datum targets (`datum_target`), composite feature control frames
  (`composite_feature_control_frame`), hole callouts (`hole_callout` — ⌀ ⌴ ⌵ ↧),
  all-around / all-over leaders (`leader(all_around=…)`), and the
  `find_interferences(obstacles=…)` label-over-geometry check with the vertical-dim
  `label_bbox` fix. Drawing scripts run via `execute` can now use these directly.

## v0.3.28 — 2026-06-01

### Changed

- **`lint_drawing` (session mode) now delegates to the helpers** instead of
  reimplementing the geometry checks. It reconstructs `DimResult`/`CenterlineResult`
  from the session and calls `build123d_drafting.lint_drawing()` +
  `find_interferences()`, mapping each `LintIssue.code` to the violation `check`.
  Single source of truth — the duplicated label-vs-measured / overlap / centerline
  logic is gone. New geometry-precise checks (`line_pierces_label`, `redundant_lines`,
  `labels_overlap`) are now surfaced through the MCP tool for the first time. The
  **leader check is also delegated** — reconstructed from the stored `label_bbox`
  (which fixes a latent bug: the old whole-leader-bbox check always contained the
  elbow, so it could false-fire on every leader). Only the per-edge page-bounds check
  stays MCP-native.
- **SVG-mode check re-purposed** `text_no_fill` → **`native_svg_text`**: build123d
  renders text as glyph *paths*, never `<text>`, so any `<text>` in an exported SVG
  means it won't survive a DXF export / won't scale — flagged regardless of fill.

### Dependencies

- **`build123d-drafting-helpers` pin bumped `>=0.1.7` → `>=0.1.11`**, picking up the
  `surface_finish_mark` ISO-1302 fix, `add_to_layers()` SVG routing, `find_interferences()`
  geometry-precise collision detection, `draft_preset()`, `LintIssue.code`, and
  `LeaderResult.label_bbox`.

## v0.3.27 — 2026-05-31

### Documentation

- **GD&T drafting recipe**: the drafting cookbook now documents `feature_control_frame()`, `datum_feature()`, and `surface_finish_mark()` (a runnable `gdt_symbols` example), and the "no GD&T symbols" limitation note is removed. The presentation cookbook's "use the heavier path for GD&T" wording is corrected — the drafting helpers cover feature control frames, datum features, and surface-finish marks.

### Dependencies

- **`build123d-drafting-helpers` pin bumped `>=0.1.5` → `>=0.1.7`**, which is the release that adds the GD&T symbol helpers (ISO 1101 feature control frames, ISO 5459 datum features).

## v0.3.26 — 2026-05-21

### Features

- **`align_check(object_a, object_b, axis, mode)`**: deterministic alignment verification along X/Y/Z — `flush` (signed bbox-face delta), `center` (centroid offset), `clearance` (gap). Returns structured JSON with an `interpretation` field.
- **`resolve(object_name, selector, label)`**: evaluates a build123d selector against a named object and returns structured JSON including an `@cad[object#label]` reference. Named refs stored in `session.geometry_refs`.
- **`script(save_to="")`**: exports the session's `execute_history` (all successful `execute()` calls) as a standalone runnable Python file.
- **`failure_class` in execute() errors**: every error response now includes a stable `failure_class` key (`boolean_fail`, `syntax_error`, `selector_empty`, `fillet_fail`, `timeout`, `import_blocked`, `unknown`) plus a `suggested_fix` hint.
- **Validation protocol docs**: `default_prompt.md` and `llms.md` updated to codify measure-before-render order, post-assembly clearance check, and source-vs-derived rule.

---

## v0.3.25

### Features

- **Annotation overlap detection** (`annotation_overlap` lint check): `lint_drawing()` now flags annotation pairs whose bounding boxes overlap by >0.5 mm in both axes at the same Y level. Uses `dim_level_y` metadata (Y coordinate of the actual dim line, stored by `annotate()`) to skip stacked dims whose extension lines legitimately share an X range — eliminates false positives from witness lines.
- **Page-bounds detection** (`annotation_out_of_bounds` lint check): `set_page(width, height, margin=5)` registers the drawable area; `lint_drawing()` flags any annotation whose bbox extends past the margin. `session.reset()` clears the page.
- **Centreline-label overlap detection** (`label_centerline_overlap` lint check): `register_centerline(shape, name)` tags a shape as a centreline; `lint_drawing()` checks whether any dim's label bbox crosses it, using the precise text extent rather than the full annotation bbox. Suggests `label_offset_x` or a leader as fixes.
- **`label_offset_x` propagated to session**: `annotate()` now extracts `label_bbox` and `dim_level_y` from `DimResult` (set by `build123d-drafting-helpers` ≥ 0.1.3).

### Documentation

- **`build123d://drafting` resource** updated with `place_dims`, `place_labels`, `centerline`, `register_centerline` workflow; centreline-label collision avoidance section.
- **`default_prompt.md`** adds unmissable `pip install build123d-drafting-helpers` block with `ModuleNotFoundError` guidance; step 3 names placement helpers first.

---

## v0.3.24

### Bug fixes

- **`view_axes` no longer times out on `look_at=[0,0,0]`** (#114, #122): `tools/view_axes.py` was importing `build123d_drafting`, which loads OCC symbols at module level. On a fresh worker subprocess (before any `execute()` call), the cold-start exceeded the 10 s `SHORT_TIMEOUT`. Fixed by inlining the pure Python math directly — no OCC import, no timeout.
- **`annotate()` no longer produces false-negative lint results** (#119, #121): calling `annotate(vanilla_el, "name")` without `label=` was setting `label_str = str(round(measured_length, 1))`, making lint always see label == measured regardless of what label the `ExtensionLine` was built with. `label_str` is now left absent when we can't extract it, so lint skips the check rather than falsely approving a drawing with an axis-swap bug. Pass `label=` explicitly or use `dim_linear()` for full lint coverage.
- **`leader()` line no longer strikes through label text** (#120): the horizontal shelf was extended by `gap + text_w + gap`, making it run through the full width of the label. Fixed in `build123d-drafting-helpers` 0.1.2: shelf length is now `gap` (a short stub ending where the text starts).

### Documentation

- **Default system prompt steers AIs toward `build123d.drafting`**: added an explicit 2D drawings section prohibiting `reportlab`/`matplotlib` and directing AIs to read `build123d://drafting` first; added an MCP resources table so AIs know all five resources exist without being pushed.

---

## v0.3.23

- **`lint_drawing(svg_path=…)` now uses the sidecar** (#118): when a `.dims.json` sidecar exists alongside the SVG (written by `save_drawing_annotations()`), the label-vs-measured and leader checks run against the sidecar annotations — the same axis-swap detection as session mode. Makes `save_drawing_annotations` + `lint_drawing(svg_path=…)` a complete out-of-band lint flow usable from CI without a live session.

---

## v0.3.22

Bug fixes and tooling improvements for the drawing workflow.

### Bug fixes

- **`view_axes` no longer times out** (#114): `view_axes` was forwarding to the worker subprocess which cold-imported the OCC kernel, blowing the 10 s `SHORT_TIMEOUT`. Fixed in `build123d-drafting-helpers` 0.1.1: `view_axes` is now pure Python arithmetic with no OCC dependency.
- **`annotate()` auto-derives label from measured length** (#115): vanilla `build123d.ExtensionLine` does not expose the constructor label string after construction (`.label` is always `''`). `annotate()` now auto-derives `label_str` from `round(measured_length, 1)` when no explicit `label=` kwarg is passed. Pass `label="40"` explicitly or use `dim_linear()` from `build123d_drafting` when a custom label differs from the measured length.

### Features

- **`save_drawing_annotations(svg_path)` tool** (#116): writes `<svg_path>.dims.json` alongside an SVG with the session's `drawing_annotations` metadata. `inspect_drawing(svg_path=…)` reads the sidecar automatically and includes `annotations` + `annotations_note` in the response, restoring label content that is otherwise irrecoverable from build123d SVG output (text is rendered as glyph paths, not `<text>` elements).

---

## v0.3.21

Drawing-side fixes and feature landing. The four issues raised against 0.3.20 are all addressed, plus the helper library is now a proper PyPI dependency rather than a git-URL dev pin.

### Bug fixes

- **`inspect_drawing` no longer crashes with `'WorkerSession' object has no attribute 'objects'`** (#105 → #109). The tool was being called with the parent-side IPC proxy as if it were the in-process `Session`. Routed it through `worker._dispatch` like every other tool. Regression test goes through `WorkerSession`, not bare `Session`, so this class of routing bug can't recur silently.
- **`from build123d_drafting import …` works out of the box** (#106 → #110). The helper library was on the import allowlist but not actually installed at runtime — its `inspect_drawing` docstring and the drafting cookbook both promised a workflow users couldn't run. Now bundled as a runtime dependency (`build123d-drafting-helpers>=0.1.0`, published to PyPI). Install name and import name deliberately differ; existing call sites keep working unchanged. Regression test reads installed-package metadata so a future move back to dev-only fails the suite.
- **`annotate()` accepts vanilla `build123d.ExtensionLine` / `DimensionLine`** (#107 → #111). The previous attribute-lookup list (`label_str`, `measured_length`, `tip`, `elbow`) matched the helper-library result types only, so existing drafting codebases using upstream primitives got empty metadata blocks. Now reads `.dimension` (set by build123d itself) for measured_length, and accepts an explicit `label="…"` kwarg — build123d does **not** store the constructor label anywhere on the shape after `__init__`, so this is the honest mechanism. Helper-library flows are unchanged.

### Features

- **Drawing-side MCP tooling** (#108 → #112). Four new tools closing the build → review → fix loop for 2D drawings the same way 3D parts already work:
  - **`render_drawing(svg_path, width=1200, save_to=…)`** — rasterise an SVG file written outside the sandbox (e.g. by a short script that did the `ExportSVG` call directly). The PNG is returned inline so the LLM can see the drawing without you opening it in another tool. Uses the existing `resvg-py` runtime dep.
  - **`inspect_drawing(svg_path=…)` mode** — parse an SVG and report page size, layer ids, text content + positions, and element counts. Decouples inspection from the build-and-register ceremony; works on SVGs from any source.
  - **`lint_drawing(svg_path="")`** — standalone tool extracting the inline lint from `inspect_drawing` and adding an SVG-mode check for `<text>` elements without `fill` (the single most common SVG drafting bug — glyphs render as illegible thick outlines).
  - **`view_axes(viewport_origin, viewport_up, look_at)`** — analytic world→page axis mapping for a `project_to_viewport` call. Use BEFORE rendering to catch bottom-view / side-view axis swaps before they show up in the output. Wraps the helper library's existing `view_axes` function.
- **`build123d://drafting` cookbook gains a Drafting conventions section** (#108 → #112). Five recurring failure modes (offset-sign convention, label-too-long crash, text-without-fill, leader-needs-gap, view-axis swap) each paired with the helper or lint tool that catches them.

### Dependency change

- **`build123d-drafting-helpers>=0.1.0`** added as a runtime dependency (was a dev-only git-URL pin previously). Package install name is `build123d-drafting-helpers`; import name stays `build123d_drafting`. The dev-only pin and the `[tool.uv.sources]` git pointer are removed — the helper resolves from PyPI normally now.

---

## v0.3.20

Drawing annotation tooling: a companion helper library, an inspection tool, and sandbox access for drawing scripts.

### Features

- **`build123d-drafting` helper library** ([pzfreo/build123d-drafting-helpers](https://github.com/pzfreo/build123d-drafting-helpers)): pure-build123d helpers that address the rough edges in `build123d.drafting` — named-side `dim_linear`, crash-safe `safe_dim_line`, from-scratch `leader`, analytic `view_axes`, drawing linter `lint_drawing`, `iso_title_block`, and `surface_finish_mark`. Install with `pip install git+https://github.com/pzfreo/build123d-drafting-helpers.git`. Documented in the `build123d://drafting` cookbook.
- **`inspect_drawing` tool**: reports bounding boxes, edge/face counts, and annotation metadata (label string, measured length, tip/elbow for leaders) for every object in the session. Includes an inline linter that flags label-vs-measured-length divergence > 0.5% and leader lines passing through their label text. Returns structured JSON so the LLM can verify a drawing before exporting.
- **`annotate()` session builtin**: companion to `show()` for drawing objects. `annotate(dim_result, "width")` registers the shape in `session.objects` AND stores its `DimResult`/`LeaderResult` metadata in `session.drawing_annotations`, which `inspect_drawing` then reads back.
- **`build123d_drafting` allowed in sandboxed code**: the security allowlist now includes `build123d_drafting` so LLM-generated drawing scripts can `from build123d_drafting import dim_linear, leader, …` without hitting the import block.
- **`build123d://drafting` cookbook updated**: prominent section at the top covering the GitHub install line, all six helpers, and a worked pipeline example (view_axes → dim_linear → annotate → inspect_drawing).

---

## v0.3.19

Two bug fixes.

### Bug fixes

- **SVG renders no longer break the Claude API session** (#101): SVG output was returned as `ImageContent(mimeType="image/svg+xml")`, which the Claude API rejects with `400 Could not process image`. Once this content landed in conversation history every subsequent message — including simple greetings — failed with the same error, making the session unusable. SVGs are now delivered only via the `[SEND: path]` file marker, matching how DXF output was already handled.
- **Library index rescans correctly after partial indexing** (#100): `_LibraryIndex._last_scan` now tracks the maximum mtime of actually-indexed files rather than `time.time()` at scan completion. Previously, any file written between scan-start and scan-end could be missed on the next incremental scan.

---

## v0.3.18

This release lands the **`build123d://presentation` cookbook** for design-discussion diagrams plus four follow-up improvements driven by feedback from a real LLM-driven drafting session (#92). The 2D drawing workflow is now substantially more usable for presentation-quality output.

### Features

- **`build123d://presentation` cookbook** (#93): a sister resource to `build123d://drafting`, focused on design-discussion diagrams (vs fabrication handoff). Seven runnable recipes covering Draft auto-scaling for small parts, layered SVG export, filled feature highlights, legends with colour swatches, reference axes, and proportional title blocks.
- **2D auto-detection honours per-object colour** (#95, #92 F3): multi-object 2D drawings rendered with `objects="plate_a:red,plate_b:blue"` now route through the 2D pipeline AND apply each object's colour. Was previously rendering everything in flat black with no part/dim distinction.
- **`render_view` `colors=` dict for per-layer control** (#96, #92 F4): optional dict mapping object names and special `_dims`/`_labels` keys to colours. Resolution priority: `colors[name]` > inline `name:color` > shared palette. Use this when presentation diagrams want a specific dim colour (e.g. `darkgreen` against a light part) or fine-grained per-layer hues without restating the whole `objects=` string.
- **`render_view` explicit `mode=` parameter** (#97, #92 F8): `'auto'` (default) keeps the heuristic; `'2d'` and `'3d'` force a path and error clearly on mismatched shapes. Every render now also reports `render_mode` (`"2d"` or `"3d"`) in the response so the LLM can verify which path actually ran. Closes the silent-routing failure mode where a Compound containing both 2D Sketches and 3D solids ended up in the wrong pipeline.

### Bug fixes

- **`render_view(save_to=…)` now honoured for DXF in the MCP wrapper response** (#94): the function-level `render_view` always wrote to the user's path correctly, but the MCP server wrapper unconditionally wrote a tempfile copy and reported THAT path in the `[SEND:]` marker. The LLM saw `/tmp/build123d_<random>.dxf` even when it asked for a specific location. Same anti-pattern existed for PNG/SVG. Fix: `render_view` now records `result["<fmt>_path"]` for save_to'd files; the wrapper prefers those paths over creating tempfiles.

---

## v0.3.17

This release closes the loop on **LLM-driven 2D engineering drawings**. The workflow for 2D mirrors what was already there for 3D — write Python, render to review, export to ship — and the underlying drafting library is build123d's own (no MCP-specific dialect).

### Features

- **`build123d://drafting` cookbook** (#89): a new task-indexed MCP resource with 11 runnable examples covering the full code-first 2D drafting pipeline — Draft config, basic + tolerance dimensions, diameter dim, 3D-to-2D projection, multi-view sheet layout, hole-table pattern, title block via `TechnicalDrawing`, and the build → review → ship loop. Plus a "clean SVG export" recipe that explicitly teaches the `fill_color = line_color` trick on the dimensions layer so the LLM can produce the same clean output in scripts that run outside the MCP.
- **`render_view` auto-detects 2D inputs** (#89): when a named object has no solid content and lies flat in Z (a Sketch or Compound built via `build123d.drafting`), `render_view` routes through an `ExportSVG` → `resvg-py` raster pipeline instead of VTK tessellation. Output is a clean engineering drawing — black part lines, blue dimensions, real filled text, no doubled-line artefacts. `label_objects=True` works for 2D too, adding a label below each named object's bbox so the LLM can identify what it's looking at.
- **`export` auto-detects 2D inputs** (#89): Sketches and dimensioned drawings can now be exported to DXF or SVG via the same `export()` tool. Mixing 2D and 3D formats for the same shape errors with a clear pointer at the right tool (`use render_view(format="dxf") for the projected outline of a 3D solid`).

### Workflow guidance

- **`workflow_hints` item 11.5** (#89): explicit nudge toward `build123d.drafting` for 2D drawing work and the build → render_view → export loop.
- **`start-cad-session` step 10** (#89): same nudge in the session prompt.

### Dependency

- **`resvg-py`** added as a dependency for the SVG → PNG rasterisation step. Pure Rust wheels ship pre-built for Linux / macOS / Windows — no native cairo dependency, no system package needed.

---

## v0.3.16

### Release process

- **Hot-fix the MCP registry auto-publish workflow** (#83): the v0.3.15 publish workflow's first registry-publish run failed because the `mcp-publisher` install step downloaded the asset name as a raw binary, but upstream actually ships a tarball. The hot-fix downloads the `.tar.gz`, extracts it, smoke-tests with `--help`, and adds `-f` to every `curl` so any 4xx/5xx fails the step loudly instead of silently producing a broken binary. It also resolves the latest release tag via the GitHub API rather than the `/releases/latest/download/` shortcut, which had been returning intermittent 502s.

No user-visible code changes — this release exists to validate the registry auto-publish path end-to-end so v0.3.16 lands on `registry.modelcontextprotocol.io` automatically via GitHub OIDC, with no human authentication step.

---

## v0.3.15

### Improvements

- **`execute()` output gains shape deltas and silent-failure warnings** (#81): the diagnostic appended after every `execute()` now shows volume/topology deltas relative to the previous shape (e.g. `volume: 437.2 (-62.8, -12.6%) mm³  |  ... 7f (+1) 15e (+3) 10v (+2)`) and flags two silent failure modes the LLM otherwise sailed past unnoticed — boolean no-ops (cuts that didn't intersect, leaving topology bit-identical) and degenerate results (volume collapsed to ≈ 0). No new MCP tool, no LLM behaviour change required; warnings arrive in the response text the LLM already reads.

### Release process

- **Auto-publish to MCP registry on release** (#82): a new `publish-mcp-registry` job in `publish.yml` mirrors the PyPI publish path. On every `gh release create vX.Y.Z`, after PyPI succeeds, the job authenticates via GitHub OIDC (no stored secret), rewrites `server.json`'s version fields from the release tag, and pushes to `registry.modelcontextprotocol.io`. From this release onward the registry stays in sync with PyPI automatically.

---

## v0.3.14

This release is "more build123d native" — every change closes a gap where the server was a generic Python sandbox rather than a build123d-aware tool. Five merged PRs:

### Features

- **`render_view` labels** (#73): two new optional parameters. `label_objects=True` labels each named object from `show()` at its centroid in the PNG. `highlights=[{"object", "type", "index", "label"}, ...]` labels specific faces, edges, or vertices by index — useful for confirming "edge 5 is the one I want to fillet" before committing to an operation. Labels render on a depth-cleared overlay layer so they stay legible even at a solid's interior centroid. SVG output is unlabelled (a `label_warnings` entry surfaces this).
- **`build123d://selectors` MCP resource** (#76): a task-indexed selector cookbook, separate from `quickref`'s API-shaped reference. 15 runnable examples covering the drill-down idiom (parent → child topology), cardinal selection, geom-type filters, parallel/perpendicular orientation, numeric properties, `Select.LAST` in builder context, fillet detection (`is_circular_convex`/`is_circular_concave`), and more — plus an operator translation card (`>`, `<`, `|`, `>>`, `<<`, `@`) and a pitfalls section.
- **Compound-aware STEP export** (#77): single-object exports carry `object_name` as the body label; `*` exports produce a `Compound` labelled `assembly` with each child labelled by its `show()` name. Downstream CAD tools (FreeCAD, Fusion) now see structured assemblies with named bodies instead of "Body 1, Body 2, …".

### Documentation (LLM behaviour-shaping)

- **Joints guidance** (#75): `quickref` gains a runnable `RigidJoint` example plus a reference card listing all joint types (`RigidJoint`, `RevoluteJoint`, `LinearJoint`, `CylindricalJoint`, `BallJoint`). `workflow_hints()`, `start-cad-session`, and `llms.md` all nudge toward joints for assemblies with mechanical relationships, instead of raw `.move()`/`Location()`. Docs-only — no new MCP tool — keeps LLM-generated code idiomatic and portable outside the MCP.
- **Five more native idioms in `quickref`** (#78): pattern-placement utilities (`GridLocations`, `PolarLocations`, `Locations` with task-indexed naming), the `@` and `%` operators on edges for chaining curves without coordinate duplication, the broader operations set (`sweep`, `loft`, `mirror`, `offset`, `thicken`), and `Mode.PRIVATE` for helper geometry that doesn't join the part. The two top-level patterns are renamed using build123d's own terminology — algebra mode and builder mode. Each example was verified end-to-end before being added to the `Section` dataclass.

### Release process

- **build123d version is now explicit** (#79): `pyproject.toml` soft-pins build123d as `>=0.10,<0.11` (build123d is pre-1.0, so minor bumps may break the API). The `build123d://quickref` and `build123d://selectors` resources prepend a runtime banner showing the actually-installed version via `importlib.metadata.version`, so the docs are self-describing about their compatibility window — if a user overrides the pin, the banner reflects what they really have.

---

## v0.3.13

### Features

- **`build123d://quickref` MCP resource**: exposes a plain-text quick reference for the build123d API so LLM clients can read accurate syntax before calling `execute()`. Every runnable example is tested automatically to ensure the quickref stays accurate as the codebase evolves.
- **`start-cad-session` prompt**: primes a design session with the task description plus step-by-step workflow reminders.
- **`build123d://session` MCP resource**: read-only JSON resource exposing live session state — `current_shape` diagnostics, named objects, snapshots, and user-defined variables. Clients can read session state without spending a tool-call round-trip on `session_state()`.
- **`build123d://bd_warehouse` MCP resource**: introspects the installed `bd_warehouse` package and returns a plain-text catalogue of all available parametric components (bearings, fasteners, flanges, gears, OpenBuilds parts, pipes, sprockets, threads). Each entry shows the class name, description, constructor signature, and for size-standardised classes the available types and sizes.
- **`render_view` labels**: two new optional parameters. `label_objects=True` labels each named object from `show()` at its centroid in the PNG. `highlights=[{"object", "type", "index", "label"}, ...]` labels specific faces, edges, or vertices by index — useful for confirming "edge 5 is the one I want to fillet" before committing to an operation. Labels render on a depth-cleared overlay layer so they stay legible even when sitting at a solid's interior centroid. SVG output is unlabelled (a `label_warnings` entry surfaces this).

### Improvements

- **Default exec timeout raised to 120 s** (was 60 s) — allows more complex boolean operations to complete inside the MCP without needing to fall back to a plain Python script.
- **`dir()` restored** — available again as a builtin inside `execute()`. Dunder attribute access remains blocked at the AST level, so the sandbox is unaffected.
- **`inspect` allowlisted** — `import inspect` now works inside `execute()`. `inspect.signature()`, `inspect.getdoc()`, and `inspect.getmembers()` enable API discovery without trial-and-error round trips.
- **STL render quality improved** — `vtkPolyDataNormals` (with `ConsistencyOn` and `AutoOrientNormalsOn`) is now applied before the VTK mapper. Imported STL shells shade correctly instead of rendering with incorrect face orientation.
- **`import_cad_file` docstring clarified** — documents that `render_view` works after import, that STL imports produce a shell (volume = 0), and that rendering by object name avoids Z-fighting when the original built shape is also in session.
- **Timeout error improved** — when `execute()` times out the error message now explains that all session state has been lost (worker restarted) and recommends the probe-in-MCP / build-in-script / import-and-verify workflow.
- **`bd_warehouse` resource expanded** — new preamble documents the correct size string format (`"M6-1"` not `"M6-1.0"`), a probe pattern (`ClassName.sizes("type")`), and working code examples for `CounterSinkHole`, `TapHole`, `ClearanceHole`, and `CounterBoreHole`.
- **`workflow_hints()` expanded** — new items cover bd_warehouse fastener probing, the complex-build workflow (probe → script → import → verify), import→render pattern, and Z-fighting guidance.
- **README expanded** — "Recommended workflow" and "bd_warehouse fasteners" sections added.

### Release process

- **`.dev0` version convention**: between releases, `pyproject.toml` carries a `.dev0` suffix (e.g. `0.3.14.dev0`) so it self-documents that the working version has not yet been published. The publish workflow strips the suffix on real release and TestPyPI builds replace `.dev0` with `.dev<run_number>`. Anyone — human or AI — reading `pyproject.toml` can immediately tell which version is published vs in development.
- **`CLAUDE.md` documents release process**: only `gh release create vX.Y.Z` cuts a release; never edit `pyproject.toml` or push tags manually.

---

## v0.3.12

### Features

- **`measure()` unified response**: returns a single comprehensive JSON — volume, area, topology (face/edge/vertex counts), bounding box with center, volumetric center of mass, 6-component inertia tensor (Ixx/Iyy/Izz/Ixy/Ixz/Iyz), and face-type inventory classifying every face as Plane/Cylinder/Cone/Sphere/Torus/BSpline with type-specific params (cylinder diameter/axis, cone semi-angle, sphere radius, torus radii). Replaces the old query-dispatch API.
- **`clearance(object_a, object_b)` tool**: returns the minimum distance (mm) between two named shapes.
- **`cross_sections(object_name, axis, num_slices)` tool**: cross-sectional area at evenly spaced planes along X/Y/Z — useful for detecting internal voids, wall-thickness variation, and verifying profile against a reference.
- **`import_cad_file(path, name)` tool**: loads a STEP (.step/.stp) or STL (.stl) file as a named object in the session. Supports multi-body STEP files. Use with `shape_compare()` to verify a procedural build against a reference.
- **`named_face(shape, name)` session built-in**: returns a face by semantic name (`top`, `bottom`, `front`, `back`, `left`, `right`) based on axis sorting. Available in every `execute()` call without import.
- **OCP sub-module imports in user code**: geometric OCP modules (`OCP.gp`, `OCP.BRepGProp`, `OCP.TopExp`, `OCP.BRepAlgoAPI`, etc.) are now allowed via an explicit allowlist. File I/O modules (`OCP.STEPControl`, `OCP.IGESControl`, `OCP.OSD`) remain blocked.
- **`execute()` inline repair hints**: on error, matched hints from the repair library are appended directly to the error response — no separate `repair_hints()` call needed.

### Removed

- **`fingerprint` tool**: data is now part of the `measure()` response; `cross_sections` is a separate tool.
- **`list_objects` tool**: `session_state()` is a strict superset.
- **`validate_code` tool**: `execute()` already returns syntax and security errors inline; the standalone pre-check added friction without benefit.

---

## v0.3.7

### Features

- **`last_error()` tool**: returns structured JSON for the most recent failed `execute()` call — error type, message, line number, and a 5-line code excerpt with an arrow marker at the failing line. Cleared automatically on success.
- **`validate_code()` tool**: static analysis of code before execution — catches syntax errors, blocked imports, missing build123d import, and code that produces no output (no `result` assignment or `show()` call). No execution required.
- **`shape_compare()` tool**: compares two named objects side-by-side — volume, area, topology counts, bounding-box dimensions, and center-point offset delta. Returns structured JSON.
- **`repair_hints()` tool**: takes an error message and returns a targeted hint from an 11-entry pattern library (NoneType, CadQuery syntax, face selection, interference check, missing show(), etc.). Falls back to a generic hint if nothing matches.
- **`measure(query="summary")` mode**: single call returning volume, area, topology, bounding-box dimensions, and center — covers the most common post-execute sanity check in one round trip.
- **`session_state()` namespace variables**: the response now includes a `variables` map summarising all non-shape Python variables in the session namespace (type + value/length).
- **Assembly export via `object_name='*'`**: `export()` with `object_name='*'` bundles all named objects into a single `Compound` and exports it as one STEP or STL file.
- **Dual `render_view` response**: returns both an `ImageContent` (base64 PNG for standard MCP clients) and a `TextContent("[SEND: path]")` marker (for Telegram/file-path consumers) so both client types work without configuration.

### Bug fixes

- **Issue #54 — PNG render fails for complex assemblies**: replaced `Mesher`/Lib3MF pipeline with `shape.tessellate()` + direct VTK PolyData construction. Lib3MF's `IsValid()` check was rejecting valid OCCT boolean shapes; `tessellate()` bypasses the Lib3MF layer entirely. Per-shape try/except means partial renders succeed rather than failing the whole call.
- **Transactional `execute()`**: on any error (exception, timeout, assertion) the session now rolls back `current_shape` and `objects` to their pre-exec state. Failed code can no longer silently advance session geometry.
- **STL export via `tessellate()`**: `export()` for STL now uses `shape.tessellate()` + a binary STL writer instead of `Mesher`, matching the render fix and avoiding the same Lib3MF failures.
- **CLI `--python` version**: `--help` epilog now correctly shows `3.12` instead of `3.13` (no Python 3.13 wheels for vtk/cadquery-ocp).

---

## v0.3.5

### Features

- **`session_state` tool**: returns a structured JSON snapshot of the full session — `current_shape` metrics, all named objects with geometry stats, and snapshot names. Useful for orienting at session start or after a restore.
- **`health_check` tool**: verifies PNG render (VTK), SVG render (HLR), STEP export, and STL export with a trivial shape. Returns per-capability `ok`/`error` status. Run at session start if you suspect a missing dependency.
- **`version` MCP tool**: returns the server version string from inside the session, complementing the existing `--version` CLI flag.
- **`diff_snapshot` JSON mode**: passing `format="json"` returns structured diff output (`{"a": {...}, "b": {...}}`) for programmatic consumption by agents.
- **Outcome test suite**: added 21 usage-focused outcome tests covering the full API surface (all MCP tools exercised end-to-end).
- **README badges**: added PyPI version, Python version, CI status, and MIT license badges.
- **Updated `llms.md`**: full rewrite covering all tools with inputs, outputs, and examples; updated recommended 12-step workflow.

### Bug fixes

- **`show()` now sets `current_shape`**: calling `show(shape, "name")` now also updates `current_shape`, so subsequent `measure()`/`render_view()`/`export()` calls work immediately without an explicit `result` assignment.
- **Failed `execute()` no longer mutates `current_shape`**: if code raises an exception, the previous `current_shape` is preserved. Failed code cannot silently advance session state.
- **`exec_timeout` wired through to worker**: `WorkerSession(exec_timeout=N)` now correctly passes the timeout to the child process (previously silently used the default 30 s).
- **`requires-python` capped at `<3.13`**: `vtk` and `cadquery-ocp` have no wheels for Python 3.13+; the cap now prevents confusing resolver errors.

---

## v0.3.4

### Features

- **Auto-diagnostics after `execute()`**: when `current_shape` changes on a successful run, the response now includes a compact diagnostics line (volume, bounding-box dimensions, face/edge/vertex counts). Agents no longer need a separate `measure()` call just to confirm a new shape was created.
- **Assertion / constraint support**: `AssertionError` raised inside executed code is now surfaced as `"Constraint failed: <message>"` rather than `"Error: AssertionError: ..."`. Scripts can use `assert shape.volume > X, "too small"` as explicit geometry constraints, distinct from accidental bugs.
- **`diff_snapshot` tool**: new tool comparing two named snapshots (or a snapshot vs current session state). Reports volume delta, topology changes (face/edge/vertex counts), bounding-box changes, and added/removed/changed objects — useful for confirming that a fillet, cut, or other operation changed geometry as expected.

---

## v0.3.3

### Bug fixes

- Fix `render_view` crashing with `AttributeError: module 'pyvista' has no attribute 'start_xvfb'` under `uvx build123d-mcp` (#43). pyvista 0.48 removed the helper that the server relied on for headless Linux rendering. Replaced pyvista with direct VTK calls (already pulled in transitively via cadquery-ocp/cadquery-vtk, no install bloat); `_ensure_display()` spawns Xvfb on Linux when needed, mirroring what pyvista's helper used to do.
- Fix `export` and `render_view(save_to=...)` rejecting `/tmp/` paths as path-traversal (#44). Writes are now allowed under the cwd, `tempfile.gettempdir()`, and `/tmp`. Validation runs against the resolved real path, so symlink escapes (e.g. `/tmp/foo` → `/etc/passwd`) are now caught — the previous textual `..` check missed them.

### Features

- Add `format` parameter to `render_view`: `"png"` (default), `"svg"`, or `"both"`. SVG uses build123d's HLR projection — works without a display backend at all. When `format="png"` is requested but the VTK pipeline fails (no DISPLAY, no OSMesa/EGL), the call automatically falls back to SVG so the AI still gets a visual.

### CI

- Add cross-platform matrix: Ubuntu, macOS, and Windows. Linux gets xvfb, Windows gets Mesa3D for offscreen rendering (via `pyvista/setup-headless-display-action`, CI-tooling only — no pyvista runtime dep). Pin Python to 3.12 in CI because vtk 9.3 has no cp313 wheel.

---

## v0.3.2

### Packaging

- Cap `requires-python` at `<3.14` so `uvx build123d-mcp` selects a compatible interpreter instead of trying to build `cadquery-ocp` from source on Python versions where it has no wheels.

---

## v0.3.1

### Features

- Add `--version` flag to the CLI (`uvx build123d-mcp --version`).

### CI

- Fix TestPyPI publish failures: dev builds now use a unique `.devNNN` version suffix, and the patch version is auto-bumped in `pyproject.toml` after each release.

---

## v0.3.0

### Security

- Block subclass-traversal sandbox escapes at AST level: dunder attribute access (`__class__`, `__bases__`, `__subclasses__`, etc.) is now rejected by the AST check, and `getattr`/`vars`/`dir`/`hasattr` are removed from both the AST-level blocklist and the restricted builtins. Closes the most common prompt-injection escape paths without affecting normal build123d usage (operator overloading uses bytecode ops, not explicit dunder access).
- Add AST check to `load_part` for consistency with `execute` — library part code now goes through the same security validation as user-submitted code.

### Architecture

- Replace fork-per-call worker with a persistent subprocess. The worker process now stays alive across calls; the session (namespace, shapes, snapshots) persists in the worker. On timeout the worker is killed and restarted with a fresh session. This eliminates per-call fork overhead and makes timeout behaviour deterministic.
- Use `spawn` context `Pipe()` instead of the default `multiprocessing.Pipe()` for cross-platform reliability.

### Bug fixes

- Fix worker crash paths that returned `str` where `bytes` were expected, causing cascading errors after a crash.
- Fix library name collision when two parts in different subdirectories share the same filename.
- Fix `save_snapshot` / `restore_snapshot` incorrectly listing `current_shape` in the captured geometry when it was `None`.

### Performance

- Reduce `_needs_rescan` syscall overhead with a directory mtime fast path — the library index skips a full directory walk when the mtime is unchanged.

---

## v0.2.0

### Features

- Add part library: `search_library` and `load_part` tools for parametric part reuse.
- Add topology queries to `measure` (`face_count`, `edge_count`, `vertex_count`, `shell_count`, `solid_count`, `compound_count`).
- Add arbitrary camera angles to `render_view` (`azimuth`, `elevation` parameters).
- Add positional clip plane to `render_view` (`clip_at` parameter to specify cut position rather than always bisecting at the mesh centre).

### Fixes

- Update docs for `src` layout, `uvx` invocation, and corrected `show()` argument order.

---

## v0.1.0

Initial release.

- MCP server with `execute`, `render_view`, `export_file`, `measure`, `interference`, `save_snapshot`, `restore_snapshot`, `reset`, `list_objects` tools.
- Persistent session: namespace, `current_shape`, and named objects survive across `execute()` calls.
- Three-layer security model: AST inspection, restricted builtins, execution timeout.
- Multi-object support via `show(shape, name)`.
- Security fixes: path traversal in `export_file`, temp-file race in `render_view`.
