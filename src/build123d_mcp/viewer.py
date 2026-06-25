"""Live-session viewer publisher: stream mesh updates over a Unix domain socket.

When the server is started with ``--viewer-socket PATH`` (or
``BUILD123D_VIEWER_SOCKET``), it binds a UDS and broadcasts the session's
geometry to any connected viewer client as it changes, so a human can watch and
rotate the model while an agent drives the MCP. The agent keeps getting its
fixed-viewpoint ``render_view`` PNGs; the human gets a live 3D view.

Design (see docs/live-viewer.md):

- This publisher runs in the SERVER process, on a background daemon thread. It
  does not stall the agent path on viewer I/O: a broadcast only appends to
  bounded per-client buffers (drop-oldest for a slow client), and the loop
  thread holds the shared lock for at most one non-blocking send at a time.
- The geometry lives in the worker subprocess; the server asks the worker for
  per-shape mesh deltas (``pull_viewer_deltas``) after each mutating tool and
  encodes them to glTF-binary (glb) here, with no OCC/VTK dependency and no new
  third-party dependency (a small, self-contained glTF 2.0 writer below).
- A viewer connecting mid-session receives ``HELLO`` followed by an ``UPSERT``
  for every shape currently in the scene (served from a server-side glb cache),
  so a late attach is correct.

Access control: the socket is restricted to mode 0600 (owner only) right after
bind, so only the server's user can connect. The publisher also lives in the
server process, outside the sandboxed ``execute()`` worker, so under the default
sandbox user code cannot reach it (the import allowlist blocks
``socket``/filesystem modules in the worker's exec namespace). Starting the
server with ``--no-sandbox`` or ``--allow-all-imports`` lifts that allowlist, so
worker code could then connect like any local process; the 0600 permission stays
as the backstop.

Wire protocol, length-prefixed frames::

    [u32 BE json_len][json header utf8][u32 BE bin_len][binary payload]

Header: ``{type, session_id, seq, name?, units:"mm"}``. Types: ``HELLO`` (no
payload), ``UPSERT`` (payload = a binary glb of one named shape), ``REMOVE``
(no payload), ``RESET`` (no payload; clear the scene). ``seq`` is monotonic per
publisher.
"""

from __future__ import annotations

import array
import atexit
import json
import os
import selectors
import socket
import stat
import struct
import sys
import threading
import uuid
from collections import deque
from collections.abc import Iterable, Sequence

# --------------------------------------------------------------------------- #
# glTF 2.0 binary (glb) encoder: zero dependencies (stdlib struct/array/json)  #
# --------------------------------------------------------------------------- #

_GLB_MAGIC = 0x46546C67  # "glTF"
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A  # "JSON"
_CHUNK_BIN = 0x004E4942  # "BIN\0"
_COMPONENT_FLOAT = 5126  # FLOAT
_COMPONENT_UINT = 5125  # UNSIGNED_INT
_TARGET_ARRAY_BUFFER = 34962
_TARGET_ELEMENT_ARRAY_BUFFER = 34963
_MODE_TRIANGLES = 4


