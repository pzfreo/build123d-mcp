"""Tests for the live-session viewer publisher (build123d_mcp.viewer).

These exercise the server-side UDS publisher and the dependency-free glb
encoder directly, with no GUI/render dependency and no worker subprocess, so they
run anywhere the rest of the suite does. AF_UNIX is POSIX-only, so the socket tests
skip on Windows.
"""

import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile

import pytest

from build123d_mcp.viewer import ViewerPublisher, encode_frame, encode_glb

# A unit triangle: 3 verts, 1 face.
_VERTS = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 2.0, 0.0)]
_TRIS = [[0, 1, 2]]


# --------------------------------------------------------------------------- #
# glb encoder                                                                  #
# --------------------------------------------------------------------------- #


def _parse_glb(blob: bytes) -> tuple[dict, bytes]:
    """Minimal glb parser: return (gltf_json, bin_chunk). Dependency-free."""
    magic, version, total = struct.unpack_from("<III", blob, 0)
    assert magic == 0x46546C67  # "glTF"
    assert version == 2
    assert total == len(blob)
    off = 12
    json_chunk = None
    bin_chunk = b""
    while off < len(blob):
        clen, ctype = struct.unpack_from("<II", blob, off)
        off += 8
        data = blob[off : off + clen]
        off += clen
        if ctype == 0x4E4F534A:  # JSON
            json_chunk = json.loads(data.decode("utf-8"))
        elif ctype == 0x004E4942:  # BIN
            bin_chunk = data
    assert json_chunk is not None
    return json_chunk, bin_chunk


def test_encode_glb_structure_and_counts():
    blob = encode_glb(_VERTS, _TRIS)
    assert blob[:4] == b"glTF"
    gltf, binc = _parse_glb(blob)

    assert gltf["asset"]["version"] == "2.0"
    pos_acc, idx_acc = gltf["accessors"]
    assert pos_acc["type"] == "VEC3" and pos_acc["count"] == 3
    assert idx_acc["type"] == "SCALAR" and idx_acc["count"] == 3
    # POSITION min/max are required and must bound the input.
    assert pos_acc["min"] == [0.0, 0.0, 0.0]
    assert pos_acc["max"] == [1.0, 2.0, 0.0]

    # Decode the positions out of the BIN chunk and compare to the input.
    pos_view = gltf["bufferViews"][pos_acc["bufferView"]]
    floats = struct.unpack_from("<9f", binc, pos_view["byteOffset"])
    assert list(floats) == [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 2.0, 0.0]
    # And the indices.
    idx_view = gltf["bufferViews"][idx_acc["bufferView"]]
    idx = struct.unpack_from("<3I", binc, idx_view["byteOffset"])
    assert list(idx) == [0, 1, 2]


def test_encode_glb_4byte_aligned_chunks():
    blob = encode_glb(_VERTS, _TRIS)
    # glTF requires both chunk lengths to be multiples of 4.
    json_len = struct.unpack_from("<I", blob, 12)[0]
    assert json_len % 4 == 0
    bin_len = struct.unpack_from("<I", blob, 12 + 8 + json_len)[0]
    assert bin_len % 4 == 0


def test_encode_frame_layout():
    frame = encode_frame({"type": "RESET", "seq": 7}, b"")
    (jlen,) = struct.unpack_from(">I", frame, 0)
    header = json.loads(frame[4 : 4 + jlen])
    (blen,) = struct.unpack_from(">I", frame, 4 + jlen)
    assert header == {"type": "RESET", "seq": 7}
    assert blen == 0


# --------------------------------------------------------------------------- #
# UDS publisher                                                                #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sock_path():
    if sys.platform == "win32":
        pytest.skip("AF_UNIX viewer socket is POSIX-only")
    # Keep the path short: AF_UNIX sun_path is capped (~104 bytes on macOS),
    # and pytest's tmp_path under /var/folders can overflow it.
    base = "/tmp" if os.path.isdir("/tmp") else None
    d = tempfile.mkdtemp(prefix="b123dv_", dir=base)
    try:
        yield os.path.join(d, "live.sock")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _recv_exactly(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError("socket closed before frame was complete")
        buf += chunk
    return buf


def _read_frame(sock) -> tuple[dict, bytes]:
    (jlen,) = struct.unpack(">I", _recv_exactly(sock, 4))
    header = json.loads(_recv_exactly(sock, jlen).decode("utf-8"))
    (blen,) = struct.unpack(">I", _recv_exactly(sock, 4))
    payload = _recv_exactly(sock, blen) if blen else b""
    return header, payload


def _connect(path: str):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5.0)
    c.connect(path)
    return c


