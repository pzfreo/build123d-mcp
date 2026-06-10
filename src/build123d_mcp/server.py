from mcp.server.fastmcp import FastMCP
from mcp.types import PromptMessage, TextContent

from build123d_mcp.tools._marshal import marshal_render_drawing, marshal_render_view
from build123d_mcp.worker import WorkerSession

mcp = FastMCP("build123d-mcp")
_session: WorkerSession


def configure(session: WorkerSession) -> None:
    """Set the module-level session singleton.

    Called by ``cli.main()`` at startup; kept here so the tool closures resolve
    ``_session`` at this module's scope. Whether a part library is configured is
    read from ``session.has_library`` rather than tracked separately.
    """
    global _session
    _session = session


@mcp.tool()
def execute(code: str) -> str:
    """Execute build123d Python code in the persistent session. Errors include automatic fix hints — read them before retrying. Use show(shape, name) to register named objects (name defaults to 'shape'); show() immediately prints volume and face count confirming the shape is non-empty. After any boolean operation (-, +, &) call measure() to confirm it succeeded (check topology.faces). named_face(shape, name) is a built-in helper: named_face(box, 'top') returns the highest-Z face, 'bottom'/'front'/'back'/'left'/'right' work similarly."""
    from build123d_mcp.tools.execute import execute_code

    return execute_code(_session, code)


@mcp.tool()
def render_view(
    direction: str = "iso",
    objects: str = "",
    quality: str = "standard",
    clip_plane: str = "",
    clip_at: float | None = None,
    azimuth: float = 0.0,
    elevation: float = 0.0,
    save_to: str = "",
    format: str = "png",
    label_objects: bool = False,
    highlights: list[dict] | None = None,
    colors: dict[str, str] | None = None,
    mode: str = "auto",
) -> list:
    """Render model. Auto-detects 3D vs 2D inputs: solids go through the VTK tessellation path; 2D shapes (Sketches, Compounds of edges, dimensioned drawings composed via build123d.drafting) go through the ezdxf+matplotlib raster path. Use this to review your dimensioned drawings the same way you review 3D parts. format: 'png' (raster, default), 'svg' (HLR line drawing — works without a display, no shading but precise edges), 'dxf' (HLR line drawing as DXF — the standard 2D CAD interchange format; use when you need projected polylines as parseable geometry rather than as a raster, e.g. to draw an annotated overlay on top of an accurate base layer instead of redrawing the shape by hand), or 'both' (returns the PNG and SVG together — useful when you want shaded depth cues plus crisp edge geometry). If the raster path fails (typically headless host with no display backend) and format='png', the server falls back to SVG automatically. Renders confirm appearance, not geometry — verify boolean operations with measure() before rendering. direction: top, front, side, iso. objects: comma-separated names or name:color pairs e.g. 'u_frame:blue,roller:red' (default: all, auto-coloured). quality: standard, high. clip_plane: x, y, z to slice; clip_at: absolute world coordinate along that axis (default: each mesh's midpoint). azimuth/elevation: camera rotation in degrees applied after the direction preset. save_to: optional file path; for format='both' the PNG and SVG are written as <save_to>.png and <save_to>.svg. mode: render-pipeline selector. 'auto' (default) detects 2D vs 3D from shape content (no solids + flat in Z = 2D). '2d' forces the 2D pipeline (errors if any shape is 3D). '3d' forces the 3D pipeline (errors if every shape is flat 2D). Useful when the auto-detection picks wrong (e.g. a Compound with both a 2D Sketch and a 3D solid silently routes to 3D). The actual path used is reported in the response as 'Rendered via <mode> pipeline.' colors: optional dict mapping object names (e.g. 'bracket') and special layer keys (`_dims`, `_labels`) to colour names like 'red'/'blue' or '#aabbcc'. Overrides the per-object name:color syntax and the default blue dimension colour. Use this when you need fine-grained colour control for presentation diagrams (different parts in different hues, distinct dimension colour, etc.). Only affects PNG/SVG output for 2D drawings; ignored for 3D solids and DXF. label_objects: when true, each named object from show() is labelled at its centroid in the PNG. highlights: optional list of specific entities to label, e.g. [{"object": "bracket", "type": "edge", "index": 5, "label": "hinge_edge"}]; type is 'face', 'edge', or 'vertex' and index matches shape.faces()/edges()/vertices() position. The referenced object must already be registered with show() and included in the rendered set. Labels are PNG-only; SVG output is unlabelled."""
    result = _session.render_view(
        direction=direction,
        objects=objects,
        quality=quality,
        clip_plane=clip_plane,
        clip_at=clip_at,
        azimuth=azimuth,
        elevation=elevation,
        save_to=save_to,
        format=format,
        label_objects=label_objects,
        highlights=highlights,
        colors=colors,
        mode=mode,
    )

    return marshal_render_view(result)


