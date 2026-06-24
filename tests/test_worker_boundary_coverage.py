"""Production-boundary regression coverage for session-stateful MCP tools (issue #182).

In production ``server.py`` holds a :class:`WorkerSession` proxy whose real
``Session`` state (objects, current_shape, namespace, geometry_refs, snapshots,
annotations, execute_history) lives in a worker subprocess. A tool that reads
that state must dispatch into the worker; one that calls its helper against the
empty parent proxy silently sees no state (the bug class fixed in #179).

Helper-level tests against an in-process ``Session`` cannot catch that: they pass
because the helper and the state share a process. This module pins the worker
boundary instead:

1. ``test_every_op_reachable_via_proxy`` — every op in the ``worker._OPS`` table
   resolves to a callable ``WorkerSession`` proxy (an ``@_op``-decorated stub or
   an explicit method), so no table entry is unreachable from server.py.
2. ``test_session_stateful_tool_sees_worker_state`` — each listed stateful tool,
   invoked through a real ``WorkerSession``, sees state that was created with
   ``execute()`` in the worker.
3. ``test_every_dispatch_op_is_classified`` — every dispatch op is either in the
   smoke inventory above or in an explicit, reasoned allowlist. A newly added
   worker-routed tool therefore fails CI until it is given smoke coverage or a
   documented exemption.

Boundary note: a tool added the #179 way (server.py calling a state-reading
helper directly, with no dispatch op) is broken in production and is regression-
guarded by tests/test_worker_session_tools.py and tests/test_session_resource.py;
this module guards the worker-routed pattern that is now the established norm.
"""

import json

import pytest

from build123d_mcp import worker
from build123d_mcp.worker import WorkerSession

# --------------------------------------------------------------------------- #
# The op table (single source of truth)                                        #
# --------------------------------------------------------------------------- #


def _dispatch_ops() -> set[str]:
    """Ops registered in the ``worker._OPS`` table (handler + timeout + params)."""
    return set(worker._OPS)


def test_every_op_reachable_via_proxy():
    """Every table op resolves to a callable on ``WorkerSession``.

    ``@_op``-decorated stubs and explicit methods (execute, reset) both count;
    an op registered in ``_OPS`` without a corresponding method would make the
    tool unreachable from server.py.
    """
    ws = WorkerSession.__new__(WorkerSession)  # attribute access only; no worker spawned
    for op in sorted(_dispatch_ops()):
        assert callable(getattr(ws, op)), f"op '{op}' has no callable proxy"


def test_stub_defaults_match_tool_function_defaults():
    """Stub-signature defaults must equal the tool function's own defaults.

    Omitted optionals are not sent over the wire, so the tool function's
    defaults are what actually applies; the stub defaults are the documented
    interface. If they drift, behaviour silently depends on whether a caller
    passes the argument explicitly.
    """
    import importlib
    import inspect

    checked = 0
    for op, spec in worker._OPS.items():
        path = getattr(spec.handler, "__tool_path__", None)
        if path is None:
            continue  # custom _op_<name> handler validates its own args
        module_name, _, func_name = path.partition(":")
        fn = getattr(importlib.import_module(module_name), func_name)
        fn_params = inspect.signature(fn).parameters
        # signature() follows __wrapped__ back to the @_op-decorated stub.
        stub_params = inspect.signature(getattr(WorkerSession, op)).parameters
        for pname, stub_p in stub_params.items():
            if pname == "self":
                continue
            assert pname in fn_params, f"{op}: stub param '{pname}' not on the tool function"
            if stub_p.default is inspect.Parameter.empty:
                continue
            assert stub_p.default == fn_params[pname].default, (
                f"{op}.{pname}: stub default {stub_p.default!r} != "
                f"tool-function default {fn_params[pname].default!r}"
            )
            checked += 1
    assert checked > 20, "default-sync check matched suspiciously few parameters"


# --------------------------------------------------------------------------- #
# Smoke inventory: each stateful tool must SEE worker-owned state              #
# --------------------------------------------------------------------------- #
#
# Each check receives a seeded WorkerSession plus a tmp_path and asserts the
# tool reflects state created in the worker (objects 'a'/'b', snapshot 'snap',
# execute_history). Were the tool reading the empty parent proxy, the seeded
# object would be absent and the assertion would fail.


def _measure(ws, tmp_path):
    r = json.loads(ws.measure("a"))
    assert "error" not in r and r["volume"] > 0


