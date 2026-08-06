"""Per-handle CAD sessions for the HTTP transport (#428).

Over stdio there is exactly one client and one namespace, and none of this
applies. Over HTTP the server historically shared a single ``WorkerSession``
across every request: safe (the worker pipe is lock-guarded, ADR 0001) but not
isolated — one client's ``reset()`` wiped another's model.

This registry maps an opaque handle to its own ``WorkerSession``. The handle
arrives as the ``Mcp-Cad-Session`` HTTP header, which is supplied from *outside*
the MCP client:

  - an auth gateway mapping authenticated identity to a stable handle, or
  - a handle pasted into a client's static server config.

It deliberately is NOT the MCP protocol session id. Protocol sessions die on
reconnect, which would silently discard a half-built model; a gateway-issued
handle is stable across reconnects and survives the transport blipping. See
docs/adr/0003-http-cad-session-handles.md.

Sessions are expensive — each one spawns a subprocess that imports the OCC
kernel (seconds of startup, hundreds of MB resident) — so the registry enforces
a hard cap and evicts on idleness. Without both, an unauthenticated peer sending
random handles would exhaust memory.

Concurrency: a session-aware load balancer routes a given handle to one process,
but nothing stops two concurrent requests carrying the SAME handle from landing
here at once. ``_lock`` therefore covers registry mutation only — never the CAD
call itself, which is serialised separately by ``WorkerSession._call``.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

# Handles are generated server-side and unguessable. Auth is the gateway's job,
# but an unguessable handle means a leaked or bypassed gateway does not hand an
# attacker a trivially enumerable namespace.
_HANDLE_BYTES = 24


class SessionLimitExceeded(RuntimeError):
    """Raised when creating a session would exceed ``max_sessions``."""


class SessionRegistry:
    """Handle -> WorkerSession, with an idle TTL and a hard cap.

    ``factory`` builds a fresh ``WorkerSession``; it is injected so tests can
    substitute a cheap stub instead of spawning real OCC subprocesses.
    """

    def __init__(
        self,
        factory: Callable[[], object],
        max_sessions: int = 8,
        idle_timeout_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._max_sessions = max_sessions
        self._idle_timeout_s = idle_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, object] = {}
        self._last_used: dict[str, float] = {}

    # --- lookup ------------------------------------------------------------ #

    def get_or_create(self, handle: str) -> object:
        """Return the session for ``handle``, creating it on first sight.

        Create-on-first-use is required, not a convenience: a handle injected by
        the gateway or configured statically in a client is always unknown to
        the server on that client's first request. There is no round trip in
        which the server could hand a handle to an MCP client — nothing in the
        protocol lets a server ask a client to adopt a custom header.
        """
        self.reap_idle()
        with self._lock:
            existing = self._sessions.get(handle)
            if existing is not None:
                self._last_used[handle] = self._clock()
                return existing
            if len(self._sessions) >= self._max_sessions:
                raise SessionLimitExceeded(
                    f"Server is at its CAD session limit ({self._max_sessions}). "
                    "Retry once an idle session is reclaimed, or raise --max-sessions."
                )
            # Reserve the slot before the expensive spawn so two concurrent
            # first-requests for different handles cannot both pass the cap
            # check and overshoot it.
            self._sessions[handle] = _PENDING
            self._last_used[handle] = self._clock()

        try:
            session = self._factory()
        except Exception:
            with self._lock:
                # Only clear our own reservation; a racing destroy may have
                # already removed it.
                if self._sessions.get(handle) is _PENDING:
                    del self._sessions[handle]
                    self._last_used.pop(handle, None)
            raise

        with self._lock:
            self._sessions[handle] = session
            self._last_used[handle] = self._clock()
        return session

    def new_handle(self) -> str:
        """Generate an unguessable handle (for operators provisioning clients)."""
        return secrets.token_urlsafe(_HANDLE_BYTES)

    # --- lifecycle --------------------------------------------------------- #

    def destroy(self, handle: str) -> bool:
        """Close and forget ``handle``. Returns whether it existed."""
        with self._lock:
            session = self._sessions.pop(handle, None)
            self._last_used.pop(handle, None)
        if session is None or session is _PENDING:
            return False
        _close(session)
        return True

    def reap_idle(self) -> list[str]:
        """Close sessions idle for longer than the TTL. Returns handles reaped.

        Called on the request path rather than from a background thread: a
        server with no traffic has nothing to reclaim for, and this keeps the
        registry free of a thread that would outlive the ASGI app.
        """
        if self._idle_timeout_s <= 0:
            return []
        cutoff = self._clock() - self._idle_timeout_s
        with self._lock:
            stale = [
                h
                for h, seen in self._last_used.items()
                if seen < cutoff and self._sessions.get(h) is not _PENDING
            ]
            reaped = [(h, self._sessions.pop(h)) for h in stale]
            for h in stale:
                self._last_used.pop(h, None)
        for _, session in reaped:
            _close(session)
        return [h for h, _ in reaped]

    def close_all(self) -> None:
        """Tear down every session (server shutdown)."""
        with self._lock:
            sessions = [s for s in self._sessions.values() if s is not _PENDING]
            self._sessions.clear()
            self._last_used.clear()
        for session in sessions:
            _close(session)

    # --- introspection ----------------------------------------------------- #

    def stats(self) -> dict:
        """Counts and idle ages, for the list_sessions operator tool.

        Handles are secrets, so they are never returned — only how many exist
        and how long each has been idle.
        """
        now = self._clock()
        with self._lock:
            ages = sorted(round(now - seen, 1) for seen in self._last_used.values())
            live = sum(1 for s in self._sessions.values() if s is not _PENDING)
        return {
            "sessions": live,
            "max_sessions": self._max_sessions,
            "idle_timeout_s": self._idle_timeout_s,
            "idle_seconds": ages,
        }

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s is not _PENDING)


class _Pending:
    """Placeholder occupying a cap slot while its worker is still spawning."""

    __slots__ = ()


_PENDING = _Pending()


def _close(session: object) -> None:
    """Best-effort teardown; a failure to reap must not break the request."""
    close = getattr(session, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        pass
