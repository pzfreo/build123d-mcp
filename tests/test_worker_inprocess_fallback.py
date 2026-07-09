"""Automatic in-process fallback when the worker subprocess can't (re)start (#143).

On a host that blocks or breaks multiprocessing spawn the worker never signals
ready. Rather than erroring every tool call ("Worker was not running;
restarted") for the life of the server, WorkerSession degrades to running the
Session in-process. These tests force the failure by patching _start_worker so
they exercise the fallback without needing a genuinely broken host.
"""

import json

import pytest

from build123d_mcp.worker import WorkerSession


def _boom(self):
    raise RuntimeError("the worker did not signal ready within 60s (simulated #143)")


def test_startup_failure_degrades_and_still_serves(monkeypatch):
    """Worker fails at construction → session runs in-process instead of crashing."""
    monkeypatch.setattr(WorkerSession, "_start_worker", _boom)
    ws = WorkerSession(exec_timeout=60)

    assert ws._in_process is True
    out = ws.execute("from build123d import *\nshow(Box(10, 10, 10), 'b')")
    assert "Error" not in out
    data = json.loads(ws.measure("b"))
    assert data["volume"] == pytest.approx(1000, rel=0.01)
    assert data["topology"]["faces"] == 6


def test_degraded_error_contract_matches_worker(monkeypatch):
    """Tool exceptions surface as RuntimeError('TypeName: message'), like the worker."""
    monkeypatch.setattr(WorkerSession, "_start_worker", _boom)
    ws = WorkerSession(exec_timeout=60)

    with pytest.raises(RuntimeError, match=r"ValueError: Unknown object 'nope'"):
        ws.measure("nope")


def test_degraded_reset_dispatches_in_process(monkeypatch):
    """reset() must not touch the (absent) worker process once degraded."""
    monkeypatch.setattr(WorkerSession, "_start_worker", _boom)
    ws = WorkerSession(exec_timeout=60)

    ws.execute("from build123d import *\nshow(Box(1, 1, 1), 'a')")
    ws.reset()
    state = json.loads(ws.session_state())
    assert state["objects"] == {}


def test_opt_out_env_var_fails_hard(monkeypatch):
    """BUILD123D_NO_WORKER_FALLBACK=1 restores the strict "must have a worker" contract."""
    monkeypatch.setenv("BUILD123D_NO_WORKER_FALLBACK", "1")
    monkeypatch.setattr(WorkerSession, "_start_worker", _boom)

    with pytest.raises(RuntimeError, match="simulated #143"):
        WorkerSession(exec_timeout=60)


def test_restart_failure_degrades_mid_session(monkeypatch):
    """A worker that starts, then can't be restarted (dies + spawn now broken),
    degrades to in-process on the next op instead of wedging every call."""
    ws = WorkerSession(exec_timeout=60)
    try:
        ws.execute("from build123d import *")
        ws._kill_worker()  # worker is now dead; next op must restart it

        # From here, every (re)start fails — the Copilot-CLI-on-Windows case where
        # the initial worker booted but restart workers hang at bootstrap.
        monkeypatch.setattr(WorkerSession, "_start_worker", _boom)

        out = ws.execute("show(Box(3, 3, 3), 'c')")
        assert "Error" not in out
        assert ws._in_process is True
        assert json.loads(ws.measure("c"))["volume"] == pytest.approx(27, rel=0.01)
    finally:
        ws._kill_worker()


def test_in_process_replay_is_budget_bounded(monkeypatch):
    """A degraded session rebuilds only what fits the wall-clock budget and keeps the
    prefix, so a session with hundreds of execute() calls can't stall the degrade."""
    import time

    monkeypatch.setattr(WorkerSession, "_start_worker", _boom)
    ws = WorkerSession(exec_timeout=60)  # degrades at construction; in-process Session built
    ws.execute("from build123d import *")

    # Fake clock: deadline is read once (start), then the per-step check jumps past it
    # on the second step, so only the first step replays.
    ticks = iter([1000.0, 1000.0, 9999.0, 9999.0, 9999.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))

    restored = ws._replay_execute_history_in_process(["a = 1", "b = 2", "c = 3"])
    assert restored == 1
