"""Per-handle CAD sessions over HTTP (#428).

Uses a stub session rather than a real ``WorkerSession`` throughout: the point
under test is registry policy (isolation, cap, eviction, teardown), and spawning
real OCC subprocesses would make these tests minutes long for no extra coverage.
``test_worker_close_is_real`` covers the one place a real worker matters.
"""

import threading

import pytest

from build123d_mcp.session_registry import (
    SessionLimitExceeded,
    SessionRegistry,
    SessionUnavailable,
)


class FakeSession:
    """Stands in for a WorkerSession; records that it was closed."""

    def __init__(self):
        self.closed = False
        self.namespace = {}

    def close(self):
        self.closed = True


def _registry(**kw):
    kw.setdefault("factory", FakeSession)
    return SessionRegistry(**kw)


def acquire(registry, handle):
    """Acquire and immediately release, returning the session.

    Most tests care about which session a handle maps to, not lease lifetime.
    The lease-sensitive cases hold their leases explicitly.
    """
    lease = registry.acquire(handle)
    try:
        return lease.session
    finally:
        lease.release()


def test_distinct_handles_get_distinct_sessions():
    """The whole point: two clients must not share a namespace."""
    reg = _registry(max_sessions=4)
    a = acquire(reg, "alice")
    b = acquire(reg, "bob")

    assert a is not b
    a.namespace["part"] = "alice's box"
    assert b.namespace == {}


def test_same_handle_is_stable_across_requests():
    """A handle is the client's identity — it must survive request boundaries,
    which is the entire reason it is not the MCP protocol session id."""
    reg = _registry(max_sessions=4)
    first = acquire(reg, "alice")
    first.namespace["part"] = "box"

    again = acquire(reg, "alice")
    assert again is first
    assert again.namespace["part"] == "box"


def test_unknown_handle_is_created_not_rejected():
    """Create-on-first-use is mandatory: a gateway-injected or statically
    configured handle is always unknown on that client's first request, and no
    MCP round trip exists in which a server could issue one."""
    reg = _registry(max_sessions=2)
    assert len(reg) == 0
    acquire(reg, "never-seen-before")
    assert len(reg) == 1


def test_cap_is_enforced():
    """Each session is a subprocess with the OCC kernel loaded, so an unbounded
    registry is a memory-exhaustion DoS."""
    reg = _registry(max_sessions=2)
    acquire(reg, "a")
    acquire(reg, "b")

    with pytest.raises(SessionLimitExceeded):
        acquire(reg, "c")


def test_cap_counts_reservations_so_concurrent_creates_cannot_overshoot():
    """The slot is reserved before the (slow) spawn, so two first-requests
    arriving together cannot both pass the cap check."""
    started = []

    class SlowFactory:
        def __call__(self):
            # Re-entering the registry mid-spawn simulates a concurrent request
            # landing while this one is still building its worker.
            started.append(1)
            if len(started) == 1:
                with pytest.raises(SessionLimitExceeded):
                    acquire(reg, "second")
            return FakeSession()

    reg = _registry(max_sessions=1, factory=SlowFactory())
    acquire(reg, "first")
    assert len(reg) == 1


def test_idle_sessions_are_reaped_and_closed():
    """Eviction must release the subprocess, not just drop the dict entry."""
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=60, clock=lambda: now[0])

    session = acquire(reg, "alice")
    now[0] += 61
    reaped = reg.reap_idle()

    assert reaped == ["alice"]
    assert session.closed is True
    assert len(reg) == 0


def test_active_sessions_are_not_reaped():
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=60, clock=lambda: now[0])

    acquire(reg, "alice")
    now[0] += 40
    acquire(reg, "alice")  # refreshes last-used
    now[0] += 40
    reg.reap_idle()

    assert len(reg) == 1


def test_reaping_frees_a_slot_under_the_cap():
    now = [1000.0]
    reg = _registry(max_sessions=1, idle_timeout_s=60, clock=lambda: now[0])
    acquire(reg, "alice")

    now[0] += 61
    acquire(reg, "bob")  # reaps alice on the way in

    assert len(reg) == 1


def test_idle_timeout_zero_disables_reaping():
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=0, clock=lambda: now[0])
    acquire(reg, "alice")
    now[0] += 10_000

    assert reg.reap_idle() == []
    assert len(reg) == 1