def encode_glb(verts: Iterable[Sequence[float]], tris: Iterable[Sequence[int]]) -> bytes:
    """Encode a triangle mesh as a binary glTF (glb).

    ``verts`` is an iterable of ``(x, y, z)`` world-space points (mm); ``tris``
    is an iterable of ``(i, j, k)`` vertex-index triples, exactly the shape of
    ``build123d``'s ``Shape.tessellate`` output as marshalled by the render
    tessellation path. The result is a standard glb readable by three.js'
    ``GLTFLoader``, trimesh, pyvista, etc.
    """
    positions = array.array("f")
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    n_verts = 0
    for v in verts:
        x, y, z = float(v[0]), float(v[1]), float(v[2])
        positions.append(x)
        positions.append(y)
        positions.append(z)
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)
        min_z, max_z = min(min_z, z), max(max_z, z)
        n_verts += 1

    indices = array.array("I")
    for t in tris:
        indices.append(int(t[0]))
        indices.append(int(t[1]))
        indices.append(int(t[2]))
    n_indices = len(indices)

    if n_verts == 0:  # degenerate guard: POSITION min/max must be finite
        min_x = min_y = min_z = max_x = max_y = max_z = 0.0

    # glTF buffers are little-endian; array.tobytes() is native-endian.
    if sys.byteorder == "big":
        positions.byteswap()
        indices.byteswap()
    pos_bytes = positions.tobytes()
    idx_bytes = indices.tobytes()
    # POSITION (4-byte float) sits at offset 0; indices (4-byte uint) follow at
    # len(pos_bytes), which is a multiple of 4, so both accessor alignments hold.
    bin_blob = pos_bytes + idx_bytes

    gltf = {
        "asset": {"version": "2.0", "generator": "build123d-mcp"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": _MODE_TRIANGLES}]}
        ],
        "buffers": [{"byteLength": len(bin_blob)}],
        "bufferViews": [
            {
                "buffer": 0,
                "byteOffset": 0,
                "byteLength": len(pos_bytes),
                "target": _TARGET_ARRAY_BUFFER,
            },
            {
                "buffer": 0,
                "byteOffset": len(pos_bytes),
                "byteLength": len(idx_bytes),
                "target": _TARGET_ELEMENT_ARRAY_BUFFER,
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": _COMPONENT_FLOAT,
                "count": n_verts,
                "type": "VEC3",
                "min": [min_x, min_y, min_z],
                "max": [max_x, max_y, max_z],
            },
            {
                "bufferView": 1,
                "componentType": _COMPONENT_UINT,
                "count": n_indices,
                "type": "SCALAR",
            },
        ],
    }

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)  # pad with spaces
    bin_blob += b"\x00" * ((4 - len(bin_blob) % 4) % 4)  # pad with zeros

    total_len = 12 + 8 + len(json_bytes) + 8 + len(bin_blob)
    out = bytearray()
    out += struct.pack("<III", _GLB_MAGIC, _GLB_VERSION, total_len)
    out += struct.pack("<II", len(json_bytes), _CHUNK_JSON)
    out += json_bytes
    out += struct.pack("<II", len(bin_blob), _CHUNK_BIN)
    out += bin_blob
    return bytes(out)


# --------------------------------------------------------------------------- #
# Wire framing                                                                 #
# --------------------------------------------------------------------------- #

_UNITS = "mm"


def encode_frame(header: dict, payload: bytes = b"") -> bytes:
    """Length-prefixed frame: ``[u32 json_len][json][u32 bin_len][payload]``."""
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return (
        struct.pack(">I", len(header_bytes))
        + header_bytes
        + struct.pack(">I", len(payload))
        + payload
    )


# Per-client backlog cap (whole frames). A client slower than the producer has
# its OLDEST queued frames dropped; the producer never blocks. A reconnecting
# viewer always gets a fresh full-scene dump, so dropping stale deltas is safe.
# The cap is on frame count, not bytes: a scene of very large meshes can hold a
# few hundred MB on a stalled client before frames start dropping.
_MAX_QUEUED_FRAMES = 256


class _Client:
    __slots__ = ("sock", "frames", "offset")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.frames: deque[bytes] = deque()  # complete frames pending send
        self.offset = 0  # bytes already sent from frames[0]


