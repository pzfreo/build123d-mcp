"""Test the build123d://session MCP resource wiring.

The resource dispatches through the production session proxy, so it is
exercised against a real WorkerSession — the object server.py uses in
production — rather than an in-process Session (issue #179).
"""

import json

import pytest

from build123d_mcp.worker import WorkerSession


@pytest.fixture
def patched_server(monkeypatch):
    """Return the server module with _session set to a real WorkerSession."""
    import build123d_mcp.server as srv

    s = WorkerSession(exec_timeout=30)
    monkeypatch.setattr(srv, "_session", s, raising=False)
    try:
        yield srv, s
    finally:
        s._kill_worker()


def test_session_resource_empty(patched_server):
    srv, _ = patched_server
    data = json.loads(srv.build123d_session_state())
    assert data["current_shape"] is None
    assert data["objects"] == {}
    assert data["snapshots"] == []


def test_session_resource_reflects_live_state(patched_server):
    srv, s = patched_server
    s.execute("from build123d import *\nresult = Box(10, 10, 10)")
    s.execute("show(Cylinder(3, 15), 'pin')")
    s.save_snapshot("v1")
    data = json.loads(srv.build123d_session_state())
    assert data["current_shape"]["volume"] == pytest.approx(1000, rel=0.01)
    assert "pin" in data["objects"]
    assert "v1" in data["snapshots"]
