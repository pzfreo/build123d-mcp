"""Parent-side execute-history replay rebuilds session state after a worker
restart (#359), instead of returning a wiped session.

The worker's own Session (namespace, named objects, snapshots) dies with the
child on a timeout/crash SIGKILL. WorkerSession keeps a parent-side log of every
completed execute() call and replays it into the fresh worker, so a slow op that
kills the worker costs only that one op, not the whole session.
"""

from build123d_mcp.worker import WorkerSession


def test_replay_rebuilds_namespace_and_objects_after_worker_kill():
    ws = WorkerSession()
    try:
        ws.execute("from build123d import *")
        ws.execute("plate_w = 42.0\nshow(Box(plate_w, 20, 5), 'plate')\n")
        assert len(ws._execute_history) == 2

        ws._kill_worker()  # simulate the timeout/crash SIGKILL
        restored, total = ws._restart_and_replay()

        assert restored == 2 and total == 2  # both steps replayed
        assert "42.0" in ws.execute("print(plate_w)")  # variable rebuilt
        assert "error" not in ws.measure("plate").lower()  # named object rebuilt
    finally:
        ws._kill_worker()


def test_reset_clears_replay_history():
    ws = WorkerSession()
    try:
        ws.execute("x = 1")
        assert ws._execute_history
        ws.reset()
        assert ws._execute_history == []
    finally:
        ws._kill_worker()


def test_timed_out_op_is_not_in_replay_history():
    # Only completed calls are logged, so the op that dies is excluded and replay
    # can't re-hit it. A call that raises inside _call must not be appended.
    ws = WorkerSession()
    try:
        ws.execute("keep = 7")

        def _boom(*a, **k):
            raise RuntimeError("simulated timeout")

        ws._call = _boom
        ws.execute("this_would_time_out = 1")  # swallowed to an error string
        assert ws._execute_history == ["keep = 7"]  # the failed op was NOT logged
    finally:
        ws._kill_worker()