def test_publisher_hello_then_full_scene_dump(sock_path):
    pub = ViewerPublisher(sock_path)
    pub.start()
    try:
        pub.upsert("part", _VERTS, _TRIS)  # cached before any client connects
        client = _connect(sock_path)
        try:
            hello, _ = _read_frame(client)
            assert hello["type"] == "HELLO"
            assert hello["units"] == "mm"
            assert hello["session_id"] == pub.session_id

            up_header, up_payload = _read_frame(client)
            assert up_header["type"] == "UPSERT"
            assert up_header["name"] == "part"
            assert up_payload[:4] == b"glTF"  # the cached glb of the shape
        finally:
            client.close()
    finally:
        pub.stop()


def test_publisher_live_upsert_remove_reset(sock_path):
    pub = ViewerPublisher(sock_path)
    pub.start()
    try:
        client = _connect(sock_path)
        try:
            hello, _ = _read_frame(client)
            assert hello["type"] == "HELLO"  # empty scene → just HELLO

            pub.upsert("box", _VERTS, _TRIS)
            h, payload = _read_frame(client)
            assert h["type"] == "UPSERT" and h["name"] == "box"
            assert payload[:4] == b"glTF"

            pub.remove("box")
            h, payload = _read_frame(client)
            assert h["type"] == "REMOVE" and h["name"] == "box" and payload == b""

            pub.reset()
            h, payload = _read_frame(client)
            assert h["type"] == "RESET" and payload == b""

            # seq is monotonic across the stream.
            assert h["seq"] > hello["seq"]
        finally:
            client.close()
    finally:
        pub.stop()


def test_publisher_noop_without_clients(sock_path):
    pub = ViewerPublisher(sock_path)
    pub.start()
    try:
        assert pub.client_count == 0
        # No client attached: these must not raise and must not block.
        pub.upsert("a", _VERTS, _TRIS)
        pub.remove("a")
        pub.reset()
        assert pub.client_count == 0
    finally:
        pub.stop()


def test_publisher_stop_unlinks_socket(sock_path):
    pub = ViewerPublisher(sock_path)
    pub.start()
    assert os.path.exists(sock_path)
    pub.stop()
    assert not os.path.exists(sock_path)
    # stop() is idempotent.
    pub.stop()


def test_publisher_refuses_non_socket_path(sock_path):
    # A real file at the configured path must not be silently clobbered.
    with open(sock_path, "w") as f:
        f.write("not a socket")
    try:
        pub = ViewerPublisher(sock_path)
        with pytest.raises(ValueError, match="not a socket"):
            pub.start()
    finally:
        os.unlink(sock_path)