def test_destroy_closes_and_forgets():
    reg = _registry(max_sessions=4)
    session = acquire(reg, "alice")

    assert reg.destroy("alice") is True
    assert session.closed is True
    assert reg.destroy("alice") is False

    # A destroyed handle starts clean rather than erroring.
    fresh = acquire(reg, "alice")
    assert fresh is not session


def test_failed_spawn_releases_its_slot():
    """A worker that fails to start must not permanently consume a slot."""

    def boom():
        raise RuntimeError("worker failed to start")

    reg = _registry(max_sessions=1, factory=boom)
    with pytest.raises(RuntimeError):
        acquire(reg, "alice")

    assert len(reg) == 0
    reg._factory = FakeSession
    acquire(reg, "bob")  # slot was released


def test_stats_never_leak_handles():
    """Handles are bearer-ish secrets; the operator tool reports counts only."""
    reg = _registry(max_sessions=4)
    acquire(reg, "super-secret-handle")

    stats = reg.stats()
    assert stats["sessions"] == 1
    assert stats["max_sessions"] == 4
    assert "super-secret-handle" not in repr(stats)


def test_close_all_tears_everything_down():
    reg = _registry(max_sessions=4)
    sessions = [acquire(reg, h) for h in ("a", "b", "c")]

    reg.close_all()

    assert len(reg) == 0
    assert all(s.closed for s in sessions)


def test_new_handle_is_unguessable():
    reg = _registry()
    handles = {reg.new_handle() for _ in range(50)}

    assert len(handles) == 50
    assert all(len(h) >= 32 for h in handles)


def test_worker_close_is_real():
    """The stubs above assert registry policy; this asserts the teardown they
    stand in for actually happens — the subprocess dies, and a later call raises
    instead of silently restarting it.

    That last part is the subtle one: ``_do_call`` restarts a dead worker by
    design (crash recovery), so without the closed-flag guard an evicted session
    would resurrect itself on the next request and leak the process back.
    """
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=30)
    proc = ws._proc
    assert proc.is_alive()

    ws.close()

    proc.join(10)
    assert not proc.is_alive()

    # A plain op surfaces the closure as an exception...
    with pytest.raises(RuntimeError, match="closed"):
        ws.measure()

    # ...while execute() reports it as an agent-readable error string, because it
    # deliberately converts RuntimeError rather than raising at the tool boundary.
    assert "closed" in ws.execute("x = 1")

    ws.close()  # idempotent


# --- Regressions from the adversarial review --------------------------------- #


class BlockingFactory:
    """Factory that parks inside the spawn so races can be driven deterministically."""

    def __init__(self):
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.built = []

    def __call__(self):
        self.entered.set()
        self.proceed.wait(5)
        session = FakeSession()
        self.built.append(session)
        return session


def test_concurrent_same_handle_waits_instead_of_getting_the_placeholder():
    """A second request for a handle whose worker is still spawning must get the
    real session. It previously received the internal reservation sentinel, which
    reached tool code as a bogus session object."""
    factory = BlockingFactory()
    reg = _registry(max_sessions=2, factory=factory)
    got = {}

    creator = threading.Thread(target=lambda: got.__setitem__("a", reg.acquire("shared")))
    creator.start()
    factory.entered.wait(5)

    waiter = threading.Thread(target=lambda: got.__setitem__("b", reg.acquire("shared")))
    waiter.start()

    factory.proceed.set()
    creator.join(5)
    waiter.join(5)

    assert isinstance(got["a"].session, FakeSession)
    assert got["b"].session is got["a"].session
    assert len(factory.built) == 1  # one worker, not two
    got["a"].release()
    got["b"].release()


def test_destroy_during_creation_does_not_resurrect_the_session():
    """destroy() while a worker is spawning must not leave that worker installed
    afterwards — it would be tracked by nothing and outlive shutdown."""
    factory = BlockingFactory()
    reg = _registry(max_sessions=2, factory=factory)
    outcome = {}

    def create():
        try:
            outcome["lease"] = reg.acquire("doomed")
        except SessionUnavailable as exc:
            outcome["error"] = exc

    t = threading.Thread(target=create)
    t.start()
    factory.entered.wait(5)
    reg.destroy("doomed")
    factory.proceed.set()
    t.join(5)

    assert "error" in outcome
    assert len(reg) == 0
    assert factory.built[0].closed is True  # the orphaned worker was reaped