@mcp.tool()
def measure(object_name: str = "") -> str:
    """Measure a shape and return a complete geometric summary: volume (mm³), surface area (mm²), topology (face/edge/vertex counts), bounding box with per-axis size and center, volumetric center of mass, 6-component inertia tensor (Ixx/Iyy/Izz/Ixy/Ixz/Iyz), and a face-type inventory classifying every face as Plane/Cylinder/Cone/Sphere/Torus/BSpline with area and type-specific params (e.g. cylinder diameter and axis). Prefer measure over render_view for verifying geometry — numbers are unambiguous. topology is the fastest confirmation that a boolean operation succeeded: a failed cut leaves face/edge/vertex counts unchanged. object_name: named object from show() (default: current shape)."""
    return _session.measure(object_name)


@mcp.tool()
def clearance(object_a: str, object_b: str) -> str:
    """Spatial relationship between two named shapes. Returns JSON with `clearance` (mm), `status` (one of: apart, touching, containing, interpenetrating), `containment` (a_in_b, b_in_a, or neither), and `intersection_volume` / `a_volume_outside_b` / `b_volume_outside_a` for overlap quantification. Reads `clearance` differently per status: apart=gap, containing=wall thickness from inner surface to outer hull (use this to verify a pocket fits inside a plate), touching=0, interpenetrating=0 (check intersection_volume + a_volume_outside_b for the wall-piercing case). Single call replaces the older clearance + interference combination. object_a, object_b: names from show()."""
    return _session.clearance(object_a, object_b)


@mcp.tool()
def analyze_printability(
    object_name: str = "",
    support_angle: float = 45.0,
    nozzle: float = 0.4,
    min_perimeters: int = 2,
    build_volume: str = "",
) -> str:
    """Analyse a build123d shape for FDM printability using augura (BREP-exact analysis).

    Checks: overhangs, manifold/watertight, tip-over risk, brim/raft need,
    minimum vertical feature (→ max layer height), and thin walls. Optionally
    checks bed-fit against a declared build volume.

    Returns a plain-text summary followed by a JSON report with per-finding
    detail (kind, severity, message, area/location where applicable).

    object_name: named object from show() (default: current shape).
    support_angle: faces shallower than this many degrees from horizontal need
        support (default 45).
    nozzle: nozzle diameter in mm for wall-thickness check (default 0.4).
    min_perimeters: walls thinner than min_perimeters × nozzle are flagged
        (default 2).
    build_volume: optional build envelope as 'X Y Z' in mm, e.g. '256 256 256';
        omit to skip the bed-fit check.
    """
    return _session.analyze_printability(
        object_name, support_angle, nozzle, min_perimeters, build_volume
    )


@mcp.tool()
def cross_sections(object_name: str = "", axis: str = "Z", num_slices: int = 10) -> str:
    """Compute cross-sectional areas at evenly spaced planes along an axis. Returns a list of {position, area} pairs. axis: X, Y, or Z (default Z). num_slices: number of planes (default 10, minimum 2). Useful for detecting internal voids, wall-thickness variation, or verifying that a shape's cross-section profile matches a reference. object_name: named object from show() (default: current shape)."""
    return _session.cross_sections(object_name, axis, num_slices)


@mcp.tool()
def export(filename: str, format: str = "step", object_name: str = "") -> str:
    """Export model. format: step, stl, dxf, svg, or comma-separated list e.g. 'step,stl' or 'dxf,svg'. 3D shapes (solids) export to step/stl; 2D shapes (Sketches and dimensioned drawings composed via build123d.drafting) export to dxf/svg. Mixing 2D and 3D formats for the same shape errors with a clear message. object_name: named object from show(), '*' to export all named shapes as a combined assembly (default: current shape). STEP exports carry the session names as labels — single-object exports use the object_name, '*' exports produce a Compound labelled 'assembly' with each child labelled by its show() name. Downstream CAD tools (FreeCAD, Fusion) will see the structured assembly with named bodies. Use dxf for engineering-drawing handoff to other CAD tools; svg for embedding in docs/wikis."""
    return _session.export_file(filename, format, object_name)


@mcp.tool()
def interference(object_a: str, object_b: str) -> str:
    """[Deprecated — use clearance() instead, which supersedes this tool with richer diagnostics.] Check whether two named objects (from show()) intersect. Returns interferes (bool), volume (mm³ of overlap), and bounds of the interference region."""
    return _session.interference(object_a, object_b)


