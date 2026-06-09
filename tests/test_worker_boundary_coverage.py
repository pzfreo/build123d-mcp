"""Production-boundary regression coverage for session-stateful MCP tools (issue #182).

In production ``server.py`` holds a :class:`WorkerSession` proxy whose real
``Session`` state (objects, current_shape, namespace, geometry_refs, snapshots,
annotations, execute_history) lives in a worker subprocess. A tool that reads
that state must dispatch into the worker; one that calls its helper against the
empty parent proxy silently sees no state (the bug class fixed in #179).

Helper-level tests against an in-process ``Session`` cannot catch that: they pass
because the helper and the state share a process. This module pins the worker
boundary instead:

1. ``test_dispatch_ops_match_proxy_methods`` — every worker dispatch op has a
   matching ``WorkerSession`` proxy method and vice-versa (1:1, AST-derived).
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

import ast
import inspect
import json

import pytest

from build123d_mcp import worker
from build123d_mcp.worker import WorkerSession

# --------------------------------------------------------------------------- #
# AST extraction of the dispatch / proxy op sets (single source of truth)      #
# --------------------------------------------------------------------------- #


def _dispatch_ops() -> set[str]:
    """Op strings compared in ``worker._dispatch`` (``if op == "...":``)."""
    tree = ast.parse(inspect.getsource(worker._dispatch))
    ops: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "op"
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        ):
            ops.add(node.comparators[0].value)
    return ops


def _proxy_ops() -> set[str]:
    """Op strings each ``WorkerSession`` method passes to ``self._call(op, ...)``."""
    tree = ast.parse(inspect.getsource(WorkerSession))
    ops: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_call"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            ops.add(node.args[0].value)
    return ops


def test_dispatch_ops_match_proxy_methods():
    """Every worker dispatch op is reachable through a proxy method and vice-versa.

    Adding a ``_dispatch`` branch without a ``WorkerSession`` method (the tool is
    unreachable from server.py), or a proxy method whose op string has no handler
    (every call raises ``Unknown operation``), breaks this 1:1 mapping.
    """
    dispatch = _dispatch_ops()
    proxy = _proxy_ops()
    assert dispatch == proxy, (
        f"dispatch-only ops (no proxy method): {sorted(dispatch - proxy)}; "
        f"proxy-only ops (no dispatch handler): {sorted(proxy - dispatch)}"
    )


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


def _clearance(ws, tmp_path):
    r = json.loads(ws.clearance("a", "b"))
    assert "error" not in r and "status" in r


def _interference(ws, tmp_path):
    r = json.loads(ws.interference("a", "b"))
    assert "error" not in r and "interferes" in r


def _shape_compare(ws, tmp_path):
    r = json.loads(ws.shape_compare("a", "b"))
    assert "error" not in r and "delta" in r


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
    "clearance": _clearance,
    "interference": _interference,
    "shape_compare": _shape_compare,
    "align_check": _align_check,
    "cross_sections": _cross_sections,
    "resolve": _resolve,
    "suggest_view_layout": _suggest_view_layout,
    "script": _script,
    "session_state": _session_state,
    "objects_types": _objects_types,
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