def test_close_all_during_creation_does_not_leave_a_worker_behind():
    factory = BlockingFactory()
    reg = _registry(max_sessions=2, factory=factory)
    outcome = {}

    def create():
        try:
            outcome["lease"] = reg.acquire("late")
        except SessionUnavailable as exc:
            outcome["error"] = exc

    t = threading.Thread(target=create)
    t.start()
    factory.entered.wait(5)
    reg.close_all()
    factory.proceed.set()
    t.join(5)

    assert "error" in outcome
    assert factory.built[0].closed is True


def test_leased_session_is_never_reaped_mid_request():
    """The idle stamp is taken at request start, so a CAD call longer than the
    TTL would otherwise be evicted out from under itself — killing the worker
    while it is executing."""
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=60, clock=lambda: now[0])

    lease = reg.acquire("alice")  # request in flight
    now[0] += 600  # its CAD call runs far longer than the TTL

    assert reg.reap_idle() == []
    assert lease.session.closed is False

    lease.release()
    now[0] += 61
    assert reg.reap_idle() == ["alice"]
    assert lease.session.closed is True


def test_destroy_while_leased_defers_the_close_until_release():
    """destroy_session() runs inside a request holding a lease on the very
    session it destroys, so the close has to wait for that request to finish."""
    reg = _registry(max_sessions=4)
    lease = reg.acquire("alice")

    assert reg.destroy("alice") is True
    assert lease.session.closed is False  # still in use
    assert len(reg) == 0  # but no longer findable

    lease.release()
    assert lease.session.closed is True


def test_destroyed_handle_starts_fresh_for_the_next_request():
    reg = _registry(max_sessions=4)
    lease = reg.acquire("alice")
    reg.destroy("alice")
    lease.release()

    replacement = reg.acquire("alice")
    assert replacement.session is not lease.session
    replacement.release()


def test_close_all_defers_leased_sessions_to_their_holders():
    reg = _registry(max_sessions=4)
    lease = reg.acquire("alice")

    reg.close_all()
    assert lease.session.closed is False

    lease.release()
    assert lease.session.closed is True


def test_acquire_after_shutdown_is_refused():
    reg = _registry(max_sessions=4)
    reg.close_all()

    with pytest.raises(SessionUnavailable):
        reg.acquire("alice")


def test_release_is_idempotent():
    reg = _registry(max_sessions=1)
    lease = reg.acquire("alice")
    lease.release()
    lease.release()  # a double release must not free someone else's slot

    other = reg.acquire("alice")
    assert other.session is lease.session
    other.release()


def test_stats_counts_agree_and_report_in_use():
    reg = _registry(max_sessions=4)
    held = reg.acquire("alice")
    acquire(reg, "bob")

    stats = reg.stats()
    assert stats["sessions"] == 2
    assert stats["in_use"] == 1
    assert len(stats["idle_seconds"]) == stats["sessions"]
    held.release()


def test_reset_on_a_closed_session_does_not_start_a_new_worker():
    """reset() bypasses _call's guard via its dead-worker shortcut, so without
    its own check it would spawn a replacement for a closed session — one that
    every later call refuses to use, leaking until the process exits."""
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=30)
    ws.close()
    dead = ws._proc

    with pytest.raises(RuntimeError, match="closed"):
        ws.reset()

    assert ws._proc is dead  # no replacement was spawned
    assert not ws._proc.is_alive()


