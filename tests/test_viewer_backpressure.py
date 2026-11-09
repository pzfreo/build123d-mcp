"""Portable socketpair checks for retained viewer-output byte limits."""

import os
import socket
import subprocess
import sys

import pytest

from build123d_mcp import viewer


@pytest.fixture
def connection():
    pub = viewer.ViewerPublisher("unused-viewer-test.sock", max_client_bytes=64)
    sender, receiver = socket.socketpair()
    sender.setblocking(False)
    receiver.settimeout(2)
    client = viewer._Client(sender)
    pub._clients[sender] = client
    pub._sel.register(sender, viewer.selectors.EVENT_READ)
    try:
        yield pub, client, receiver
    finally:
        pub.stop()
        receiver.close()


def _receive_exactly(receiver, size):
    received = b""
    while len(received) < size:
        piece = receiver.recv(size - len(received))
        assert piece, "viewer disconnected before the complete payload arrived"
        received += piece
    return received


def test_delta_backlog_is_bounded_by_bytes(connection):
    pub, client, _ = connection
    for _ in range(10):
        pub._enqueue_locked(client, b"x" * 24)
    assert sum(map(len, client.frames)) <= 64
    assert list(client.frames) == [b"x" * 24, b"x" * 24]


def test_initial_dump_disconnects_instead_of_dropping_geometry(connection):
    pub, client, receiver = connection
    pub._enqueue_locked(client, b"hello", cap=False)
    pub._enqueue_locked(client, b"a" * 40, cap=False)
    pub._enqueue_locked(client, b"b" * 40, cap=False)
    pub._update_write_interest()
    assert pub.client_count == 0
    assert not client.frames
    assert receiver.recv(1) == b""


def test_indivisible_frame_over_budget_disconnects_client(connection):
    pub, client, receiver = connection
    pub._enqueue_locked(client, b"x" * 65)
    pub._enqueue_locked(client, b"later update")
    pub._update_write_interest()
    assert pub.client_count == 0
    assert not client.frames
    assert receiver.recv(1) == b""


def test_partial_frame_retains_its_full_byte_cost_and_wire_integrity(connection, monkeypatch):
    pub, client, receiver = connection
    head = viewer.encode_frame({"seq": 1}, b"a" * 8)
    discarded = viewer.encode_frame({"seq": 2}, b"b" * 8)
    latest = viewer.encode_frame({"seq": 3}, b"c" * 8)
    pub.max_client_bytes = len(head) + len(latest)
    sender = client.sock

    class PartialSender:
        def send(self, data):
            return sender.send(data[:3])

        def close(self):
            sender.close()

    client.sock = PartialSender()
    pub._enqueue_locked(client, head)
    assert pub._flush(client) is False
    assert client.offset == 3
    assert client.queued_bytes == len(head)  # bytes object is still retained
    pub._enqueue_locked(client, discarded)
    pub._enqueue_locked(client, latest)
    assert list(client.frames) == [head, latest]
    while client.frames:
        assert pub._flush(client) is False
    expected = head + latest
    assert _receive_exactly(receiver, len(expected)) == expected
    assert client.queued_bytes == 0
    assert client.offset == 0


def test_frame_count_cap_still_applies_to_deltas(connection, monkeypatch):
    pub, client, _ = connection
    monkeypatch.setattr(viewer, "_MAX_QUEUED_FRAMES", 2)
    for frame in (b"one", b"two", b"three"):
        pub._enqueue_locked(client, frame)
    assert list(client.frames) == [b"two", b"three"]
    assert client.queued_bytes == 8


def test_initial_dump_bypasses_frame_count_within_byte_budget(connection, monkeypatch):
    pub, client, receiver = connection
    monkeypatch.setattr(viewer, "_MAX_QUEUED_FRAMES", 2)
    for frame in (b"hello", b"first", b"second"):
        pub._enqueue_locked(client, frame, cap=False)
    assert len(client.frames) == 3
    assert pub._flush(client) is False
    assert _receive_exactly(receiver, 16) == b"hellofirstsecond"
    assert client.queued_bytes == 0


def test_overflow_does_not_disconnect_healthy_viewer(connection):
    pub, slow, _ = connection
    sender, receiver = socket.socketpair()
    receiver.settimeout(2)
    healthy = viewer._Client(sender)
    pub._clients[sender] = healthy
    pub._sel.register(sender, viewer.selectors.EVENT_READ)
    try:
        pub._enqueue_locked(slow, b"x" * 65)
        pub._enqueue_locked(healthy, b"scene")
        pub._update_write_interest()
        assert pub.client_count == 1
        assert pub._flush(healthy) is False
        assert _receive_exactly(receiver, 5) == b"scene"
    finally:
        receiver.close()


def test_cumulative_slow_viewer_drops_oldest_without_disconnect(connection, capsys):
    pub, client, _ = connection
    for i in range(100):  # 100 * 24 bytes, far past the 64-byte budget
        pub._enqueue_locked(client, bytes([i]) * 24)
    pub._update_write_interest()
    assert pub.client_count == 1  # a slow viewer keeps the newest updates
    assert list(client.frames) == [bytes([98]) * 24, bytes([99]) * 24]
    assert client.queued_bytes == 48
    assert capsys.readouterr().err == ""  # dropping deltas is not an error


