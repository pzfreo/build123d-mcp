"""The op-timeout message points at --in-process once timeouts repeat (#452).

An MCP host that stops the worker creating grandchild processes makes every op
that shells out hang until its budget expires instead of failing: render_view
and health_check always, plus the bounded geometry ops. Before this, the only
places naming the --in-process escape hatch were the worker *start* failures
(#143), so a user hitting this burned the full budget per call with no pointer
to the workaround that fixes it.

The hint stays quiet on a single timeout, because one slow boolean is
indistinguishable from a blocked spawn at that point and the degraded mode
costs crash containment and operation timeouts.
"""

import pytest

from build123d_mcp.worker import WorkerSession


@pytest.fixture
def ws():
    session = WorkerSession(exec_timeout=30)
    yield session
    session.close()


class _NeverReady:
    """A pipe stand-in whose poll() always expires, forcing the timeout path."""

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def poll(self, _timeout):
        return False

    def close(self):
        pass


def _force_timeout(session, op="render_view"):
    """Drive one op down the timeout path and return the raised message."""
    session._conn = _NeverReady()
    with pytest.raises(RuntimeError) as excinfo:
        session._do_call(op, {}, 1)
    return str(excinfo.value)


def test_first_timeout_does_not_suggest_switching_modes(ws):
    """One timeout reads exactly like slow geometry — telling someone whose
    fillet is too big to give up crash containment would be wrong."""
    message = _force_timeout(ws)
    assert "timed out" in message
    assert "in-process" not in message
    assert "BUILD123D_IN_PROCESS" not in message


def test_repeated_timeouts_name_the_escape_hatch(ws):
    """Two in a row is no longer plausibly slow geometry."""
    _force_timeout(ws)
    message = _force_timeout(ws)
    assert "--in-process" in message
    assert "BUILD123D_IN_PROCESS=1" in message
    assert "timeout 2 in a row" in message


def test_hint_names_the_cost_of_the_degraded_mode(ws):
    """The workaround is not free; the message has to say so or it reads as a
    plain recommendation."""
    _force_timeout(ws)
    message = _force_timeout(ws)
    assert "no crash containment" in message
    assert "no operation timeouts" in message


def test_hint_names_the_symptom_so_it_can_be_ruled_out(ws):
    """A user with genuinely slow geometry should be able to tell this is not
    their case."""
    _force_timeout(ws)
    message = _force_timeout(ws)
    assert "render_view" in message or "health_check" in message
    assert "spawn" in message


def test_counter_resets_after_a_successful_call(ws):
    """A slow op that times out once and then succeeds must not leave the
    session primed to blame the host on its next unrelated timeout."""
    _force_timeout(ws)
    assert ws._consecutive_timeouts == 1
    # A real round trip through the live worker resets it.
    ws.execute("x = 1")
    assert ws._consecutive_timeouts == 0
    message = _force_timeout(ws)
    assert "in-process" not in message


def test_execute_timeout_keeps_its_own_guidance(ws):
    """execute() has tailored advice (smaller steps, --exec-timeout) and must
    not be redirected to a mode switch."""
    from build123d_mcp.security import ExecutionTimeout

    ws._conn = _NeverReady()
    with pytest.raises(ExecutionTimeout) as excinfo:
        ws._do_call("execute", {"code": "x = 1"}, 1)
    message = str(excinfo.value)
    assert "smaller steps" in message
    assert "--exec-timeout" in message
    assert "--in-process" not in message