def test_concurrent_release_of_one_lease_cannot_close_another_holders_session():
    """Two threads releasing the SAME lease must decrement once. Otherwise the
    count reaches zero while a second holder is still using the session, which
    closes a worker mid-request and leaves the count negative."""
    reg = _registry(max_sessions=2)
    first = reg.acquire("alice")
    second = reg.acquire("alice")
    reg.destroy("alice")  # detached; must survive until BOTH leases are back

    start = threading.Barrier(2)

    def release_first():
        start.wait(5)
        first.release()

    threads = [threading.Thread(target=release_first) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert first.session.closed is False  # second still holds it
    second.release()
    assert first.session.closed is True


def test_cap_counts_detached_sessions_whose_workers_are_still_alive():
    """destroy() defers the close while a lease is held, so that worker is still
    resident. Admitting a replacement alongside it would let live subprocesses
    exceed --max-sessions, which is the memory bound the cap exists to enforce."""
    reg = _registry(max_sessions=1)
    lease = reg.acquire("alice")
    reg.destroy("alice")  # alice's worker is alive until lease.release()

    with pytest.raises(SessionLimitExceeded):
        reg.acquire("bob")

    lease.release()  # alice really goes away...
    assert lease.session.closed is True
    bob = reg.acquire("bob")  # ...and only now does the slot free up
    bob.release()


def test_cap_slot_is_held_until_the_close_actually_completes():
    """Freeing the slot at detach time lets a replacement spawn while the
    predecessor's subprocess is still being killed, so live workers briefly
    exceed the cap the mechanism exists to enforce."""
    observed = []

    class SlowClosingSession:
        closed = False

        def close(self):
            # Try to take the slot from inside the close, i.e. exactly the
            # window where a concurrent request would.
            try:
                reg.acquire("replacement")
                observed.append("admitted-during-close")
            except SessionLimitExceeded:
                observed.append("refused-during-close")
            self.closed = True

    reg = _registry(max_sessions=1, factory=SlowClosingSession)
    lease = reg.acquire("alice")
    lease.release()
    reg.destroy("alice")

    assert observed == ["refused-during-close"]
    later = reg.acquire("replacement")  # freed once the close returned
    later.release()


def test_a_worker_that_cannot_be_killed_keeps_its_slot():
    """The cap is a memory bound, so a session whose close() failed still counts:
    that subprocess is presumed alive and holding memory."""

    class UnkillableSession:
        def close(self):
            raise OSError("worker will not die")

    reg = _registry(max_sessions=1, factory=UnkillableSession)
    lease = reg.acquire("stuck")
    lease.release()
    reg.destroy("stuck")

    with pytest.raises(SessionLimitExceeded):
        reg.acquire("someone-else")


def test_orphaned_creation_also_holds_its_slot_until_the_close_completes():
    """The orphan path freed the slot before closing, reintroducing the very
    race _finish_close exists to prevent — and permanently freeing a slot whose
    worker survived, if that close failed."""
    factory = BlockingFactory()
    reg = _registry(max_sessions=1, factory=factory)
    outcome = {}

    def create():
        try:
            reg.acquire("doomed")
        except SessionUnavailable as exc:
            outcome["error"] = exc

    t = threading.Thread(target=create)
    t.start()
    factory.entered.wait(5)
    reg.destroy("doomed")

    with pytest.raises(SessionLimitExceeded):
        reg.acquire("opportunist")  # slot still held while the orphan closes

    factory.proceed.set()
    t.join(5)

    assert "error" in outcome
    assert factory.built[0].closed is True
    later = reg.acquire("opportunist")  # freed only now
    later.release()


def test_stats_reports_capacity_not_just_live_sessions():
    """An operator reading this is usually diagnosing 503s, so the figure the
    cap actually compares against has to be visible — otherwise it can read
    sessions: 0 while every request is refused."""
    reg = _registry(max_sessions=1)
    lease = reg.acquire("alice")
    reg.destroy("alice")  # detached, worker alive until release

    stats = reg.stats()
    assert stats["sessions"] == 0  # nothing in the lookup table...
    assert stats["closing"] == 1  # ...but a worker is still resident
    assert stats["capacity_used"] == 1
    assert stats["capacity_used"] == stats["max_sessions"]  # hence the 503s
    lease.release()

    assert reg.stats()["capacity_used"] == 0


def test_close_reports_a_worker_that_refuses_to_die():
    """The registry frees a capacity slot on the strength of close() returning,
    so close() must not report success for a live process."""
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=30)
    real_proc = ws._proc

    class Undead:
        pid = 4242

        def is_alive(self):
            return True

        def kill(self):
            pass

        def join(self, timeout=None):
            pass

    ws._proc = Undead()
    with pytest.raises(RuntimeError, match="still alive"):
        ws.close()

    ws._proc = real_proc  # let the genuine worker be reaped
    ws._closed = False
    ws.close()