@mcp.tool()
def inspect_drawing(objects: str = "", svg_path: str = "") -> str:
    """Structured bbox and annotation report for a 2D drawing.

    Two modes:

    1. Session mode (default): inspects objects registered via annotate()/show().
       Returns per-object bounding boxes, face/edge counts, annotation metadata
       (label string, measured length, Leader tip/elbow), and structural lint.

    2. SVG mode (svg_path set): parses an SVG file from disk and reports page
       size, layer ids, text content + positions, and element counts. Decouples
       inspection from the build-and-register ceremony — works on SVGs from any
       source (CI artifacts, third-party exports, prior runs).

    Use annotate(result, name) instead of show(result.shape, name) when building
    with build123d_drafting so metadata is captured:

        from build123d_drafting import Dimension, Draft
        draft = Draft(font_size=2.5, decimal_precision=1)
        w = Dimension((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
        annotate(w, "width_dim")

    For vanilla build123d.ExtensionLine/DimensionLine, pass the label explicitly:

        w = ExtensionLine(border=[...], offset=6, draft=draft, label="40")
        annotate(w, "width_dim", label="40")

    Args:
        objects: comma-separated object names (default: all). Session mode only.
        svg_path: path to an SVG file on disk. Switches to SVG mode.
    """
    return _session.inspect_drawing(objects, svg_path)


@mcp.tool()
def view_axes(
    viewport_origin: list[float],
    viewport_up: list[float] | None = None,
    look_at: list[float] | None = None,
) -> str:
    """Return the world→page axis mapping for a project_to_viewport call,
    computed analytically (no projection performed). Use this BEFORE rendering
    a projected view to confirm which world axis ends up on which page axis
    and with what sign — catches bottom-view/side-view axis swaps before they
    show up in the render.

    Returns JSON like {"world_X": ["page_X", -1.0], "world_Y": ["page_Y", 1.0],
    "world_Z": ["depth", 0.0]} — for a bottom-view origin (0,0,-100), world-X
    flips to negative page-X.

    Args:
        viewport_origin: camera position, same arg as project_to_viewport.
        viewport_up: up vector. Defaults to (0,1,0).
        look_at: target point. Defaults to origin.
    """
    return _session.view_axes(
        tuple(viewport_origin),
        tuple(viewport_up) if viewport_up is not None else (0.0, 1.0, 0.0),
        tuple(look_at) if look_at is not None else (0.0, 0.0, 0.0),
    )


@mcp.tool()
def lint_drawing(
    svg_path: str = "",
    drawing_scale: float = 1.0,
    view_shape_names: list[str] | None = None,
) -> str:
    """Run structural drawing-quality checks and return JSON {violations: [...]}.

    Session mode (default): reconstructs the session's annotations and delegates
    to build123d-drafting-helpers (lint_drawing + find_interferences) — single
    source of truth. Surfaces label-vs-measured divergence (axis swap), Leader
    line through its own label, annotation/label overlap, a witness/extension
    line piercing a neighbour's label, redundant collinear lines, and page-bounds
    overshoot.

    SVG mode (svg_path set): scans an SVG file for export-only pathologies — most
    importantly native <text> elements (build123d renders glyph paths, so any
    <text> won't DXF-export and won't scale with the model).

    drawing_scale: when the geometry was scaled up before projecting — e.g. a
    7.5 mm feature drawn at 5:1 via part.scale(5) — pass the same factor (5.0)
    so the label-vs-measured check divides each measured path length by it
    before comparing to the label. This lets labels carry the *real* dimension
    while the geometry is drawn enlarged, instead of every dim tripping a false
    axis-swap warning. Session mode only; defaults to 1.0 (no scaling).

    view_shape_names: list of shape names (from show()) representing the placed
    view outlines. Used to detect view_annotation_overlap (annotation bbox
    overlaps a view outline) and view_overlap (two view outlines overlap).
    Pass the visible-side placed compounds from each projection, e.g.
    ["front_placed", "side_placed", "plan_placed", "iso"]. Session mode only.

    Each violation is {severity, check, object, message}. Run this after major
    drawing additions; running it BEFORE rendering catches the bug at the source.
    """
    return _session.lint_drawing(svg_path, drawing_scale, view_shape_names)


