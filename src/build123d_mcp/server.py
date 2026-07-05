import contextvars
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import PromptMessage, TextContent, ToolAnnotations

from build123d_mcp.tools._marshal import marshal_render_drawing, marshal_render_view
from build123d_mcp.worker import WorkerSession

_INSTRUCTIONS = """\
Persistent CAD modeling server for build123d. Use these tools for ANY task that
builds, modifies, measures, or renders 3D geometry or engineering drawings —
"model this part", "build this from the technical drawing", "does A fit inside
B", "make a drawing of X" — instead of writing standalone Python scripts run
via the shell. The server keeps a persistent build123d session, so you can
build incrementally with execute(), verify each step numerically with measure()
(volume, bounding box, face inventory), inspect visually with render_view(),
check fits with clearance(), and undo experiments with snapshots — a feedback
loop a one-shot script cannot give.

Quick start: execute("from build123d import *"), build in small steps,
register parts with show(part, "name"), measure() after every boolean,
export() when done. Read the build123d://quickref resource before writing
build code. Step-by-step workflows: build123d://skill/modeling (build 3D
parts, incl. from technical drawings) and build123d://skill/drawing
(multi-view engineering drawings); install either into the project with
install_skill().
"""

# --- MCP tool annotations (#368) --------------------------------------------- #
# Client-side UX hints, NOT enforcement — the security model is unchanged. They let
# clients auto-approve read-only queries so the tight execute()→measure()→execute()
# verify loop isn't gated by a prompt on every read. readOnlyHint reflects the tool's
# PURPOSE: a query tool that can write an OPTIONAL, caller-directed output file
# (render_view / render_drawing / script via save_to=) is still read-only — its default
# is a query and the file is a directed output, not a surprising mutation. idempotentHint
# is set only on mutating-but-idempotent ops (per the spec it's meaningful only when
# readOnlyHint is false). No wire/behaviour change for clients that ignore annotations.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

mcp = FastMCP("build123d-mcp", instructions=_INSTRUCTIONS, stateless_http=True)
_session: WorkerSession
_session_var: contextvars.ContextVar[WorkerSession | None] = contextvars.ContextVar(
    "b123d_session", default=None
)


def _resolve_session() -> WorkerSession:
    """Return the per-request session (HTTP) or the module-level singleton (stdio)."""
    s = _session_var.get()
    return s if s is not None else _session


def configure(session: WorkerSession) -> None:
    """Set the module-level session singleton.

    Called by ``cli.main()`` at startup; kept here so the tool closures resolve
    ``_session`` at this module's scope. Whether a part library is configured is
    read from ``session.has_library`` rather than tracked separately.
    """
    global _session
    _session = session


# Live-session viewer publisher (build123d_mcp.viewer.ViewerPublisher), or None
# when --viewer-socket was not given. Set by start_viewer().
_viewer = None


def start_viewer(socket_path: str):
    """Bind the live-viewer UDS and start broadcasting mesh deltas.

    Called by ``cli.main()`` when ``--viewer-socket`` / ``BUILD123D_VIEWER_SOCKET``
    is set. The publisher runs on its own daemon thread in this (server) process,
    never on the agent path. A worker restart triggers a viewer RESET so clients
    drop their now-stale scene.
    """
    global _viewer
    from build123d_mcp.viewer import ViewerPublisher

    _viewer = ViewerPublisher(socket_path)
    _viewer.start()
    try:
        _resolve_session().set_on_restart(_viewer.reset)
    except Exception:  # noqa: BLE001 - the viewer must never break startup
        pass
    return _viewer


def _publish_deltas() -> None:
    """Refresh the viewer's scene cache with changed shapes and broadcast them.

    Runs whenever the viewer socket is configured (``_viewer`` is set), even with
    no client attached, so the server-side cache stays current and a viewer that
    attaches at any time (early or late) gets a correct full-scene dump.
    Broadcasting to zero clients is a no-op. A pure agent run (no
    ``--viewer-socket``) does no work here. Failures are swallowed: a viewer
    problem must never turn a successful tool call into an error.
    """
    viewer = _viewer
    if viewer is None:
        return
    # The viewer is bound to the module-singleton session. In HTTP multi-session
    # mode a per-request tenant session is set on _session_var; do not publish its
    # geometry into the shared viewer stream, which would leak between tenants.
    if _session_var.get() is not None:
        return
    try:
        deltas = _resolve_session().pull_viewer_deltas()
    except Exception:  # noqa: BLE001 - viewer plumbing must not affect the tool result
        return
    if not isinstance(deltas, dict):
        return
    for name in deltas.get("remove", []):
        viewer.remove(name)
    for name, mesh in deltas.get("upsert", {}).items():
        try:
            verts, tris = mesh
            viewer.upsert(name, verts, tris)
        except Exception:  # noqa: BLE001 - skip a malformed mesh, keep the rest
            continue


def _publish_reset() -> None:
    """Tell viewer clients to clear their scene (the session was reset)."""
    viewer = _viewer
    if viewer is None or _session_var.get() is not None:
        return
    viewer.reset()