@pytest.mark.parametrize(
    ("frames", "cap", "reason"),
    [
        ([b"hello", b"a" * 40, b"b" * 40], False, "the initial scene (at least 85 bytes) exceeds"),
        ([b"x" * 65], True, "a single 65-byte frame exceeds"),
    ],
)
def test_overflow_disconnect_logs_reason_to_stderr_only(connection, capsys, frames, cap, reason):
    pub, client, receiver = connection
    for frame in frames:
        pub._enqueue_locked(client, frame, cap=cap)
    assert capsys.readouterr().err == ""  # the broadcaster path never logs or closes
    pub._update_write_interest()
    pub._update_write_interest()  # already dropped: logged exactly once
    assert receiver.recv(1) == b""
    out, err = capsys.readouterr()
    assert out == ""  # stdout carries the MCP stdio protocol
    assert err.count("WARNING: live viewer disconnected") == 1
    assert f"{reason} the 64-byte per-viewer output limit" in err
    assert "--viewer-max-client-bytes" in err and "BUILD123D_VIEWER_MAX_CLIENT_BYTES" in err


def test_default_budget_admits_scenes_far_past_the_old_16_mib(connection):
    pub, client, _ = connection
    pub.max_client_bytes = viewer.DEFAULT_MAX_CLIENT_BYTES
    assert viewer.DEFAULT_MAX_CLIENT_BYTES == 256 * 1024 * 1024
    mesh = b"m" * (20 * 1024 * 1024)  # one 20 MiB mesh, over the previous 16 MiB cap
    pub._enqueue_locked(client, b"hello", cap=False)
    for _ in range(3):
        pub._enqueue_locked(client, mesh, cap=False)
    pub._update_write_interest()
    assert pub.client_count == 1
    assert client.overflow_reason is None
    assert client.queued_bytes == 5 + 3 * len(mesh)


@pytest.mark.parametrize("value", [0, -1])
def test_publisher_rejects_non_positive_budget(value):
    with pytest.raises(ValueError, match="max_client_bytes must be positive"):
        viewer.ViewerPublisher("unused-viewer-test.sock", max_client_bytes=value)


@pytest.mark.parametrize("value", [True, 1.5, "1024"])
def test_publisher_rejects_non_integer_budget(value):
    with pytest.raises(TypeError, match="max_client_bytes must be an int"):
        viewer.ViewerPublisher("unused-viewer-test.sock", max_client_bytes=value)


def _cli_run(args, env_value=None):
    env = {k: v for k, v in os.environ.items() if k != "BUILD123D_VIEWER_MAX_CLIENT_BYTES"}
    if env_value is not None:
        env["BUILD123D_VIEWER_MAX_CLIENT_BYTES"] = env_value
    return subprocess.run(
        [sys.executable, "-c", "from build123d_mcp.cli import main; main()", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("args", "env_value", "message"),
    [
        (["--viewer-max-client-bytes", "0"], None, "must be a positive integer, got 0"),
        (["--viewer-max-client-bytes", "-5"], None, "must be a positive integer, got -5"),
        (["--viewer-max-client-bytes", "16MiB"], None, "invalid int value: '16MiB'"),
        ([], "abc", "invalid int value: 'abc'"),
        ([], "0", "must be a positive integer, got 0"),
    ],
)
def test_cli_rejects_invalid_viewer_byte_budget(args, env_value, message):
    result = _cli_run(args, env_value)
    assert result.returncode == 2
    assert result.stdout == ""  # usage errors never reach the MCP stdout stream
    assert "--viewer-max-client-bytes" in result.stderr
    assert message in result.stderr


@pytest.mark.parametrize(
    ("argv", "env_value", "expected"),
    [
        ([], None, viewer.DEFAULT_MAX_CLIENT_BYTES),
        ([], "1048576", 1048576),
        (["--viewer-max-client-bytes", "2097152"], "1048576", 2097152),  # flag beats env
    ],
)
def test_cli_passes_viewer_byte_budget_to_publisher(
    monkeypatch, tmp_path, argv, env_value, expected
):
    from build123d_mcp import cli, server, worker

    if env_value is None:
        monkeypatch.delenv("BUILD123D_VIEWER_MAX_CLIENT_BYTES", raising=False)
    else:
        monkeypatch.setenv("BUILD123D_VIEWER_MAX_CLIENT_BYTES", env_value)
    sock = str(tmp_path / "live.sock")
    monkeypatch.setattr(
        sys, "argv", ["build123d-mcp", "--in-process", "--viewer-socket", sock, *argv]
    )
    # start_viewer is stubbed, so no AF_UNIX socket is bound; this lets the
    # argument-to-publisher wiring run on every platform.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(worker, "InProcessSession", lambda **kwargs: object())
    monkeypatch.setattr(server, "configure", lambda session: None)
    monkeypatch.setattr(server, "apply_tool_visibility", lambda *a, **k: None)
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(server, "start_viewer", lambda path, **kwargs: calls.append((path, kwargs)))
    cli.main()
    assert calls == [(sock, {"max_client_bytes": expected})]


def test_start_viewer_forwards_budget(monkeypatch):
    from build123d_mcp import server

    created = []

    class FakePublisher:
        def __init__(self, path, **kwargs):
            created.append((path, kwargs))

        def start(self):
            pass

        def reset(self):
            pass

    monkeypatch.setattr(viewer, "ViewerPublisher", FakePublisher)
    monkeypatch.setattr(server, "_viewer", None)
    server.start_viewer("a.sock", max_client_bytes=4096)
    server.start_viewer("b.sock")
    assert created == [("a.sock", {"max_client_bytes": 4096}), ("b.sock", {})]
