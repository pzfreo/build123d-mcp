"""Per-handle CAD sessions over HTTP (#428).

Uses a stub session rather than a real ``WorkerSession`` throughout: the point
under test is registry policy (isolation, cap, eviction, teardown), and spawning
real OCC subprocesses would make these tests minutes long for no extra coverage.
``test_worker_close_is_real`` covers the one place a real worker matters.
"""

import pytest

from build123d_mcp.session_registry import SessionLimitExceeded, SessionRegistry


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


def test_distinct_handles_get_distinct_sessions():
    """The whole point: two clients must not share a namespace."""
    reg = _registry(max_sessions=4)
    a = reg.get_or_create("alice")
    b = reg.get_or_create("bob")

    assert a is not b
    a.namespace["part"] = "alice's box"
    assert b.namespace == {}


def test_same_handle_is_stable_across_requests():
    """A handle is the client's identity — it must survive request boundaries,
    which is the entire reason it is not the MCP protocol session id."""
    reg = _registry(max_sessions=4)
    first = reg.get_or_create("alice")
    first.namespace["part"] = "box"

    again = reg.get_or_create("alice")
    assert again is first
    assert again.namespace["part"] == "box"


def test_unknown_handle_is_created_not_rejected():
    """Create-on-first-use is mandatory: a gateway-injected or statically
    configured handle is always unknown on that client's first request, and no
    MCP round trip exists in which a server could issue one."""
    reg = _registry(max_sessions=2)
    assert len(reg) == 0
    reg.get_or_create("never-seen-before")
    assert len(reg) == 1


def test_cap_is_enforced():
    """Each session is a subprocess with the OCC kernel loaded, so an unbounded
    registry is a memory-exhaustion DoS."""
    reg = _registry(max_sessions=2)
    reg.get_or_create("a")
    reg.get_or_create("b")

    with pytest.raises(SessionLimitExceeded):
        reg.get_or_create("c")


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
                    reg.get_or_create("second")
            return FakeSession()

    reg = _registry(max_sessions=1, factory=SlowFactory())
    reg.get_or_create("first")
    assert len(reg) == 1


def test_idle_sessions_are_reaped_and_closed():
    """Eviction must release the subprocess, not just drop the dict entry."""
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=60, clock=lambda: now[0])

    session = reg.get_or_create("alice")
    now[0] += 61
    reaped = reg.reap_idle()

    assert reaped == ["alice"]
    assert session.closed is True
    assert len(reg) == 0


def test_active_sessions_are_not_reaped():
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=60, clock=lambda: now[0])

    reg.get_or_create("alice")
    now[0] += 40
    reg.get_or_create("alice")  # refreshes last-used
    now[0] += 40
    reg.reap_idle()

    assert len(reg) == 1


def test_reaping_frees_a_slot_under_the_cap():
    now = [1000.0]
    reg = _registry(max_sessions=1, idle_timeout_s=60, clock=lambda: now[0])
    reg.get_or_create("alice")

    now[0] += 61
    reg.get_or_create("bob")  # reaps alice on the way in

    assert len(reg) == 1


def test_idle_timeout_zero_disables_reaping():
    now = [1000.0]
    reg = _registry(max_sessions=4, idle_timeout_s=0, clock=lambda: now[0])
    reg.get_or_create("alice")
    now[0] += 10_000

    assert reg.reap_idle() == []
    assert len(reg) == 1


def test_destroy_closes_and_forgets():
    reg = _registry(max_sessions=4)
    session = reg.get_or_create("alice")

    assert reg.destroy("alice") is True
    assert session.closed is True
    assert reg.destroy("alice") is False

    # A destroyed handle starts clean rather than erroring.
    fresh = reg.get_or_create("alice")
    assert fresh is not session


def test_failed_spawn_releases_its_slot():
    """A worker that fails to start must not permanently consume a slot."""

    def boom():
        raise RuntimeError("worker failed to start")

    reg = _registry(max_sessions=1, factory=boom)
    with pytest.raises(RuntimeError):
        reg.get_or_create("alice")

    assert len(reg) == 0
    reg._factory = FakeSession
    reg.get_or_create("bob")  # slot was released


def test_stats_never_leak_handles():
    """Handles are bearer-ish secrets; the operator tool reports counts only."""
    reg = _registry(max_sessions=4)
    reg.get_or_create("super-secret-handle")

    stats = reg.stats()
    assert stats["sessions"] == 1
    assert stats["max_sessions"] == 4
    assert "super-secret-handle" not in repr(stats)


def test_close_all_tears_everything_down():
    reg = _registry(max_sessions=4)
    sessions = [reg.get_or_create(h) for h in ("a", "b", "c")]

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