def _validate(ws, tmp_path):
    r = ws.validate("a")
    assert "PASS" in r  # 'a' is a seeded unit box → valid solid


def _locate_gate_defects(ws, tmp_path):
    r = ws.locate_gate_defects("a")
    assert "No validity defects" in r  # 'a' is a seeded valid unit box → no defects


def _clearance(ws, tmp_path):
    r = json.loads(ws.clearance("a", "b"))
    assert "error" not in r and "status" in r


def _shape_compare(ws, tmp_path):
    r = json.loads(ws.shape_compare("a", "b"))
    assert "error" not in r and "delta" in r


def _find_holes(ws, tmp_path):
    # The seeded boxes have no holes; the empty parent proxy doesn't know 'a'
    # at all, so a non-error reply proves the worker's objects were consulted.
    r = json.loads(ws.find_holes("a"))
    assert "error" not in r and r["holes"] == []


def _find_bosses(ws, tmp_path):
    r = json.loads(ws.find_bosses("a"))
    assert "error" not in r and r["bosses"] == []


def _find_hole_patterns(ws, tmp_path):
    r = json.loads(ws.find_hole_patterns("a"))
    assert "error" not in r and r["patterns"] == []


def _align_check(ws, tmp_path):
    r = json.loads(ws.align_check("a", "b", mode="center"))
    assert "error" not in r and "delta" in r


def _cross_sections(ws, tmp_path):
    r = json.loads(ws.cross_sections("a"))
    assert "error" not in r


def _resolve(ws, tmp_path):
    r = json.loads(ws.resolve("a", ".faces().sort_by(Axis.Z)[-1]"))
    assert r.get("type") == "Face"


def _suggest_view_layout(ws, tmp_path):
    r = json.loads(ws.suggest_view_layout("a"))
    assert "error" not in r and "views" in r


def _script(ws, tmp_path):
    r = json.loads(ws.script())
    # execute_history lives in the worker; the empty parent proxy yields 0 blocks.
    assert r["blocks"] >= 1 and "Box(1, 1, 1)" in r["script"]


def _session_state(ws, tmp_path):
    r = json.loads(ws.session_state())
    assert "a" in r["objects"] and "b" in r["objects"]


def _objects_types(ws, tmp_path):
    r = ws.objects_types()
    assert "a" in r and "b" in r


def _analyze_printability(ws, tmp_path):
    r = ws.analyze_printability(object_name="a")
    assert "finding" in r


def _diff_snapshot(ws, tmp_path):
    # The 'snap' snapshot captured the seeded objects; on an empty worker the
    # missing snapshot yields a non-JSON "Error:" string and json.loads raises.
    r = json.loads(ws.diff_snapshot("snap", format="json"))
    assert "a" in r["a"]["objects"] and "b" in r["a"]["objects"]


def _save_snapshot(ws, tmp_path):
    # The summary lists the captured geometry; "current_shape" appears only when
    # the worker holds the seeded state ("Geometry captured: none." when empty).
    r = ws.save_snapshot("s2")
    assert "current_shape" in r


def _restore_snapshot(ws, tmp_path):
    # Success says "restored"; an empty worker has no 'snap' and returns "Error:".
    r = ws.restore_snapshot("snap")
    assert "restored" in r.lower() and "current_shape" in r


def _last_error(ws, tmp_path):
    # Trigger a failure in the worker, then assert last_error reflects THAT error
    # — distinguishing the worker's error slot from a fresh {"error": null}.
    ws.execute("raise ValueError('boundary_marker')")
    r = json.loads(ws.last_error())
    # A fresh/unrouted worker returns {"error": null}; only the worker that ran
    # the failing snippet carries the marker in its recorded error detail.
    assert "boundary_marker" in json.dumps(r)


def _inspect_drawing(ws, tmp_path):
    # Session mode iterates session.objects (the worker-owned geometry seed);
    # an empty worker returns {"error": "No objects in session ..."}.
    r = json.loads(ws.inspect_drawing())
    assert "error" not in r and "a" in r["objects"] and "b" in r["objects"]


def _export_file(ws, tmp_path):
    out = tmp_path / "a.step"
    ws.export_file(str(out), "step", "a")
    assert out.exists() and out.stat().st_size > 0


def _render_view(ws, tmp_path):
    out = tmp_path / "a.png"
    ws.render_view(objects="a", save_to=str(out))
    assert out.exists() and out.stat().st_size > 0