@mcp.tool()
def render_drawing(svg_path: str, width: int = 1200, save_to: str = "") -> list:
    """Rasterise an existing SVG file to PNG via resvg-py.

    Complements render_view (which takes build123d shapes from the live
    session) by accepting an SVG written outside the sandbox — typically by
    a short Python script that does the ExportSVG call directly. The PNG is
    returned inline so the LLM can see the drawing without you having to
    open the file in another tool.

    Args:
        svg_path: path to an SVG file on disk.
        width: output pixel width (default 1200); height set by SVG aspect ratio.
        save_to: optional path to write the PNG. If empty, PNG bytes are
            delivered inline only.
    """
    result = _session.render_drawing(svg_path, width, save_to)
    return marshal_render_drawing(result, svg_path, save_to)


@mcp.tool()
def save_drawing_annotations(svg_path: str) -> str:
    """Write a .dims.json sidecar file alongside an SVG with label metadata.

    build123d renders Text as filled glyph paths, not <text> SVG elements, so
    label strings are irrecoverable from a finished SVG. Call this tool after
    completing a drawing (annotate all dims/leaders with annotate()) and before
    or after exporting the SVG. The sidecar is read automatically by
    inspect_drawing(svg_path=...) to restore annotation content.

    Workflow:
        1. Build your drawing with Dimension / Leader / annotate()
        2. Export SVG:  execute("exporter.write('drawing.svg')")
        3. Save metadata: save_drawing_annotations("drawing.svg")
        4. Inspect later: inspect_drawing(svg_path="drawing.svg")
           → includes full annotations dict from the sidecar

    Args:
        svg_path: path to the SVG file (sidecar written as <svg_path>.dims.json).
    """
    return _session.save_drawing_annotations(svg_path)


@mcp.tool()
def search_library(query: str = "") -> str:
    """Search the part library. query: keywords matched against name, description, tags, category (empty returns all). Returns name, category, description, tags, and full parameter specs including types, defaults, and descriptions."""
    if not _session.has_library:
        return "No part library configured. Start the server with --library PATH or set BUILD123D_PART_LIBRARY."
    return _session.search_library(query)


@mcp.tool()
def load_part(name: str, params: str = "") -> str:
    """Load a named part from the library into the session. name: part name from search_library. params: optional JSON object of parameter overrides e.g. '{\"od\": 8.0, \"length\": 20.0}' — unspecified params use their defaults. The part is registered as a named object and becomes current_shape."""
    if not _session.has_library:
        return "No part library configured. Start the server with --library PATH or set BUILD123D_PART_LIBRARY."
    return _session.load_part(name, params)


@mcp.tool()
def save_snapshot(name: str) -> str:
    """Save a named checkpoint of the current geometric state (current_shape and the show() object registry).
    The Python variable namespace is NOT saved — only geometry. Call this before risky experiments so you can
    restore known-good geometry without re-running all prior execute() calls."""
    return _session.save_snapshot(name)


@mcp.tool()
def restore_snapshot(name: str) -> str:
    """Restore geometric state from a previously saved snapshot (current_shape and the show() registry).
    The Python variable namespace is NOT restored — execute() calls made after the snapshot are still in scope,
    but current_shape and all show() objects revert to what they were at snapshot time.
    Raises an error if the snapshot name does not exist."""
    return _session.restore_snapshot(name)


@mcp.tool()
def diff_snapshot(snapshot_a: str, snapshot_b: str = "", format: str = "text") -> str:
    """Compare two snapshots by geometry metrics (volume, topology, bounding box). snapshot_b defaults to current session state if omitted. format: 'text' (default, human-readable) or 'json' (structured, for programmatic consumption)."""
    return _session.diff_snapshot(snapshot_a, snapshot_b, format)


@mcp.tool()
def session_state() -> str:
    """Return a structured JSON snapshot of the current session: current_shape metrics, all named objects (replaces list_objects) with geometry stats, snapshot names, and a variables summary of the Python namespace (type + volume for shapes, type + length for collections, type + value for scalars). Use this to orient after a reset, restore, or multi-step build to confirm what geometry and variables are active."""
    return _session.session_state()


@mcp.tool()
def health_check() -> str:
    """Verify that render and export dependencies are working. Tests PNG render (VTK), SVG render (build123d HLR), STEP export, and STL export with a trivial shape. Returns JSON with ok/error per capability. Run at session start if you suspect a missing dependency."""
    return _session.health_check()


@mcp.tool()
def reset() -> str:
    """Clear the current session back to empty state, including all snapshots."""
    return _session.reset()


@mcp.tool()
def shape_compare(object_a: str, object_b: str) -> str:
    """Compare two named shapes (from show()) by geometry metrics: volume delta, bbox delta, topology delta (faces/edges/vertices), and center offset. Useful when you have an intended design and a reference/test shape and want to verify they match — or to quantify how a modification changed the geometry."""
    return _session.shape_compare(object_a, object_b)


