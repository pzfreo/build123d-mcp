"""Portable socketpair checks for retained viewer-output byte limits."""

import socket

import pytest

from build123d_mcp import viewer


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setattr(viewer, "_MAX_QUEUED_BYTES", 64, raising=False)
    pub = viewer.ViewerPublisher("unused-viewer-test.sock")
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
    monkeypatch.setattr(viewer, "_MAX_QUEUED_BYTES", len(head) + len(latest))
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