def http_app():
    """Return the FastMCP ASGI app for use with an ASGI server (e.g. uvicorn).

    **Single-session mode (default)**: all HTTP requests share the module-level
    WorkerSession set by ``configure()``.  This is correct for single-user
    deployments (one operator, one CAD namespace).  Concurrent clients will
    interleave operations in the same session.

    **Multi-session mode**: host middleware can isolate tenants by setting
    ``_session_var`` to a per-request ``WorkerSession`` before the MCP handler
    runs; ``_resolve_session()`` will then return that session instead of the
    singleton.  No such middleware is included — this hook exists for embedders.
    """
    return mcp.streamable_http_app()


@mcp.tool(annotations=_MUTATING)
def execute(code: str) -> str:
    """Execute build123d Python code in the persistent session. Errors include automatic fix hints — read them before retrying. Use show(shape, name) to register named objects (name defaults to 'shape'); show() immediately prints volume and face count confirming the shape is non-empty. After any boolean operation (-, +, &) call measure() to confirm it succeeded (check topology.faces). named_face(shape, name) is a built-in helper: named_face(box, 'top') returns the highest-Z face, 'bottom'/'front'/'back'/'left'/'right' work similarly. find_edges(shape, geom='circle', radius=4.25, at_z=10.2, length=None, tol=0.05) filters edges for fillet/chamfer selection and prints what matched. Analysis primitives are callable IN code and return real Python objects so you compose (filter, do arithmetic) instead of copying numbers out of a tool result: measure(shape) -> dict (measure(part)['volume']), clearance(a, b) -> dict, cross_sections(shape) -> list of {position,area}, find_holes(shape) -> hole records with .location (an (x,y,z) tuple), .diameter, .depth, … ([h for h in find_holes(part) if h.location[0] < 5]); find_bosses(shape) / find_countersinks(shape) / find_hole_patterns(shape) return recogniser records too; align_check(a, b, axis='Z', mode='flush') -> dict (align_check(a,b)['delta'] is a float). shape defaults to the current shape, and measure/clearance/cross_sections stay bounded on large shapes. save_json(name, obj) writes structured analysis data (face inventories, hole tables) to a server scratch file and returns its path — use it instead of printing large results; open()/os stay blocked."""
    from build123d_mcp.tools.execute import execute_code

    result = execute_code(_resolve_session(), code)
    _publish_deltas()
    return result