class ViewerPublisher:
    """Server-side UDS publisher broadcasting mesh deltas to viewer clients.

    Thread model: a single background daemon thread owns the listening socket,
    all client sockets, and the ``selectors`` loop. The public mutators
    (:meth:`upsert`, :meth:`remove`, :meth:`reset`) are called from the server's
    request thread; they update the glb cache + per-client queues under a lock
    and wake the loop. All socket I/O happens on the loop thread.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.session_id = uuid.uuid4().hex
        self._sel = selectors.DefaultSelector()
        self._lsock: socket.socket | None = None
        self._clients: dict[socket.socket, _Client] = {}
        self._cache: dict[str, bytes] = {}  # name -> glb bytes (insertion-ordered)
        self._seq = 0
        self._lock = threading.Lock()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._thread: threading.Thread | None = None
        self._closed = False

    # ---- lifecycle ------------------------------------------------------- #

    @staticmethod
    def _socket_has_listener(path: str) -> bool:
        """True if a server is currently accepting connections at ``path``."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
            return True
        except OSError:
            return False  # ConnectionRefusedError (stale socket) or other failure
        finally:
            probe.close()

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            # Refuse to clobber a real file, and refuse to steal a socket another
            # server is actively listening on; only reclaim a stale socket whose
            # listener is gone.
            if not stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
                raise ValueError(
                    f"viewer socket path exists and is not a socket: {self.socket_path}"
                )
            if self._socket_has_listener(self.socket_path):
                raise RuntimeError(f"another server is already listening on {self.socket_path}")
            os.unlink(self.socket_path)
        lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Create the socket owner-only with no window: set a private umask across
        # bind() so the path is never world-accessible (a chmod after bind would
        # leave a brief gap). The chmod then pins the exact mode regardless of the
        # umask inherited by the process.
        old_umask = os.umask(0o077)
        try:
            lsock.bind(self.socket_path)
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o600)
        lsock.listen(8)
        lsock.setblocking(False)
        self._lsock = lsock
        self._sel.register(lsock, selectors.EVENT_READ)
        self._sel.register(self._wake_r, selectors.EVENT_READ)
        self._thread = threading.Thread(target=self._loop, name="viewer-publisher", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for c in clients:
            try:
                c.sock.close()
            except OSError:
                pass
        if self._lsock is not None:
            try:
                self._lsock.close()
            except OSError:
                pass
        # The loop thread has joined, so the selector and wake socketpair are no
        # longer in use; close them too to avoid fd leaks across start/stop cycles.
        try:
            self._sel.close()
        except Exception:  # noqa: BLE001 - selector close is best-effort on teardown
            pass
        for wake_sock in (self._wake_r, self._wake_w):
            try:
                wake_sock.close()
            except OSError:
                pass
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass

    # ---- public mutators (called from the server request thread) --------- #

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def upsert(
        self, name: str, verts: Iterable[Sequence[float]], tris: Iterable[Sequence[int]]
    ) -> None:
        """Encode ``name``'s mesh to glb, cache it, and broadcast an UPSERT."""
        glb = encode_glb(verts, tris)
        with self._lock:
            self._cache[name] = glb
            frame = self._frame_locked("UPSERT", name=name, payload=glb)
            self._broadcast_locked(frame)
        self._wake()

    def remove(self, name: str) -> None:
        with self._lock:
            self._cache.pop(name, None)
            frame = self._frame_locked("REMOVE", name=name)
            self._broadcast_locked(frame)
        self._wake()

    def reset(self) -> None:
        """Clear the cached scene and tell every client to drop its scene."""
        with self._lock:
            self._cache.clear()
            frame = self._frame_locked("RESET")
            self._broadcast_locked(frame)
        self._wake()

    # ---- frame construction (caller holds the lock) ---------------------- #

    def _frame_locked(self, type_: str, name: str | None = None, payload: bytes = b"") -> bytes:
        self._seq += 1
        header: dict = {
            "type": type_,
            "session_id": self.session_id,
            "seq": self._seq,
            "units": _UNITS,
        }
        if name is not None:
            header["name"] = name
        return encode_frame(header, payload)

    def _broadcast_locked(self, frame: bytes) -> None:
        for client in self._clients.values():
            self._enqueue_locked(client, frame)

    def _enqueue_locked(self, client: _Client, frame: bytes, cap: bool = True) -> None:
        client.frames.append(frame)
        if not cap:
            return  # the on-connect HELLO + full-scene dump must never be dropped
        # Drop oldest whole frames past the cap, never the in-flight head.
        while len(client.frames) > _MAX_QUEUED_FRAMES:
            if client.offset > 0 and len(client.frames) > 1:
                del client.frames[1]
            else:
                client.frames.popleft()

    # ---- background loop (the only thread touching sockets) -------------- #

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\x01")
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._closed:
            events = self._sel.select(timeout=1.0)
            for key, mask in events:
                fileobj = key.fileobj
                try:
                    if fileobj is self._lsock:
                        self._accept()
                    elif fileobj is self._wake_r:
                        self._drain_wake()
                    else:
                        self._service_client(fileobj, mask)
                except Exception:  # noqa: BLE001 - one bad client must not kill the loop
                    self._drop(fileobj)
            self._update_write_interest()

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except OSError:
            pass

    def _accept(self) -> None:
        assert self._lsock is not None
        try:
            conn, _ = self._lsock.accept()
        except OSError:
            return
        conn.setblocking(False)
        client = _Client(conn)
        with self._lock:
            self._clients[conn] = client
            # HELLO + a full-scene dump from the cache so a mid-session attach sees
            # the existing model. These bypass the drop-oldest cap (cap=False): the
            # dump is the client's required starting state, so it is never shed even
            # for a scene larger than _MAX_QUEUED_FRAMES. Live deltas after it are
            # still capped.
            self._enqueue_locked(client, self._frame_locked("HELLO"), cap=False)
            for name, glb in self._cache.items():
                self._enqueue_locked(
                    client, self._frame_locked("UPSERT", name=name, payload=glb), cap=False
                )
        self._sel.register(conn, selectors.EVENT_READ)

    def _service_client(self, fileobj, mask: int) -> None:
        with self._lock:
            client = self._clients.get(fileobj)
        if client is None:
            return
        if mask & selectors.EVENT_READ:
            try:
                data = client.sock.recv(4096)
            except BlockingIOError:
                data = None  # spurious readiness; not a disconnect
            except OSError:
                self._drop(fileobj)
                return
            if data == b"":  # peer closed
                self._drop(fileobj)
                return
            # Inbound bytes are ignored in v1 (read only to detect disconnect).
        if mask & selectors.EVENT_WRITE and self._flush(client):
            self._drop(fileobj)

    def _flush(self, client: _Client) -> bool:
        """Send as much queued data as the socket accepts. Return True if dead.

        The lock is taken per frame, not across the whole drain, so a fast viewer
        emptying a large backlog never holds it long enough to stall the agent
        thread's broadcast: each iteration is one non-blocking send.
        """
        while True:
            with self._lock:
                if not client.frames:
                    return False
                head = client.frames[0]
                try:
                    # memoryview slice is zero-copy, avoiding a re-copy of the
                    # whole remaining frame on each partial send of a large glb.
                    sent = client.sock.send(memoryview(head)[client.offset :])
                except BlockingIOError:
                    return False  # kernel buffer full; resume on next writable event
                except OSError:
                    return True
                client.offset += sent
                if client.offset >= len(head):
                    client.frames.popleft()
                    client.offset = 0
                else:
                    return False  # partial frame sent; resume on next writable event

    def _update_write_interest(self) -> None:
        with self._lock:
            pending = {fo: bool(c.frames) for fo, c in self._clients.items()}
        for fileobj, has_pending in pending.items():
            want = selectors.EVENT_READ | (selectors.EVENT_WRITE if has_pending else 0)
            try:
                if self._sel.get_key(fileobj).events != want:
                    self._sel.modify(fileobj, want)
            except KeyError:
                pass  # client dropped concurrently

    def _drop(self, fileobj) -> None:
        try:
            self._sel.unregister(fileobj)
        except (KeyError, ValueError):
            pass
        with self._lock:
            client = self._clients.pop(fileobj, None)
        if client is not None:
            try:
                client.sock.close()
            except OSError:
                pass
