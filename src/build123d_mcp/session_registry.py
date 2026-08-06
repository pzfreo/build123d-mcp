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

Concurrency
-----------
A session-aware load balancer routes a handle to one process, but nothing stops
two concurrent requests carrying the SAME handle from landing here together, nor
an unrelated request triggering eviction while another is mid-CAD-call. Two
mechanisms handle that.

**Leases.** ``acquire()`` returns a ``Lease``; the holder must ``release()`` it.
A session with a live lease is never closed. Eviction and explicit destruction
*detach* it from the lookup table at once — so no new request can find it — and
defer the actual ``close()`` to the last holder. Without this, reaping could kill
a worker mid-call, and a CAD call longer than the TTL would be evicted out from
under itself, since the idle stamp is taken at request *start*.

**Creation barrier.** The first caller for a handle installs an entry and spawns
the worker outside the lock (spawning takes real time; holding the lock would
stall every other handle). Concurrent callers for the same handle wait on that
entry's event instead of racing — and are never handed the half-built entry.

``_lock`` covers registry bookkeeping only, never the CAD call itself, which is
serialised separately by ``WorkerSession._call``.
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

# Longest handle accepted. The ones we mint are ~32 chars; this bounds what a
# peer can turn into a retained dictionary key.
MAX_HANDLE_LENGTH = 256

# How long a same-handle caller waits for another request's worker to finish
# spawning before giving up with SessionUnavailable.
#
# This bounds WAITERS only — it cannot bound the creator, which is a plain
# blocking call in its own request's thread and not cancellable. The factory
# must therefore be self-bounding, and WorkerSession is: _start_worker() polls
# for the ready signal with _WORKER_READY_TIMEOUT and raises rather than hanging.
# A factory that could block forever would strand its reservation, holding a cap
# slot for the life of the process.
_CREATE_WAIT_S = 120.0


class SessionLimitExceeded(RuntimeError):
    """Raised when creating a session would exceed ``max_sessions``."""


class SessionUnavailable(RuntimeError):
    """A session could not be produced for a transient reason — retry.

    Distinct from ``SessionLimitExceeded``, which means the server is full.
    Raised when a creation is abandoned (registry shut down, handle destroyed
    mid-spawn), fails, or wedges past the wait.
    """


class _Entry:
    """One handle's session plus the bookkeeping that keeps it alive safely."""

    __slots__ = ("session", "leases", "last_used", "detached", "ready", "failed")

    def __init__(self, now: float) -> None:
        self.session: object | None = None  # None until the worker finishes spawning
        self.leases = 0
        self.last_used = now
        self.detached = False  # off the lookup table; close when leases hit zero
        self.failed = False  # creation raised; waiters must fail rather than hang
        self.ready = threading.Event()


class Lease:
    """A borrowed session. Release exactly once, in a ``finally``."""

    __slots__ = ("session", "_registry", "_entry", "_released")

    def __init__(self, registry: SessionRegistry, entry: _Entry, session: object) -> None:
        self.session = session
        self._registry = registry
        self._entry = entry
        self._released = False

    def release(self) -> None:
        # The released flag is checked and set inside the registry lock, not
        # here: two threads releasing the SAME lease could otherwise both pass a
        # plain check, double-decrement the count, and close a session another
        # holder is still using.
        self._registry._release(self._entry, self)


