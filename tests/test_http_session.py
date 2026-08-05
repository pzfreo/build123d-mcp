"""HTTP/ASGI transport and per-request session acceptance coverage (#268).

#268 added MCP HTTP hosting hooks (shipped in v0.3.50). Two of its
acceptance criteria had no regression test:

  1. Concurrent requests with different session keys resolve to different
     WorkerSessions, with no cross-talk (the per-request ContextVar resolver).
  2. ``http_app()`` returns an ASGI app that mounts in an external ASGI
     application and is wired as a streamable-HTTP handler.

These tests pin both at the mechanism level — the ContextVar routing and the
ASGI app construction — without spawning real worker subprocesses or a live
HTTP server.
"""

import contextvars
import threading

import pytest

from build123d_mcp import server


def _set_singleton(value):
    """Set the module-level session singleton, returning the previous state so
    a test can restore it (other tests rely on a configured ``_session``)."""
    had = hasattr(server, "_session")
    prev = getattr(server, "_session", None)
    server.configure(value)
    return had, prev


def _restore_singleton(state):
    had, prev = state
    if had:
        server._session = prev
    elif hasattr(server, "_session"):
        del server._session


def test_resolve_session_falls_back_to_singleton():
    """With no per-request var set (stdio mode), the singleton is used."""
    sentinel = object()
    state = _set_singleton(sentinel)
    try:
        assert server._session_var.get() is None
        assert server._resolve_session() is sentinel
    finally:
        _restore_singleton(state)


def test_resolve_session_prefers_per_request_var():
    """A per-request session set on the ContextVar wins over the singleton, and
    resetting the var restores singleton resolution."""
    singleton = object()
    per_request = object()
    state = _set_singleton(singleton)
    token = server._session_var.set(per_request)
    try:
        assert server._resolve_session() is per_request
    finally:
        server._session_var.reset(token)
    try:
        assert server._resolve_session() is singleton
    finally:
        _restore_singleton(state)


def test_concurrent_requests_isolated_no_crosstalk():
    """Acceptance criterion 1: two concurrent 'requests' (each in its own
    context, as an ASGI task) set distinct sessions and each resolves only its
    own — no leakage between them or onto the shared singleton."""
    singleton = object()
    state = _set_singleton(singleton)
    sessions = {"a": object(), "b": object()}
    seen: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    # The barrier forces both requests to set their var BEFORE either reads.
    # That ordering is what gives this test teeth: a plain module global would
    # be overwritten by the second setter, so both reads would return the same
    # value and the assertions below would fail. A ContextVar keeps them apart.
    both_set = threading.Barrier(2)

    def handle(key: str) -> None:
        def run() -> None:
            try:
                server._session_var.set(sessions[key])
                both_set.wait(timeout=10)
                seen[key] = server._resolve_session()
            except BaseException as exc:  # surface thread failures, incl. barrier timeout
                errors[key] = exc

        # copy_context() models the ASGI server running each request in its own
        # context; the var set inside never escapes to the base context.
        contextvars.copy_context().run(run)

    try:
        threads = [threading.Thread(target=handle, args=(k,)) for k in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), "request thread hung"
        assert not errors, f"request thread(s) errored: {errors}"
        assert seen["a"] is sessions["a"]
        assert seen["b"] is sessions["b"]
        # The per-request vars never touched the base context's resolution.
        assert server._session_var.get() is None
        assert server._resolve_session() is singleton
    finally:
        _restore_singleton(state)


def test_http_app_is_mountable_asgi():
    """Acceptance criterion 2: http_app() returns an ASGI app that mounts in an
    external ASGI application under a sub-path."""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    app = server.http_app()
    # A real ASGI app, not just any callable — Mount would accept anything.
    assert isinstance(app, Starlette)

    parent = Starlette(routes=[Mount("/mcp", app=app)])
    mount = next((r for r in parent.routes if getattr(r, "path", None) == "/mcp"), None)
    assert mount is not None
    assert mount.app is app  # the MCPServer app is what got mounted


def test_http_app_is_stateless_http():
    """http_app() relies on stateless HTTP (session identity comes from the
    embedder's headers, not MCP session IDs) — pin that the server is built
    that way so the ASGI hook stays embeddable.

    SDK v2 moved ``stateless_http`` off the server settings onto the transport
    factory. Asserting the real session manager the app actually builds, rather
    than that ``http_app()`` passed a keyword, keeps this a state assertion: a
    future SDK that renamed or ignored the flag would fail here."""
    server.http_app()

    manager = server.mcp._lowlevel_server.session_manager
    assert manager.stateless is True


def test_serves_both_protocol_eras():
    """SDK v2 serves the 2026-07-28 protocol alongside the 2025 era, so the
    migration widened the surface clients can reach — `server/discover` and the
    stateless per-request lifecycle did not exist before it.

    The conformance job cannot gate that era (the suite has no server scenarios
    for it yet), so pin it here: both eras negotiable, and discover answering
    with OUR capabilities and instructions rather than an SDK default."""
    import asyncio
    from types import SimpleNamespace

    from mcp.types.version import SUPPORTED_PROTOCOL_VERSIONS

    assert "2026-07-28" in SUPPORTED_PROTOCOL_VERSIONS
    assert "2025-06-18" in SUPPORTED_PROTOCOL_VERSIONS

    lowlevel = server.mcp._lowlevel_server
    ctx = SimpleNamespace(protocol_version="2026-07-28")
    result = asyncio.run(lowlevel._handle_discover(ctx, None))

    assert result.supported_versions == ["2026-07-28"]
    assert result.instructions == server._INSTRUCTIONS
    assert result.capabilities.tools is not None


def test_server_info_reports_our_own_version():
    """serverInfo.version is what a user quotes in a bug report, so it must be
    this package's version. SDK v1 defaulted it to the SDK's own version and v2
    defaults it to "", so it only holds while we pass it explicitly."""
    from importlib.metadata import version

    assert server.mcp._lowlevel_server.version == version("build123d-mcp")


def test_compare_missing_support_error_does_not_mask_internal_attribute_errors():
    """Only a missing compare method should produce the compatibility error."""
    state = _set_singleton(object())
    try:
        assert "does not support compare" in server.compare(a="part")
    finally:
        _restore_singleton(state)

    class BrokenCompare:
        def compare(self, *args, **kwargs):
            raise AttributeError("internal compare failure")

    state = _set_singleton(BrokenCompare())
    try:
        with pytest.raises(AttributeError, match="internal compare failure"):
            server.compare(a="part")
    finally:
        _restore_singleton(state)