@mcp.tool()
def align_check(object_a: str, object_b: str, axis: str = "Z", mode: str = "flush") -> str:
    """Check alignment between two named objects along an axis. axis: X, Y, or Z. mode: flush (signed distance between bbox extremes — positive=A extends further), center (offset between bbox centroids), clearance (gap between nearest faces — positive=apart, negative=overlap). Returns JSON: {delta, axis, mode, object_a, object_b, interpretation}."""
    return _session.align_check(object_a, object_b, axis=axis, mode=mode)


@mcp.tool()
def resolve(object_name: str, selector: str, label: str = "") -> str:
    """Evaluate a selector expression against a named object and return a geometry descriptor. selector is a Python expression suffix applied to the object, e.g. '.faces().filter_by(Axis.Z).last()'. If label is given, the descriptor is stored in session.geometry_refs[label] and appears in session_state(). Returns JSON: {label, ref, object, selector, type, area/length, center, normal (for Face)}. The ref field uses @cad[object#label] format."""
    return _session.resolve(object_name, selector, label=label)


@mcp.tool()
def script(save_to: str = "") -> str:
    """Return a single Python script assembled from all successfully executed code blocks in this session. Prepends 'from build123d import *' if not already present. If save_to is given, writes the script to that path and returns {script_path, blocks}; otherwise returns {script, blocks}. Useful for exporting a reproducible script after an interactive session."""
    return _session.script(save_to=save_to)


@mcp.tool()
def import_cad_file(path: str, name: str = "") -> str:
    """Import a STEP (.step/.stp) or STL (.stl) file as a named object in the session. path: absolute or relative path to the file. name: name to register the shape under (defaults to the filename stem). The shape becomes both the named object and the current_shape. Returns volume, topology, and bounding box of the imported shape. After importing, use render_view() to visualise the shape, measure() for geometry queries, or shape_compare() to diff against a show() object. Note: STL imports produce a shell (volume=0) rather than a solid — render_view and measure still work, but interference() and boolean operations require a solid. If you have both the original built shape and an imported copy in session.objects, render the imported one by name (e.g. objects='mypart') to avoid Z-fighting artifacts from two co-located shapes."""
    return _session.import_cad_file(path, name)


@mcp.tool()
def repair_hints(error_text: str) -> str:
    """Given an error message from execute(), return targeted fix suggestions for common build123d mistakes: wrong Location syntax, missing .part, CadQuery idioms, blocked imports, degenerate boolean results, fillet edge selection, and more. Pass the full error string from execute() or last_error()."""
    from build123d_mcp.tools.repair_hints import repair_hints as _repair_hints

    return _repair_hints(error_text)


@mcp.tool()
def last_error() -> str:
    """Return details of the last failed execute() call: exception type, message, and (for runtime and syntax errors) line number and a 5-line excerpt around the failing line. Security errors include a message but no line/excerpt. Returns {\"error\": null} if the last execute() succeeded or no execute() has failed yet. Call this immediately after an execute() error to get the exact failing line — much faster than re-reading the submitted code."""
    return _session.last_error()


@mcp.tool()
def version() -> str:
    """Return the installed versions of the build123d-mcp server and its key dependencies (build123d, build123d-drafting-helpers). Use this to confirm which server build is running — e.g. to check whether a feature or fix is present, or whether the client is talking to a stale install."""
    # Computed in-process (pure importlib.metadata, same venv as the worker), so
    # it still answers when the worker subprocess is down — exactly the stale /
    # broken-install case this tool exists to diagnose.
    from build123d_mcp.tools.version import version_info

    return "\n".join(f"{name}: {ver}" for name, ver in version_info().items())