@mcp.tool(annotations=_READ_ONLY)
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
    """Render model. Auto-detects 3D vs 2D: solids use the VTK tessellation path; 2D shapes (Sketches, edge Compounds, dimensioned drawings) use the ezdxf+matplotlib raster path — review dimensioned drawings the same way as 3D parts. Renders confirm appearance, not geometry — verify booleans with measure() first. format: 'png' (raster, default), 'svg' (HLR line drawing, works without a display), 'dxf' (HLR projection as parseable polylines for downstream 2D CAD), or 'both' (PNG + SVG together). If the PNG path fails (headless host), falls back to SVG automatically. direction: top, front, side, iso. azimuth/elevation: camera rotation in degrees applied after the direction preset. objects: comma-separated names or name:color pairs e.g. 'u_frame:blue,roller:red' (default: all, auto-coloured). quality: standard, high. clip_plane: x, y, z to slice; clip_at: absolute world coordinate along that axis (default: each mesh's midpoint). save_to: optional file path; for format='both' writes <save_to>.png and <save_to>.svg. mode: 'auto' (default; no solids + flat in Z = 2D), or '2d'/'3d' to force a pipeline when auto-detection picks wrong (e.g. a Compound mixing a Sketch and a solid routes to 3D); the path used is reported as 'Rendered via <mode> pipeline.' colors: optional dict mapping object names and special layer keys (`_dims`, `_labels`) to colour names or '#aabbcc'; overrides name:color syntax and the default dimension colour (2D PNG/SVG only; ignored for 3D and DXF). label_objects: when true, each named object is labelled at its centroid in the PNG. highlights: optional list of entities to label, e.g. [{"object": "bracket", "type": "edge", "index": 5, "label": "hinge_edge"}]; type is 'face', 'edge', or 'vertex', index matches shape.faces()/edges()/vertices(); the object must be registered with show() and in the rendered set. Labels are PNG-only."""
    result = _resolve_session().render_view(
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


@mcp.tool(annotations=_READ_ONLY)
def measure(object_name: str = "", density: float = 0.0, material: str = "") -> str:
    """Measure a shape and return a complete geometric summary: volume (mm³), surface area (mm²), topology (face/edge/vertex counts), bounding box with per-axis size and center, volumetric center of mass, 6-component inertia tensor (Ixx/Iyy/Izz/Ixy/Ixz/Iyz), and a face-type inventory classifying every face as Plane/Cylinder/Cone/Sphere/Torus/BSpline with area and type-specific params (e.g. cylinder diameter and axis); identical faces are collapsed with a count, non-analytic sliver faces folded into one summary line. Prefer measure over render_view for verifying geometry — numbers are unambiguous. topology is the fastest confirmation that a boolean operation succeeded: a failed cut leaves face/edge/vertex counts unchanged. object_name: named object from show() (default: current shape). density (g/cm³) or material preset (steel, stainless, aluminum/6061, brass, copper, titanium, abs, pla, petg, nylon) adds mass_g and scales inertia to true mass moments in g·mm²."""
    return _resolve_session().measure(object_name, density, material)


@mcp.tool(annotations=_READ_ONLY)
def validate(object_name: str = "") -> str:
    """Check whether a shape would pass a CAD validity gate before exporting it. Returns a PASS/FAIL verdict plus JSON (passes_gate, n_solids, volume, is_manifold, brep_valid, reasons). The gate mirrors what CAD scorers and downstream tools require: a well-formed (BRepCheck), watertight, manifold solid with non-zero volume. A FAIL means a STEP/STL export would be rejected outright (e.g. CADGenBench scores it zero) — common causes are a leftover 2D sketch or open shell as the current shape, an un-fused compound, or a degenerate boolean result. Run this immediately before export() on any part you intend to submit or hand off. object_name: named object from show() (default: current shape)."""
    return _resolve_session().validate(object_name)


@mcp.tool(annotations=_READ_ONLY)
def locate_gate_defects(object_name: str = "") -> str:
    """Report WHERE a solid fails the validity gate, with 3D coordinates — so you can fix the exact edge/face instead of guessing. validate()/export() tell you WHAT is wrong (e.g. "1 non-manifold edge", "BRepCheck failed") but not where; call this when validate() FAILs to get a per-defect list: brep_invalid_face (face index + center + BRepCheck status, e.g. an unorientable BSpline), open_edge / nonmanifold_edge (B-rep edge midpoint + faces_incident), and the mesh self-touches a CAD scorer rejects — mesh_nonmanifold_edge (edge midpoint) and mesh_nonmanifold_vertex (corner-to-corner touch point). Each defect includes a generic repair hint. An empty list means the part passes the structural checks. Bounded out-of-process (it mesh-checks), so a huge part returns a clean budget error rather than hanging. object_name: named object from show() (default: current shape)."""
    return _resolve_session().locate_gate_defects(object_name)


# read-only despite the name: it perturbs parameters in a subprocess and never mutates
# the live session (don't "fix" this to _MUTATING).
@mcp.tool(annotations=_READ_ONLY)
def design_audit(epsilon: float = 0.1, max_params: int = 8) -> str:
    """Audit the current session program as a *design*, not just a shape: surface its named numeric parameters (Θ) and test how robust each is to editing. Parses the assembled program (see script()) for top-level numeric assignments (e.g. `plate_thickness = 5.0`), then rebuilds the program with each parameter nudged ±epsilon (default ±10%) in a hard-bounded subprocess (the live session is never mutated) and runs the validity gate on each result. Returns JSON: {parameters, baseline, audit:[{name, value, perturbations:[{delta_pct (realized), new_value, discrete_step?, rebuilt, passes_gate, volume_delta_pct, reasons?}], brittle}], summary:{robust, brittle, inconclusive, ...}, note}. A parameter is `brittle` if a small change fails to rebuild or drops below the validity gate — the thin-wall / coordinate-reasoning failure mode where a valid *shape* is not an editable *design* (Arko-T §6); a parameter reassigned at the top level is `inconclusive` (perturbation is overwritten), not counted as robust. If no named parameters are found, the program uses inline magic constants and the note advises hoisting them to a parameter block. Known limitation: only literal-valued top-level names are surfaced as Θ — a derived parameter (`radius = diameter / 2`) is not listed, though perturbing its upstream literal flows through. Bounded by a wall-clock budget and max_params (returns a partial report rather than risking a timeout). epsilon: relative nudge, 0<epsilon<1. max_params: cap on parameters audited."""
    return _resolve_session().design_audit(epsilon, max_params)


# --- Experimental tools (off by default) ------------------------------------ #
# verify_spec / suggest_spec are NOT registered at import — they are gated behind
# --experimental / BUILD123D_EXPERIMENTAL and only wired in by
# register_experimental_tools() below. Not production-ready: field data shows a
# `conforms: true` verdict can read to an autonomous agent as a stop signal, so we
# keep them out of the default tool set until that's addressed (#362).
def verify_spec(spec: str = "", spec_path: str = "", object_name: str = "") -> str:
    """Verify the built solid against a declared design-intent spec — did you build what was requested? Checks requested features/constraints against the actual geometry and returns an evidence-tiered conformance report; unlike validate() (is the solid valid?) this answers requested-vs-built. Provide the spec as inline JSON (spec=) or a .json file path (spec_path=). Supported spec keys: envelope_mm {x/y/z:[lo,hi]} (bbox size in range), solid {count, valid}, volume_mm3 {min,max}, features:[{kind:"hole_pattern",pattern:"bolt_circle"|"linear_array",holes,bcd_mm|pitch_mm,diameter_mm} | {kind:"hole",count,diameter_mm,depth_mm,through:bool,counterbore:{diameter_mm,depth_mm}|true|false,spotface:{...}} | {kind:"boss",diameter_mm,height_mm} | {kind:"countersink",count,major_diameter_mm,drill_diameter_mm,included_angle_deg,depth_mm} | {kind:"material_at_point",point:[x,y,z],expect:"solid"|"void"} | {kind:"wall_thickness_at",point:[x,y,z],direction:[dx,dy,dz],expect_mm:[lo,hi]}] (counterbore/spotface: true=present, false=absent; a depth_mm matches the recognizer-measured depth which may differ from a drawing callout; material_at_point asks is-this-point-inside-the-solid to disambiguate add-vs-remove features the recognizers can't see; wall_thickness_at measures the local wall thickness along a line through the point and range-checks it — the thin-wall blind spot; both are measured-tier but frame-DEPENDENT: absolute coordinates tied to the part's own frame, verifying a same-session build, not portable across a repositioned part; a point in no wall reads UNVERIFIED), parameters:[{name,min,max}] (top-level numeric assignment in range), min_wall_mm (global minimum wall ≥ value, measured tier via augura), targets:[{name,verifiable:false}] (→UNVERIFIED). Returns JSON: {conformance:[{requirement, status:PASS|FAIL|UNVERIFIED, tier:measured|structural|recognised|unverified, actual/found/hint}], summary:{pass,fail,unverified,conforms}, note}. conforms = no FAILs; UNVERIFIED requirements are NOT met (out of scope), never counted as passing. Dimensions match within max(0.1mm, 1%); counts exact; an unrecognised feature kind is UNVERIFIED, not a false FAIL. Not a certification. Re-run after edits as a regression/acceptance gate. object_name: named object from show() (default: current shape)."""
    return _resolve_session().verify_spec(spec, spec_path, object_name)


def suggest_spec(object_name: str = "") -> str:
    """Draft a starter design-intent spec from the current (or named) shape, so you can edit detected values instead of authoring a verify_spec spec from scratch. Introspects the shape with the same primitives verify_spec checks against — bounding box (→ envelope_mm), the validity gate (→ solid), volume, feature recognition (→ hole/hole_pattern/boss features), and top-level numeric parameters — and returns JSON {spec, note}. The `spec` describes what was BUILT (envelope/volume use a ±2% band, parameters ±10% — editable defaults); review and edit each value against your intended drawing, then pass the `spec` object to verify_spec(). NOT captured: absolute positions, and cosmetic/other features (fillets, chamfers, pockets, ribs) the recognizers don't cover — add those manually. object_name: named object from show() (default: current shape)."""
    return _resolve_session().suggest_spec(object_name)


def register_experimental_tools() -> None:
    """Register the experimental, not-yet-production-ready tools (verify_spec,
    suggest_spec) into the served tool set. Called by ``cli.main()`` ONLY when
    ``--experimental`` / ``BUILD123D_EXPERIMENTAL`` is set — they are off by default
    (#362). Idempotent: FastMCP warns on duplicate registration, so guard on it."""
    existing = {t.name for t in mcp._tool_manager.list_tools()}
    for fn in (verify_spec, suggest_spec):
        if fn.__name__ not in existing:
            mcp.tool(annotations=_READ_ONLY)(fn)  # both are read-only conformance queries


# Optional tool groups that a context-sensitive deployment can drop to slim the tool
# surface (#367). Default keeps them — this is opt-OUT, so no existing workflow breaks.
_TOOL_GROUPS = {
    "drawing": (
        "inspect_drawing",
        "view_axes",
        "lint_drawing",
        "render_drawing",
        "save_drawing_annotations",
        "suggest_view_layout",
    ),
}
# The part-library tools are dead weight without a library — they only answer "No part
# library configured" — so they auto-hide when no --library is set (not a manual group).
_LIBRARY_TOOLS = ("search_library", "load_part")


def apply_tool_visibility(
    disabled_groups: tuple[str, ...] = (), *, has_library: bool = True
) -> None:
    """Trim optional tools from the served surface to reduce per-request schema cost.

    All tools register at import; this removes (a) each group named in
    ``disabled_groups`` — an opt-out for fleets/benchmark harnesses that never touch it —
    and (b) the part-library tools when ``has_library`` is false. Called by
    ``cli.main()`` after ``configure()``. Unknown group names are reported and ignored.
    """
    remove: set[str] = set()
    for group in disabled_groups:
        if group not in _TOOL_GROUPS:
            print(
                f"WARNING: unknown --disable-tool-groups value '{group}'; "
                f"known groups: {', '.join(sorted(_TOOL_GROUPS))}",
                file=sys.stderr,
            )
            continue
        remove.update(_TOOL_GROUPS[group])
    if not has_library:
        remove.update(_LIBRARY_TOOLS)
    for name in remove:
        try:
            mcp.remove_tool(name)
        except Exception:  # noqa: BLE001 - tolerate an already-absent tool
            pass


@mcp.tool(annotations=_READ_ONLY)
def clearance(object_a: str, object_b: str) -> str:
    """Spatial relationship between two named shapes. Returns JSON with `clearance` (mm), `status` (one of: apart, touching, containing, interpenetrating), `containment` (a_in_b, b_in_a, or neither), and `intersection_volume` / `a_volume_outside_b` / `b_volume_outside_a` for overlap quantification. Reads `clearance` differently per status: apart=gap, containing=wall thickness from inner surface to outer hull (use this to verify a pocket fits inside a plate), touching=0, interpenetrating=0 (check intersection_volume + a_volume_outside_b for the wall-piercing case). object_a, object_b: names from show()."""
    return _resolve_session().clearance(object_a, object_b)


@mcp.tool(annotations=_READ_ONLY)
def analyze_printability(
    object_name: str = "",
    support_angle: float = 45.0,
    nozzle: float = 0.4,
    min_perimeters: int = 2,
    build_volume: str = "",
    bed_tol: float = 0.001,
    min_feature: float = 0.5,
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
    bed_tol: Z tolerance in mm for identifying bed-contact faces (default 0.001);
        raise it for parts whose bottom faces sit slightly off Z=0.
    min_feature: minimum vertical feature size in mm to flag (default 0.5).
    """
    return _resolve_session().analyze_printability(
        object_name,
        support_angle,
        nozzle,
        min_perimeters,
        build_volume,
        bed_tol,
        min_feature,
    )


@mcp.tool(annotations=_READ_ONLY)
def cross_sections(object_name: str = "", axis: str = "Z", num_slices: int = 10) -> str:
    """Compute cross-sectional areas at evenly spaced planes along an axis. Returns a list of {position, area} pairs. axis: X, Y, or Z (default Z). num_slices: number of planes (default 10, minimum 2). Useful for detecting internal voids, wall-thickness variation, or verifying that a shape's cross-section profile matches a reference. object_name: named object from show() (default: current shape)."""
    return _resolve_session().cross_sections(object_name, axis, num_slices)


@mcp.tool(annotations=_IDEMPOTENT)
def export(filename: str, format: str = "step", object_name: str = "") -> str:
    """Export model. format: step, stl, dxf, svg, or comma-separated list e.g. 'step,stl' or 'dxf,svg'. 3D shapes (solids) export to step/stl; 2D shapes (Sketches and dimensioned drawings composed via build123d.drafting) export to dxf/svg. Mixing 2D and 3D formats for the same shape errors with a clear message. object_name: named object from show(), '*' to export all named shapes as a combined assembly (default: current shape). STEP exports carry the session names as labels — single-object exports use the object_name, '*' exports produce a Compound labelled 'assembly' with each child labelled by its show() name. Downstream CAD tools (FreeCAD, Fusion) will see the structured assembly with named bodies. Use dxf for engineering-drawing handoff to other CAD tools; svg for embedding in docs/wikis. The result echoes the exported shape's volume/bbox/face count (or bbox/edge count for 2D) as a final sanity check that the right, non-degenerate object was written."""
    return _resolve_session().export_file(filename, format, object_name)


@mcp.tool(annotations=_READ_ONLY)
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
    return _resolve_session().inspect_drawing(objects, svg_path)


@mcp.tool(annotations=_READ_ONLY)
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
    return _resolve_session().view_axes(
        tuple(viewport_origin),
        tuple(viewport_up) if viewport_up is not None else (0.0, 1.0, 0.0),
        tuple(look_at) if look_at is not None else (0.0, 0.0, 0.0),
    )


@mcp.tool(annotations=_READ_ONLY)
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
    return _resolve_session().lint_drawing(svg_path, drawing_scale, view_shape_names)


@mcp.tool(annotations=_READ_ONLY)
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
    result = _resolve_session().render_drawing(svg_path, width, save_to)
    return marshal_render_drawing(result, svg_path, save_to)


@mcp.tool(annotations=_MUTATING)
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
    return _resolve_session().save_drawing_annotations(svg_path)


@mcp.tool(annotations=_READ_ONLY)
def search_library(query: str = "") -> str:
    """Search the part library. query: keywords matched against name, description, tags, category (empty returns all). Returns name, category, description, tags, and full parameter specs including types, defaults, and descriptions."""
    if not _resolve_session().has_library:
        return "No part library configured. Start the server with --library PATH or set BUILD123D_PART_LIBRARY."
    return _resolve_session().search_library(query)


@mcp.tool(annotations=_MUTATING)
def load_part(name: str, params: str = "") -> str:
    """Load a named part from the library into the session. name: part name from search_library. params: optional JSON object of parameter overrides e.g. '{\"od\": 8.0, \"length\": 20.0}' — unspecified params use their defaults. The part is registered as a named object and becomes current_shape."""
    if not _resolve_session().has_library:
        return "No part library configured. Start the server with --library PATH or set BUILD123D_PART_LIBRARY."
    result = _resolve_session().load_part(name, params)
    _publish_deltas()
    return result


@mcp.tool(annotations=_IDEMPOTENT)
def save_snapshot(name: str) -> str:
    """Save a named checkpoint of the current geometric state (current_shape and the show() object registry).
    The Python variable namespace is NOT saved — only geometry. Call this before risky experiments so you can
    restore known-good geometry without re-running all prior execute() calls."""
    return _resolve_session().save_snapshot(name)


@mcp.tool(annotations=_IDEMPOTENT)
def restore_snapshot(name: str) -> str:
    """Restore geometric state from a previously saved snapshot (current_shape and the show() registry).
    The Python variable namespace is NOT restored — execute() calls made after the snapshot are still in scope,
    but current_shape and all show() objects revert to what they were at snapshot time.
    Raises an error if the snapshot name does not exist."""
    result = _resolve_session().restore_snapshot(name)
    _publish_deltas()
    return result


@mcp.tool(annotations=_READ_ONLY)
def diff_snapshot(snapshot_a: str, snapshot_b: str = "", format: str = "text") -> str:
    """Compare two snapshots by geometry metrics (volume, topology, bounding box). snapshot_b defaults to current session state if omitted. format: 'text' (default, human-readable) or 'json' (structured, for programmatic consumption)."""
    return _resolve_session().diff_snapshot(snapshot_a, snapshot_b, format)


@mcp.tool(annotations=_READ_ONLY)
def session_state() -> str:
    """Return a structured JSON snapshot of the current session: current_shape metrics, all named objects (replaces list_objects) with geometry stats, snapshot names, and a variables summary of the Python namespace (type + volume for shapes, type + length for collections, type + value for scalars). Use this to orient after a reset, restore, or multi-step build to confirm what geometry and variables are active."""
    return _resolve_session().session_state()


@mcp.tool(annotations=_READ_ONLY)
def health_check() -> str:
    """Verify that render and export dependencies are working. Tests PNG render (VTK), SVG render (build123d HLR), STEP export, and STL export with a trivial shape. Returns JSON with ok/error per capability. Run at session start if you suspect a missing dependency."""
    return _resolve_session().health_check()


@mcp.tool(annotations=_DESTRUCTIVE)
def reset() -> str:
    """Clear the current session back to empty state, including all snapshots."""
    result = _resolve_session().reset()
    _publish_reset()
    return result


@mcp.tool(annotations=_READ_ONLY)
def shape_compare(object_a: str, object_b: str) -> str:
    """Compare two named shapes (from show()) by geometry metrics plus localized surface deviation. Keeps volume, bbox, topology, and center deltas, and adds a bounded surface diff that locates WHERE the geometry changed: max_deviation (largest real change, noise-floored so a no-op reads ~0), changed region(s) (centroid/bbox + exact added_volume/removed_volume), magnitude_method (exact_boolean = exact displacement+volumes; exact_volume_mesh_displacement = exact volumes, mesh-estimated displacement, e.g. a cut/flush-fill; mesh_estimate = boolean skipped/failed), and unchanged_elsewhere. The exact B-rep boolean is clipped to the changed region and runs subprocess-bounded, falling back to the flagged mesh estimate on large/spread edits. For editing, this is model↔input verification, not a score: confirm the changed region(s) and add/remove volumes match the request and unrelated regions stayed put. A tangential move (sliding a hole) or a sub-resolution edit on a very large part yields no region — unchanged_elsewhere then means "no change above the detection floor", not a guarantee; cross-check volume/bbox/center deltas and find_holes."""
    return _resolve_session().shape_compare(object_a, object_b)


@mcp.tool(annotations=_READ_ONLY)
def find_holes(object_name: str = "") -> str:
    """Recognise drilled holes on a session object (defaults to current shape). Coaxial internal cylinders are grouped into one record per hole: drill + counterbore + spotface stacks, keyway-split bores, and bores interrupted by crossing holes all count once. Returns JSON: {count, holes: [{axis (drilling direction, unit vector), location (opening point), diameter, depth (bore top to deep end; drill-point cone excluded), bottom: through|flat|drill_point|unknown, cbore: {diameter, depth}|null, spotface: {diameter, depth}|null}]}. Countersinks read as openings (not steps); threads and non-cylindrical features are not recognised."""
    return _resolve_session().find_holes(object_name)


@mcp.tool(annotations=_READ_ONLY)
def find_hole_patterns(object_name: str = "") -> str:
    """Recognise hole patterns on a session object (defaults to current shape): ≥3 identical-spec holes equally spaced on a circle → bolt_circle (center, diameter/BCD), collinear at constant pitch → linear_array (pitch, direction). Returns JSON: {count, patterns: [{type, holes: [HoleFeature records], center/diameter | pitch/direction}]}. Each hole belongs to at most one pattern; make_drawing already annotates these automatically."""
    return _resolve_session().find_hole_patterns(object_name)


@mcp.tool(annotations=_READ_ONLY)
def find_bosses(object_name: str = "") -> str:
    """Recognise external cylindrical bosses on a session object (defaults to current shape), including a turned part's OD — filter on diameter against the part envelope for local bosses only. Returns JSON: {count, bosses: [{axis (base toward free end), location (free-end point), diameter, height}]}."""
    return _resolve_session().find_bosses(object_name)


@mcp.tool(annotations=_READ_ONLY)
def find_countersinks(object_name: str = "") -> str:
    """Recognise countersinks (conical screw-head recesses) on a session object (defaults to current shape) — the feature find_holes reports only as a plain opening. A countersink is an internal cone flaring from a drilled bore out to a larger opening, coaxial with the drill; drill-point cones and external edge chamfers are excluded. Returns JSON: {count, countersinks: [{location (opening centre), axis (into the part), major_diameter (countersink Ø at the surface), drill_diameter, included_angle (deg, e.g. 82/90/100/120), depth}]}. object_name: named object from show() (default: current shape)."""
    return _resolve_session().find_countersinks(object_name)


@mcp.tool(annotations=_READ_ONLY)
def align_check(object_a: str, object_b: str, axis: str = "Z", mode: str = "flush") -> str:
    """Check alignment between two named objects along an axis. axis: X, Y, or Z. mode: flush (signed distance between bbox extremes — positive=A extends further), center (offset between bbox centroids), clearance (gap between nearest faces — positive=apart, negative=overlap). Returns JSON: {delta, axis, mode, object_a, object_b, interpretation}."""
    return _resolve_session().align_check(object_a, object_b, axis=axis, mode=mode)


# not read-only: with the optional label= arg it stores the descriptor in
# session.geometry_refs (persistent, cleared by reset(), shown in session_state()).
# Idempotent — the same label overwrites.
@mcp.tool(annotations=_IDEMPOTENT)
def resolve(object_name: str, selector: str, label: str = "") -> str:
    """Evaluate a selector expression against a named object and return a geometry descriptor. selector is a Python expression suffix applied to the object, e.g. '.faces().filter_by(Axis.Z).last()'. If label is given, the descriptor is stored in session.geometry_refs[label] and appears in session_state(). Returns JSON: {label, ref, object, selector, type, area/length, center, normal (for Face)}. The ref field uses @cad[object#label] format."""
    return _resolve_session().resolve(object_name, selector, label=label)


@mcp.tool(annotations=_READ_ONLY)
def script(save_to: str = "") -> str:
    """Return a single Python script assembled from all successfully executed code blocks in this session. Prepends 'from build123d import *' if not already present. If save_to is given, writes the script to that path and returns {script_path, blocks}; otherwise returns {script, blocks}. Useful for exporting a reproducible script after an interactive session."""
    return _resolve_session().script(save_to=save_to)


@mcp.tool(annotations=_MUTATING)
def import_cad_file(path: str, name: str = "") -> str:
    """Import a STEP (.step/.stp) or STL (.stl) file as a named object in the session. path: absolute or relative path to the file. name: name to register the shape under (defaults to the filename stem). The shape becomes both the named object and the current_shape. Returns volume, topology, and bounding box of the imported shape. After importing, use render_view() to visualise the shape, measure() for geometry queries, or shape_compare() to diff against a show() object. Note: STL imports produce a shell (volume=0) rather than a solid — render_view and measure still work, but clearance() and boolean operations require a solid. If you have both the original built shape and an imported copy in session.objects, render the imported one by name (e.g. objects='mypart') to avoid Z-fighting artifacts from two co-located shapes."""
    result = _resolve_session().import_cad_file(path, name)
    _publish_deltas()
    return result


@mcp.tool(annotations=_READ_ONLY)
def repair_hints(error_text: str) -> str:
    """Given an error message from execute(), return targeted fix suggestions for common build123d mistakes: wrong Location syntax, missing .part, CadQuery idioms, blocked imports, degenerate boolean results, fillet edge selection, and more. Pass the full error string from execute() or last_error()."""
    from build123d_mcp.tools.repair_hints import repair_hints as _repair_hints

    return _repair_hints(error_text)


@mcp.tool(annotations=_READ_ONLY)
def last_error() -> str:
    """Return details of the last failed execute() call: exception type, message, and (for runtime and syntax errors) line number and a 5-line excerpt around the failing line. Security errors include a message but no line/excerpt. Returns {\"error\": null} if the last execute() succeeded or no execute() has failed yet. Call this immediately after an execute() error to get the exact failing line — much faster than re-reading the submitted code."""
    return _resolve_session().last_error()


@mcp.tool(annotations=_READ_ONLY)
def version() -> str:
    """Return the installed versions of the build123d-mcp server, its key dependencies (build123d, build123d-drafting-helpers), and the companion packages importable inside execute() (bd_warehouse for threads/fasteners/gears/bearings, augura for printability analysis). Use this to confirm which server build is running — e.g. to check whether a feature or fix is present, or whether the client is talking to a stale install."""
    # Computed in-process (pure importlib.metadata, same venv as the worker), so
    # it still answers when the worker subprocess is down — exactly the stale /
    # broken-install case this tool exists to diagnose.
    from build123d_mcp.tools.version import version_info

    return "\n".join(f"{name}: {ver}" for name, ver in version_info().items())


@mcp.tool(annotations=_READ_ONLY)
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
   Compose in code: measure/clearance/cross_sections/find_holes/find_bosses/
   find_countersinks/find_hole_patterns/align_check are callable INSIDE execute() and
   return real objects — measure(part)["volume"], [h for h in find_holes(part) if
   h.location[0] < 5] — so filter/compute in code instead of copying numbers out of a
   JSON tool result. The standalone tools remain for one-shot queries.

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

9. BD_WAREHOUSE FASTENERS, THREADS, GEARS, BEARINGS
   bd_warehouse ships with this server — import it directly in execute() instead of
   hand-rolling threads, fasteners, gears, or bearings (a 5-line Thread beats a fiddly
   helical sweep). version() lists installed companion packages.
   Read the build123d://bd_warehouse resource before scripting any fastener geometry.
   Always probe sizes before writing the script to get the correct string format:
     execute('from bd_warehouse.fastener import CounterSunkScrew; print(CounterSunkScrew.sizes("iso10642"))')
   Use CounterSinkHole/TapHole/ClearanceHole/CounterBoreHole with the fastener object —
   never compute head geometry or tap-drill diameters manually.

10. COMPLEX / HEAVY BUILDS
   The execute() timeout (default 120s) hard-limits a SINGLE call, not the session: if a
   call times out only that step is dropped and the session is rebuilt from your prior
   execute() history (variables, shapes, named objects come back). So the first move is to
   stay in-session and go smaller/longer, NOT to leave for Bash:
     a) Build incrementally in several smaller execute() calls (a timed-out step is dropped,
        the rest of the session survives), and/or
     b) Raise the ceiling: --exec-timeout N or BUILD123D_EXEC_TIMEOUT=N (also extends the
        import budget for heavy STEP files).
   Only if a single unavoidable op (IsoThread, a multi-body fillet, a very high-face-count
   boolean) still won't fit, drop out for that one op: write it as a Python script, run it
   with Bash, then import_cad_file("part.step", "part") and verify with measure()/render_view().

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
   STL imports produce a shell (volume=0); render_view and measure work, but clearance()
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
    "build123d://drafting-api",
    mime_type="text/plain",
    description="Auto-generated API reference for build123d-drafting-helpers: exact signatures and one-line descriptions for every public class (Dimension, Leader, TitleBlock, Drawing, ...) and function, generated from the installed library so it always matches what execute() imports.",
)
def build123d_drafting_api() -> str:
    """build123d-drafting-helpers API reference — generated from the installed library."""
    return _resolve_session().drafting_api()


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
    return _resolve_session().session_state()


