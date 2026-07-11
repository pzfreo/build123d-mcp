"""Every MCP tool declares annotations (readOnlyHint/destructiveHint/idempotentHint)
so clients can auto-approve read-only queries and not gate the verify loop (#368)."""

from build123d_mcp import server


def _annotations():
    # The default (non-experimental) tool set; register_experimental_tools() is not
    # called, so this doesn't mutate the shared singleton for other tests.
    return {t.name: t.annotations for t in server.mcp._tool_manager.list_tools()}


def test_every_tool_declares_annotations():
    missing = [n for n, a in _annotations().items() if a is None]
    assert missing == [], f"tools missing annotations: {missing}"


def test_read_only_query_tools_are_marked_read_only():
    a = _annotations()
    for n in (
        "measure",
        "validate",
        "locate_gate_defects",
        "compare",
        "cross_sections",
        "inspect_part",
        "find_holes",
        "find_hole_patterns",
        "find_bosses",
        "find_bored_bosses",
        "find_countersinks",
        "session_state",
        "design_audit",
        "analyze_printability",
        "render_view",
        "render_drawing",
        "script",
        "inspect_drawing",
        "lint_drawing",
        "view_axes",
        "suggest_view_layout",
        "search_library",
        "last_error",
        "repair_hints",
        "repair_advice",
        "version",
        "workflow_hints",
        "health_check",
    ):
        assert a[n].readOnlyHint is True, n


def test_mutating_tools_are_not_read_only():
    a = _annotations()
    for n in (
        "execute",
        "load_part",
        "import_cad_file",
        "install_skill",
        "save_drawing_annotations",
    ):
        assert a[n].readOnlyHint is False, n


def test_idempotent_mutations_are_flagged():
    a = _annotations()
    # resolve(label=) writes session.geometry_refs — mutating, but idempotent (overwrite).
    for n in ("export", "save_snapshot", "restore_snapshot", "resolve"):
        assert a[n].readOnlyHint is False and a[n].idempotentHint is True, n


def test_reset_is_destructive():
    a = _annotations()["reset"]
    assert a.readOnlyHint is False and a.destructiveHint is True and a.idempotentHint is True


def test_no_tool_is_both_read_only_and_destructive():
    # A read-only tool that also claims to be destructive is a classification bug.
    bad = [n for n, a in _annotations().items() if a.readOnlyHint and a.destructiveHint]
    assert bad == [], bad


def test_experimental_tools_are_read_only_when_enabled():
    before = set(_annotations())
    server.register_experimental_tools()
    try:
        a = _annotations()
        assert a["verify_spec"].readOnlyHint is True
        assert a["suggest_spec"].readOnlyHint is True
    finally:
        for name in ("verify_spec", "suggest_spec"):
            try:
                server.mcp.remove_tool(name)
            except Exception:  # noqa: BLE001 - tolerate a not-registered tool
                pass
    assert set(_annotations()) == before  # singleton restored for other tests