@mcp.tool()
def workflow_hints() -> str:
    """Return guidance on how to use these tools effectively. Call this at the start of a session or whenever unsure which tool to reach for."""
    return """\
BUILD123D-MCP WORKFLOW GUIDE

1. ORIENT FIRST
   At the start of a session, call session_state() to see what geometry, objects, and
   snapshots are already active. Call health_check() if you suspect a missing dependency
   (VTK, display, STEP export). Call version() to confirm the server version.

2. MEASURE BEFORE YOU LOOK
   After building or modifying geometry, verify with measure() before calling render_view.
   Numbers are unambiguous; renders can look correct even when the geometry is wrong.
   Recommended order: execute → measure → render_view (if you need to see it).

3. VERIFY BOOLEAN OPERATIONS WITH TOPOLOGY
   After any cut, union, or intersection, call measure() and check topology.faces.
   A successful boolean changes face/edge/vertex counts; a failed one leaves them unchanged.
   measure().volume confirms the magnitude of the change.

4. MEASURE THE OBJECT IN QUESTION — NOT A PROXY
   When debugging, call measure() on the actual disputed object.
   Testing an isolated reconstruction and using that as proof of the full assembly is a
   common mistake — the two may differ in ways that matter.

5. NAME AND AUDIT YOUR SHAPES
   Use show(shape, "name") after creating important geometry — it also sets current_shape.
   The execute() output immediately confirms name, volume, and face count.
   Call session_state() for a full JSON view of all active shapes, objects, and snapshots.
   session_state() includes the named-object list — no separate list_objects() call needed.

6. CHECKPOINT BEFORE EXPERIMENTS — AND PROPOSALS
   Call save_snapshot("name") before any operation you might want to undo.
   Snapshots are instant. restore_snapshot("name") reverts geometry without re-running code.
   Use diff_snapshot("name") to see what changed; pass format="json" for structured output.

   "What if?" proposals: when asked to evaluate a possible modification (add a hole here,
   widen this slot, swap this part), the right pattern is:
       save_snapshot("before")   # cheap; geometry-only
       <apply the proposed change via execute()>
       <run analyses: measure(), clearance(), cross_sections(), render_view()>
       restore_snapshot("before")  # canonical model untouched
   Use this instead of redrawing the geometry in matplotlib or editing the source file.
   The 3D mutation + 3D analysis loop is cheaper than re-deriving geometry by hand,
   and the restore guarantees the canonical model isn't accidentally touched.

7. CROSS-SECTIONS FOR INTERNAL GEOMETRY
   render_view with clip_plane + clip_at reveals interior features.
   Use clip_at to position the cut at a specific world coordinate, not just the midpoint.
   Combine with measure(topology) on the unclipped shape to confirm what you see.

8. PART LIBRARY
   search_library("keyword") returns full parameter specs.
   Call load_part("name", '{"param": value}') immediately — no second lookup needed.
   Unspecified parameters use the defaults shown in search results.

9. BD_WAREHOUSE FASTENERS
   Read the build123d://bd_warehouse resource before scripting any fastener geometry.
   Always probe sizes before writing the script to get the correct string format:
     execute('from bd_warehouse.fastener import CounterSunkScrew; print(CounterSunkScrew.sizes("iso10642"))')
   Use CounterSinkHole/TapHole/ClearanceHole/CounterBoreHole with the fastener object —
   never compute head geometry or tap-drill diameters manually.

10. RECOMMENDED WORKFLOW FOR COMPLEX BUILDS
   The execute() timeout (default 120s) hard-limits what can be built in a single call.
   For builds with many booleans (IsoThread, multi-body fillets, high face counts):
     a) Probe the API here: small execute() calls, dir(), inspect.signature(), size lookups.
     b) Write the actual build as a Python script; run it with Bash.
     c) Import the result: import_cad_file("part.step", "part")
     d) Verify and visualise: measure("part"), render_view(objects="part")
   The timeout ceiling can be raised with --exec-timeout N or BUILD123D_EXEC_TIMEOUT=N.

11.5. 2D DRAWINGS — TWO FLAVOURS
   For dimensioned 2D drawings, use build123d.drafting (Draft / ExtensionLine /
   DimensionLine / TechnicalDrawing) inside execute() to compose the drawing.
   The result is a Sketch or Compound — review it with render_view(objects="...")
   exactly like a 3D part (the server auto-detects 2D and pipes through the
   ezdxf+matplotlib path), and ship it with export(name, "dxf").

   Two cookbooks for two audiences:
   - build123d://drafting — engineering drawings for fabrication: tolerance
     dims, TechnicalDrawing title block, multi-view sheets, hole tables.
     Two-colour output (black part + blue dims).
   - build123d://presentation — design-discussion diagrams: per-group colour
     via ExportSVG layers, filled feature highlights, legends, reference
     axes, Draft scaling for small parts. Multi-colour SVG, run from a
     small script outside the MCP sandbox (the sandbox blocks
     ExportSVG.write()). Use this for chat / doc / proposal output.

   The defining recipe in the presentation cookbook is "scale Draft to your
   part size" — Draft defaults are tuned for A4, and on a 25-mm-wide part the
   default line_width=0.5 and arrow_length=3.0 make witness lines render as
   thick filled rectangles. Override every parameter, not just font_size.

   For a guided multi-view drawing workflow (choose views, scale/page size,
   annotate, lint, export SVG/DXF/PDF), call install_skill() to write a
   step-by-step skill file into the current project, or read the skill directly
   from the build123d://skill/drawing resource.

11. IMPORTING EXTERNAL FILES
   After import_cad_file(), the shape is a named object — use render_view(objects="name")
   to visualise it. If the session also contains the original built shape at the same
   position, always render by name to avoid Z-fighting (striped colour artifacts).
   STL imports produce a shell (volume=0); render_view and measure work, but interference()
   and boolean operations require a solid.

12. ASSEMBLIES — USE JOINTS, NOT JUST .move()
   For assemblies of two or more parts that have a real mechanical relationship
   (mounted on, hinged to, slides along), reach for build123d Joints rather than
   positioning parts with .move() / Location(). RigidJoint expresses a fixed
   mount; RevoluteJoint a hinge; LinearJoint a slider; CylindricalJoint
   rotate-and-slide; BallJoint a 3-axis pivot.
   The benefit: move the parent later, the child follows. With raw .move() the
   relationship is lost.
   Pattern (rigid mount):
     RigidJoint("mount", to_part=plate, joint_location=Location((0, 0, 2.5)))
     RigidJoint("base",  to_part=pin,   joint_location=Location((0, 0, -5)))
     plate.joints["mount"].connect_to(pin.joints["base"])
   See build123d://quickref for joint type details and movable-joint examples.
"""


