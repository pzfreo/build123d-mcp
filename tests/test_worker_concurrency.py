"""WorkerSession IPC must be concurrency-safe (issue #322).

Under HTTP transport one WorkerSession is shared across concurrent requests
(FastMCP runs sync tool closures off the event loop). The request/reply pair
over the single worker Pipe (send -> poll -> recv) is not atomic, so without a
lock two threads can interleave and one can recv() the other's response —
returning the wrong result to the wrong caller. A threading.Lock in _call()
serialises the critical section.

This test fires many concurrent read-only measure() calls against one shared
WorkerSession, each for a different named box with a distinct volume. measure()
does not mutate session state, so the only way a call can return another box's
measurement is a mispaired pipe reply. Without the lock this race is flaky;
with it, deterministic.
"""

import concurrent.futures

import pytest

from build123d_mcp.worker import WorkerSession

_N = 16


@pytest.fixture
def ws():
    s = WorkerSession(exec_timeout=30)
    # Box(i, 1, 1) has volume i, so every named object measures distinctly.
    setup = "from build123d import *\n" + "".join(
        f"show(Box({i}, 1, 1), 'b{i}')\n" for i in range(1, _N + 1)
    )
    s.execute(setup)
    try:
        yield s
    finally:
        s._kill_worker()


def test_concurrent_calls_do_not_mispair_responses(ws):
    names = [f"b{i}" for i in range(1, _N + 1)]
    reference = {name: ws.measure(name) for name in names}
    assert len(set(reference.values())) == _N, "objects must measure distinctly"

    # Each name appears several times so threads genuinely contend on the pipe.
    work = names * 4

    def call(name):
        return name, ws.measure(name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(call, work))

    for name, got in results:
        assert got == reference[name], f"response mispaired for {name}"