@mcp.resource(
    "build123d://bd_warehouse",
    mime_type="text/plain",
    description="Catalogue of pre-built parametric parts in bd_warehouse: bearings, fasteners, gears, pipes, threads, and more.",
)
def build123d_bd_warehouse() -> str:
    """bd_warehouse component catalogue."""
    from build123d_mcp.bd_warehouse_resource import build_bd_warehouse_text

    return build_bd_warehouse_text()


@mcp.tool(annotations=_READ_ONLY)
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
      free_space: {name: {above/below: {x, y, h}, left/right: {x, y, w}}} — the
        empty rectangle outside each view edge, bounded by neighbouring views,
        the title block, and the margins; budget dimension tiers (n × tier
        pitch must fit in h/w) before placing annotations
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
    return _resolve_session().suggest_view_layout(
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

    return _load_raw("drawing")


@mcp.resource(
    "build123d://skill/modeling",
    mime_type="text/plain",
    description="The b123d-modeling workflow skill: step-by-step guide for building 3D parts and assemblies with build123d via this server — including extracting a spec from a technical drawing, the incremental build/measure/render loop, snapshots, and export.",
)
def build123d_modeling_skill() -> str:
    """b123d-modeling workflow skill."""
    from build123d_mcp.tools.install_skill import _load_raw

    return _load_raw("modeling")


@mcp.tool(annotations=_MUTATING)
def install_skill(target: str = "claude", force: bool = False, skill: str = "drawing") -> str:
    """Copy a b123d workflow skill into the current project.

    Writes the appropriate config file for the requested agent so the
    step-by-step workflow is available in future sessions.

    skill: which workflow to install (default "drawing")
      - drawing   → multi-view engineering drawings from build123d geometry
      - modeling  → build 3D parts/assemblies (incl. from technical drawings)
    target: one of "claude" (default), "agents-md", "cursor", "windsurf"
      - claude     → .claude/skills/<skill-dir>/SKILL.md  (Claude Code)
      - agents-md  → AGENTS.md  (Codex CLI, Antigravity, GitHub Copilot, Cline)
      - cursor     → .cursor/rules/<skill-dir>.mdc
      - windsurf   → .windsurfrules
    force: overwrite existing installation (default False)
    """
    from build123d_mcp.tools.install_skill import install_skill as _install

    return _install(target=target, force=force, skill=skill)


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