class SessionRegistry:
    """Handle -> WorkerSession, with leases, an idle TTL and a hard cap.

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
        self._entries: dict[str, _Entry] = {}
        # Detached entries whose worker is still alive (a lease outlives the
        # destroy/eviction) or still spawning. They hold real subprocesses, so
        # they count against the cap until actually closed — otherwise
        # destroying a leased session would let a replacement in alongside it
        # and the "hard cap" would not bound live workers at all.
        self._closing: set[_Entry] = set()
        self._shutdown = False

    # --- lease-based access ------------------------------------------------- #

    def acquire(self, handle: str) -> Lease:
        """Return a lease on ``handle``'s session, creating it on first sight.

        Create-on-first-use is required, not a convenience: a handle injected by
        a gateway or configured statically in a client is always unknown to the
        server on that client's first request, and no MCP round trip exists in
        which a server could hand a handle to a client.
        """
        self.reap_idle()
        entry, is_creator = self._reserve(handle)

        if not is_creator:
            return self._await_creation(entry)

        # BaseException, not Exception: a factory killed by SystemExit or
        # KeyboardInterrupt would otherwise never set `ready`, leaving waiters to
        # burn the full timeout and the reservation to hold a slot forever.
        try:
            session = self._factory()
        except BaseException:
            with self._lock:
                entry.failed = True
                entry.leases -= 1
                if self._entries.get(handle) is entry:
                    del self._entries[handle]
                self._closing.discard(entry)
            entry.ready.set()  # wake waiters so they fail fast rather than hang
            raise

        orphaned = False
        with self._lock:
            entry.session = session
            entry.last_used = self._clock()
            # destroy() or close_all() may have detached this entry while the
            # worker was spawning. Publishing it now would resurrect a session
            # nothing tracks and leak its subprocess past shutdown.
            if entry.detached or self._entries.get(handle) is not entry:
                entry.session = None
                entry.failed = True
                entry.leases -= 1
                orphaned = True
        entry.ready.set()

        if orphaned:
            # Via _finish_close, not a bare _close: the slot must stay held
            # until this worker is really gone, exactly as on the other paths.
            self._finish_close(entry, session)
            raise SessionUnavailable(
                "This session was destroyed while its worker was starting. Retry."
            )
        return Lease(self, entry, session)

    def _await_creation(self, entry: _Entry) -> Lease:
        """Wait for the request that is spawning this handle's worker."""
        if not entry.ready.wait(_CREATE_WAIT_S):
            self._release(entry)
            raise SessionUnavailable(
                "Timed out waiting for this session's worker to start. Retry shortly."
            )
        if entry.failed or entry.session is None:
            self._release(entry)
            raise SessionUnavailable(
                "This session's worker failed to start. Retry to attempt a fresh one."
            )
        return Lease(self, entry, entry.session)

    def _reserve(self, handle: str) -> tuple[_Entry, bool]:
        """Take a lease, creating the entry if absent.

        Returns ``(entry, is_creator)``; the creator runs the factory and sets
        ``ready``.
        """
        with self._lock:
            if self._shutdown:
                raise SessionUnavailable("Server is shutting down.")
            entry = self._entries.get(handle)
            if entry is not None:
                entry.leases += 1
                entry.last_used = self._clock()
                return entry, False
            if len(self._entries) + len(self._closing) >= self._max_sessions:
                raise SessionLimitExceeded(
                    f"Server is at its CAD session limit ({self._max_sessions}). "
                    "Retry once an idle session is reclaimed, or raise --max-sessions."
                )
            entry = _Entry(self._clock())
            entry.leases = 1
            self._entries[handle] = entry
            return entry, True

    def _release(self, entry: _Entry, lease: Lease | None = None) -> None:
        """Give back a lease; close the session if it was the last one on an
        already-detached entry."""
        with self._lock:
            if lease is not None:
                if lease._released:
                    return
                lease._released = True
            entry.leases -= 1
            entry.last_used = self._clock()
            expired = entry.detached and entry.leases <= 0 and entry.session is not None
            session = entry.session if expired else None
            if expired:
                entry.session = None
        if session is not None:
            self._finish_close(entry, session)

    def _finish_close(self, entry: _Entry, session: object) -> None:
        """Close ``session``, freeing its cap slot only once it is really gone.

        The slot is released *after* ``close()`` returns, not before: a slot
        freed at detach time lets a replacement worker spawn while the
        predecessor's subprocess is still being killed, so live workers briefly
        exceed the cap the whole mechanism exists to enforce.

        If the close fails outright the slot is deliberately kept. A worker we
        could not kill is still holding memory, and this cap is a memory bound —
        under-admitting is the safe direction to err.
        """
        ok = _close(session)
        if ok:
            with self._lock:
                self._closing.discard(entry)

    def new_handle(self) -> str:
        """Generate an unguessable handle (for operators provisioning clients)."""
        return secrets.token_urlsafe(_HANDLE_BYTES)

    # --- lifecycle ---------------------------------------------------------- #

    def destroy(self, handle: str) -> bool:
        """Detach ``handle``, closing it once no request is still using it.

        Returns whether a session existed. Deferring matters because the usual
        caller is the ``destroy_session`` tool, running inside a request that
        holds a lease on that very session.
        """
        return self._detach(handle)

    def reap_idle(self) -> list[str]:
        """Close sessions idle beyond the TTL. Returns the handles reaped.

        Runs on the request path rather than in a background thread: a server
        with no traffic has nothing to reclaim for, and this keeps the registry
        free of a thread that would outlive the ASGI app.

        A leased session is never reaped, however old its stamp — the stamp is
        taken at request start, so a CAD call longer than the TTL would
        otherwise be evicted mid-flight.
        """
        if self._idle_timeout_s <= 0:
            return []
        cutoff = self._clock() - self._idle_timeout_s
        with self._lock:
            stale = [
                h
                for h, e in self._entries.items()
                if e.last_used < cutoff and e.leases <= 0 and e.session is not None
            ]
        return [h for h in stale if self._detach(h)]

    def _detach(self, handle: str) -> bool:
        """Remove ``handle`` from lookup; close now if unleased, else on release."""
        with self._lock:
            entry = self._entries.pop(handle, None)
            if entry is None:
                return False
            entry.detached = True
            if entry.leases > 0 or entry.session is None:
                # Still in use, or still spawning — release() (or the orphan
                # check) closes it. Its worker is alive either way, so keep it
                # counted against the cap until then.
                self._closing.add(entry)
                return True
            session, entry.session = entry.session, None
            # Hold the slot across the close, so a replacement cannot spawn
            # while this worker is still being killed.
            self._closing.add(entry)
        self._finish_close(entry, session)
        return True

    def close_all(self) -> None:
        """Tear down every session (ASGI lifespan shutdown).

        Sets the shutdown flag first, so a creation still in flight discovers it
        has been orphaned and closes its own worker instead of installing it
        after teardown.
        """
        with self._lock:
            self._shutdown = True
            closing = []
            for entry in self._entries.values():
                entry.detached = True
                self._closing.add(entry)  # slot held until the close completes
                if entry.session is not None and entry.leases <= 0:
                    closing.append((entry, entry.session))
                    entry.session = None
                # Leased or still spawning: its holder closes it on release.
            self._entries.clear()
        for entry, session in closing:
            self._finish_close(entry, session)

    # --- introspection ------------------------------------------------------ #

    def stats(self) -> dict:
        """Counts and idle ages for the list_sessions operator tool.

        Handles are secrets and are never returned — only counts and idle ages.

        ``capacity_used`` is what the cap actually compares against, and it can
        exceed ``sessions``: a session still spawning, or one detached and
        waiting on its last lease, or one whose close failed, all still hold a
        slot. Reporting only ``sessions`` could show 0 while every request is
        being refused with "at session limit", which is exactly the situation an
        operator would be reading this to diagnose.
        """
        now = self._clock()
        with self._lock:
            live = [e for e in self._entries.values() if e.session is not None]
            ages = sorted(round(now - e.last_used, 1) for e in live)
            in_use = sum(1 for e in live if e.leases > 0)
            starting = sum(1 for e in self._entries.values() if e.session is None)
            closing = len(self._closing)
            capacity_used = len(self._entries) + closing
        return {
            "sessions": len(ages),
            "in_use": in_use,
            "starting": starting,
            "closing": closing,
            "capacity_used": capacity_used,
            "max_sessions": self._max_sessions,
            "idle_timeout_s": self._idle_timeout_s,
            "idle_seconds": ages,
        }

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.session is not None)


def _close(session: object) -> bool:
    """Tear ``session`` down. Returns whether it is really gone.

    Failures are swallowed rather than raised — reaping someone else's session
    must not break the request that happened to trigger it — but they are
    reported, because the caller keeps the cap slot for a worker it could not
    kill.
    """
    close = getattr(session, "close", None)
    if close is None:
        return True
    try:
        close()
    except Exception:
        return False
    return True