def test_publisher_socket_is_owner_only(sock_path):
    pub = ViewerPublisher(sock_path)
    pub.start()
    try:
        mode = stat.S_IMODE(os.stat(sock_path).st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    finally:
        pub.stop()


def test_publish_deltas_skips_per_request_session():
    """In HTTP multi-session mode a per-request session must not leak into the
    single shared viewer. _publish_deltas() short-circuits before pulling."""
    from build123d_mcp import server

    class _FakeViewer:
        client_count = 1

        def __init__(self):
            self.calls = []

        def upsert(self, *a):
            self.calls.append("upsert")

        def remove(self, *a):
            self.calls.append("remove")

    class _FakeSession:
        def __init__(self):
            self.pulled = False

        def pull_viewer_deltas(self):
            self.pulled = True
            return {"upsert": {"x": ([(0.0, 0.0, 0.0)], [[0, 0, 0]])}, "remove": []}

    fake_viewer = _FakeViewer()
    fake_session = _FakeSession()
    saved_viewer = server._viewer
    server._viewer = fake_viewer
    token = server._session_var.set(fake_session)
    try:
        server._publish_deltas()
        assert fake_session.pulled is False  # guarded before the worker pull
        assert fake_viewer.calls == []
    finally:
        server._session_var.reset(token)
        server._viewer = saved_viewer


def test_server_execute_publishes_upsert(sock_path):
    """End-to-end: a mutating tool drives a worker pull + an UPSERT on the wire.

    Exercises the real wiring: server.execute() drives worker pull_viewer_deltas,
    the server-side glb encode, and the publisher broadcast, via a live WorkerSession.
    """
    from build123d_mcp import server
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=30)
    server.configure(ws)
    server.start_viewer(sock_path)
    try:
        client = _connect(sock_path)
        try:
            hello, _ = _read_frame(client)
            assert hello["type"] == "HELLO"  # client is now registered (count > 0)

            server.execute("from build123d import *\nshow(Box(2, 2, 2), 'box')")
            header, payload = _read_frame(client)
            assert header["type"] == "UPSERT" and header["name"] == "box"
            assert payload[:4] == b"glTF"

            server.reset()
            header, _ = _read_frame(client)
            assert header["type"] == "RESET"
        finally:
            client.close()
    finally:
        server._viewer.stop()
        server._viewer = None
        ws._kill_worker()


def test_late_attach_gets_prior_geometry(sock_path):
    """A viewer attaching AFTER the model was built still gets the full scene.

    The cache must be kept current while no viewer is attached (Option A), so the
    on-connect dump reflects work done before the viewer connected.
    """
    from build123d_mcp import server
    from build123d_mcp.worker import WorkerSession

    ws = WorkerSession(exec_timeout=30)
    server.configure(ws)
    server.start_viewer(sock_path)
    try:
        # Build BEFORE any viewer connects.
        server.execute("from build123d import *\nshow(Box(3, 3, 3), 'early')")
        client = _connect(sock_path)
        try:
            hello, _ = _read_frame(client)
            assert hello["type"] == "HELLO"
            header, payload = _read_frame(client)
            assert header["type"] == "UPSERT" and header["name"] == "early"
            assert payload[:4] == b"glTF"
        finally:
            client.close()
    finally:
        server._viewer.stop()
        server._viewer = None
        ws._kill_worker()


def test_full_scene_dump_is_not_capped(sock_path, monkeypatch):
    """The on-connect HELLO + dump must be delivered intact even when the scene
    holds more shapes than the per-client backlog cap."""
    monkeypatch.setattr("build123d_mcp.viewer._MAX_QUEUED_FRAMES", 2)
    pub = ViewerPublisher(sock_path)
    pub.start()
    try:
        names = [f"s{i}" for i in range(5)]  # > cap of 2
        for name in names:
            pub.upsert(name, _VERTS, _TRIS)  # cached; no client yet
        client = _connect(sock_path)
        try:
            hello, _ = _read_frame(client)
            assert hello["type"] == "HELLO"
            got = set()
            for _ in range(len(names)):
                header, _payload = _read_frame(client)
                assert header["type"] == "UPSERT"
                got.add(header["name"])
            assert got == set(names)  # none dropped despite the tiny cap
        finally:
            client.close()
    finally:
        pub.stop()


def test_publisher_refuses_to_steal_live_socket(sock_path):
    pub1 = ViewerPublisher(sock_path)
    pub1.start()
    try:
        pub2 = ViewerPublisher(sock_path)
        with pytest.raises(RuntimeError, match="already listening"):
            pub2.start()
    finally:
        pub1.stop()


def test_example_client_safe_filename():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_ex_client",
        os.path.join(os.path.dirname(__file__), "..", "examples", "live_viewer_client.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._safe_filename("box") == "box.glb"
    assert mod._safe_filename("a/b") == "b.glb"
    assert mod._safe_filename("../../etc/passwd") == "passwd.glb"  # cannot escape
    assert mod._safe_filename("") is None
    assert mod._safe_filename("..") is None