@mcp.resource(
    "build123d://quickref",
    mime_type="text/plain",
    description="build123d API quick reference: primitives, booleans, positioning, sketch-to-3D, selectors, fillets.",
)
def build123d_quickref() -> str:
    """build123d API quick reference."""
    from build123d_mcp.quickref import build_quickref_text

    return build_quickref_text()


@mcp.resource(
    "build123d://selectors",
    mime_type="text/plain",
    description="Task-indexed cookbook of selector patterns: get the top face, find circular edges, filter by area/length/radius, Select.LAST in builder context, fillet detection, and the operator shortcuts.",
)
def build123d_selectors_cookbook() -> str:
    """build123d selectors cookbook — task-indexed patterns."""
    from build123d_mcp.selectors_cookbook import build_selectors_cookbook_text

    return build_selectors_cookbook_text()


@mcp.resource(
    "build123d://drafting",
    mime_type="text/plain",
    description="Code-first 2D engineering drawings cookbook: project a 3D part to a 2D view, dimension with ExtensionLine/DimensionLine, add tolerances, compose a TechnicalDrawing title block, multi-view sheet layout, hole-table pattern, export to DXF/SVG.",
)
def build123d_drafting_cookbook() -> str:
    """build123d 2D drafting cookbook — code-first engineering drawings."""
    from build123d_mcp.drafting_cookbook import build_drafting_cookbook_text

    return build_drafting_cookbook_text()


@mcp.resource(
    "build123d://presentation",
    mime_type="text/plain",
    description="Code-first design-discussion diagrams: per-group colour via ExportSVG layers, filled feature highlights, legends with swatches, reference axes, titles, and Draft scaling for small parts. Sister cookbook to build123d://drafting (which targets fabrication handoff).",
)
def build123d_presentation_cookbook() -> str:
    """build123d presentation cookbook — discussion diagrams (vs drafting's fab drawings)."""
    from build123d_mcp.presentation_cookbook import build_presentation_cookbook_text

    return build_presentation_cookbook_text()


@mcp.resource(
    "build123d://session",
    mime_type="application/json",
    description="Live session state: current shape diagnostics, named objects, snapshots, and user-defined variables.",
)
def build123d_session_state() -> str:
    """Live session state as JSON."""
    return _session.session_state()


@mcp.resource(
    "build123d://bd_warehouse",
    mime_type="text/plain",
    description="Catalogue of pre-built parametric parts in bd_warehouse: bearings, fasteners, gears, pipes, threads, and more.",
)
def build123d_bd_warehouse() -> str:
    """bd_warehouse component catalogue."""
    from build123d_mcp.bd_warehouse_resource import build_bd_warehouse_text

    return build_bd_warehouse_text()


