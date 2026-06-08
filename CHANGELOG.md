# Changelog

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