# op name -> check function. The op name MUST match the dispatch/proxy op string.
SESSION_STATEFUL_TOOLS = {
    "measure": _measure,
    "validate": _validate,
    "locate_gate_defects": _locate_gate_defects,
    "clearance": _clearance,
    "shape_compare": _shape_compare,
    "align_check": _align_check,
    "find_holes": _find_holes,
    "find_bosses": _find_bosses,
    "find_hole_patterns": _find_hole_patterns,
    "cross_sections": _cross_sections,
    "resolve": _resolve,
    "suggest_view_layout": _suggest_view_layout,
    "script": _script,
    "session_state": _session_state,
    "objects_types": _objects_types,
    "analyze_printability": _analyze_printability,
    "diff_snapshot": _diff_snapshot,
    "save_snapshot": _save_snapshot,
    "restore_snapshot": _restore_snapshot,
    "last_error": _last_error,
    "inspect_drawing": _inspect_drawing,
    "export_file": _export_file,
    "render_view": _render_view,
}

# Dispatch ops deliberately NOT in the smoke inventory, each with the reason it
# cannot be exercised with a plain geometry seed. A new dispatch op must be added
# to SESSION_STATEFUL_TOOLS or here (see test_every_dispatch_op_is_classified).
NON_SMOKED_OPS = {
    "execute": "the seeding mechanism itself; exercised by every WorkerSession test",
    "reset": "session-lifecycle op; smoke-testing it would clear the seeded worker state",
    "search_library": "reads the worker library index, not Session geometry",
    "load_part": "requires a named library part, not present in the geometry seed",
    "import_cad_file": "requires an external CAD file on disk",
    "view_axes": "pure: the helper takes no session (analytic axis mapping)",
    "render_drawing": "pure: rasterises an SVG file from disk; helper takes no session",
    "drafting_api": "pure: introspects the installed drafting-helpers; session arg unused",
    "health_check": "pure: builds its own Box to probe render/export; session arg unused",
    "lint_drawing": "reads drawing-annotation state, not the geometry seed",
    "save_drawing_annotations": "reads drawing-annotation state, not the geometry seed",
}


@pytest.fixture
def seeded_ws():
    """A real WorkerSession with worker-owned state: two boxes and a snapshot."""
    s = WorkerSession(exec_timeout=30)
    s.execute(
        "from build123d import *\n"
        "show(Box(1, 1, 1), 'a')\n"
        "show(Box(1, 1, 1).move(Location((0, 0, 1))), 'b')\n"
    )
    s.save_snapshot("snap")
    try:
        yield s
    finally:
        s._kill_worker()


@pytest.mark.parametrize("op", sorted(SESSION_STATEFUL_TOOLS))
def test_session_stateful_tool_sees_worker_state(seeded_ws, tmp_path, op):
    """Each listed tool, routed through the worker, reflects worker-owned state."""
    SESSION_STATEFUL_TOOLS[op](seeded_ws, tmp_path)


def test_every_dispatch_op_is_classified():
    """No dispatch op may be left without a coverage decision.

    A newly added worker-routed tool lands in neither the smoke inventory nor the
    NON_SMOKED_OPS allowlist, failing here until it is given boundary coverage or
    an explicit, documented exemption (issue #182 acceptance criterion).
    """
    classified = set(SESSION_STATEFUL_TOOLS) | set(NON_SMOKED_OPS)
    unclassified = _dispatch_ops() - classified
    assert not unclassified, (
        f"dispatch ops with no WorkerSession smoke coverage or documented "
        f"exemption: {sorted(unclassified)} — add each to SESSION_STATEFUL_TOOLS "
        f"or NON_SMOKED_OPS"
    )
    # Guard against stale inventory entries referencing a removed op.
    stale = classified - _dispatch_ops()
    assert not stale, f"classified ops with no dispatch handler: {sorted(stale)}"


def test_empty_worker_reports_missing_state():
    """Negative case: an unseeded worker reports absent state, proving the tool
    reads the worker (not a silently-empty parent that would behave identically
    whether or not routing worked)."""
    s = WorkerSession(exec_timeout=30)
    try:
        state = json.loads(s.session_state())
        assert state["objects"] == {}
        # A geometry query against the empty worker raises on the unknown name;
        # the message ("Registered: []") is the worker's own object registry.
        with pytest.raises(RuntimeError, match="Unknown object"):
            s.measure("a")
    finally:
        s._kill_worker()