@mcp.tool()
def suggest_view_layout(
    object_name: str = "",
    page_w: float = 297.0,
    page_h: float = 210.0,
    scale: float = 1.0,
    views: list[str] | None = None,
    title_block_w: float = 150.0,
    title_block_h: float = 30.0,
    margin: float = 10.0,
    extents: list[float] | None = None,
    centroid: list[float] | None = None,
) -> str:
    """Auto-calculate safe VIEW_X / VIEW_Y positions for a multi-view engineering drawing.

    Measures the named shape's bounding box and returns per-view page positions
    (VIEW_X, VIEW_Y), look_at values, and camera/up vectors for a standard
    third-angle layout:

        [plan ]  [      ]
        [front]  [ side ] [ iso ]
                          [ title block (bottom-right) ]

    Returns JSON with:
      views: {name: {VIEW_X, VIEW_Y, half_w, half_h, look_at, camera, up}}
      warnings: list of layout problems (out-of-bounds, title-block overlap)
      suggestion: recommended page_w/page_h/scale if the layout does not fit

    object_name: name from show() — use "" to measure the current shape
    page_w/page_h: sheet size in mm (default A4 landscape 297×210)
    scale: drawing scale factor (default 1.0; use 2.0 for 2:1)
    views: subset of ["front","plan","side","iso"] to place
    title_block_w/h: reserved bottom-right area (default 150×30 mm)
    margin: page margin in mm (default 10)
    extents: [x, y, z] part sizes in mm — lays out from these numbers instead
        of a session object (use when the part isn't loaded, e.g. import failed)
    centroid: [x, y, z] look_at origin when using extents (default [0, 0, 0])

    Accuracy: front/plan/side positions are exact for orthographic projection.
    Iso position is approximate (75% of 3-D diagonal as half-extent) — verify
    with render_view() and adjust manually if the iso overlaps a neighbour.
    """
    return _session.suggest_view_layout(
        object_name,
        page_w,
        page_h,
        scale,
        views,
        title_block_w,
        title_block_h,
        margin,
        extents,
        centroid,
    )


@mcp.resource(
    "build123d://skill/drawing",
    mime_type="text/plain",
    description="The b123d-drawing engineering workflow skill: step-by-step guide for creating multi-view engineering drawings from build123d geometry (views, scale, annotation, lint, SVG/DXF/PDF export).",
)
def build123d_drawing_skill() -> str:
    """b123d-drawing engineering workflow skill."""
    from build123d_mcp.tools.install_skill import _load_raw

    return _load_raw()


@mcp.tool()
def install_skill(target: str = "claude", force: bool = False) -> str:
    """Copy the b123d-drawing engineering drawing skill into the current project.

    Writes the appropriate config file for the requested agent so the
    step-by-step drawing workflow is available in future sessions.

    target: one of "claude" (default), "agents-md", "cursor", "windsurf"
      - claude     → .claude/skills/b123d-drawing/SKILL.md  (Claude Code)
      - agents-md  → AGENTS.md  (Codex CLI, Antigravity, GitHub Copilot, Cline)
      - cursor     → .cursor/rules/b123d-drawing.mdc
      - windsurf   → .windsurfrules
    force: overwrite existing installation (default False)
    """
    from build123d_mcp.tools.install_skill import install_skill as _install

    return _install(target=target, force=force)


@mcp.prompt(
    name="start-cad-session",
    description="Prime a new CAD design session with the task description and workflow reminders.",
)
def start_cad_session(description: str) -> list[PromptMessage]:
    """Start a new CAD design session.

    Args:
        description: What you want to build.
    """
    text = f"""\
Design task: {description}

Workflow:
1. Call reset(), then execute 'from build123d import *' to start clean.
2. Build incrementally — small execute() calls are easier to debug than one large block.
3. After every execute(), call measure() to verify geometry (check volume and topology.faces).
4. After every boolean (-, +, &), confirm topology.faces changed — unchanged counts mean the boolean failed.
5. Use show(shape, "name") to register important intermediate shapes; it prints vol + face count immediately.
6. Call render_view() only after measure() confirms the geometry is correct.
7. Call save_snapshot("name") before any experiment you might want to undo.
   For "what if?" proposals (add a hole, modify a feature) use the snapshot+restore loop:
   save_snapshot → mutate via execute → run analyses (measure/clearance/render_view) → restore_snapshot.
   This is cheaper and more accurate than redrawing geometry in matplotlib to evaluate a change.
8. For assemblies of two or more parts with a mechanical relationship (mounted, hinged, sliding),
   use Joints (RigidJoint/RevoluteJoint/LinearJoint/CylindricalJoint/BallJoint) rather than raw
   .move() — the relationship survives later changes. See build123d://quickref for examples.
9. When complete: export("part", "step,stl").
10. For 2D drawings, two cookbooks for two audiences:
   - build123d://drafting   — engineering drawings for fabrication handoff.
   - build123d://presentation — design-discussion diagrams (per-group colour,
     filled features, legends, axes, titles). Read this when the audience is
     a human reviewing a design rather than a fabricator.

Read the build123d://quickref resource before writing execute() code — it has accurate API syntax.
Read the build123d://bd_warehouse resource for fastener/bearing/thread catalogue and usage patterns.
Call workflow_hints() if unsure which tool to use next.
"""
    return [PromptMessage(role="user", content=TextContent(type="text", text=text))]
