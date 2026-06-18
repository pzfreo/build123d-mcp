"""HTTP/ASGI transport and per-request session acceptance coverage (#268).

#268 added FastMCP HTTP hosting hooks (shipped in v0.3.50). Two of its
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
    assert mount.app is app  # the FastMCP app is what got mounted


def test_fastmcp_is_stateless_http():
    """http_app() relies on stateless HTTP (session identity comes from the
    embedder's headers, not MCP session IDs) — pin that the server is built
    that way so the ASGI hook stays embeddable."""
    assert server.mcp.settings.stateless_http is True
