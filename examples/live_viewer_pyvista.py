#!/usr/bin/env python3
"""Interactive pyvista window for the build123d-mcp live session socket.

Connects to the Unix domain socket published by ``build123d-mcp --viewer-socket
PATH`` and shows the session geometry in a rotatable 3D window that updates as the
model changes. This is the reference graphical consumer for the protocol in
docs/live-viewer.md; ``examples/live_viewer_client.py`` is its dependency-free,
text-only sibling.

It needs pyvista and trimesh, which are not build123d-mcp dependencies, so run it
ad hoc with uv (``trimesh`` is already in the project environment; ``--with
pyvista`` pulls in the GUI for this run only):

    uv run --with pyvista python examples/live_viewer_pyvista.py /tmp/b123d.sock

Design (see docs/live-viewer.md): a background reader thread parses the wire
frames into events and pushes them onto a queue; the main thread owns all
rendering, because VTK is not thread-safe. It drains the queue and applies
UPSERT/REMOVE/RESET to named actors, coalescing a burst into a single render.
"""

import argparse
import io
import json
import queue
import socket
import struct
import sys
import threading
import time


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed the connection")
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[dict, bytes]:
    """Read one length-prefixed frame: returns (header dict, binary payload)."""
    (json_len,) = struct.unpack(">I", _recv_exactly(sock, 4))
    header = json.loads(_recv_exactly(sock, json_len).decode("utf-8"))
    (bin_len,) = struct.unpack(">I", _recv_exactly(sock, 4))
    payload = _recv_exactly(sock, bin_len) if bin_len else b""
    return header, payload


class FrameReader(threading.Thread):
    """Daemon thread: connect, parse frames into event dicts, push onto a queue.

    Display-free (no pyvista/VTK), so the reader never touches the render window.
    An UPSERT's glb payload rides on the event under the ``glb`` key. Pushes
    ``{"type": "_EOF"}`` when the connection closes, ``{"type": "_ERROR"}`` if it
    cannot connect.
    """

    def __init__(self, sock_path: str, out: queue.Queue):
        super().__init__(daemon=True)
        self._sock_path = sock_path
        self._out = out

    def _connect(self) -> socket.socket:
        # Wait for the server to appear rather than failing fast, so the viewer
        # can be opened before the server is started. Close the window (or Ctrl-C)
        # to quit while waiting.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        announced = False
        while True:
            try:
                sock.connect(self._sock_path)
                return sock
            except (FileNotFoundError, ConnectionRefusedError):
                if not announced:
                    print(
                        f"waiting for the server socket at {self._sock_path} ...",
                        flush=True,
                    )
                    announced = True
                time.sleep(0.2)

    def run(self) -> None:
        try:
            sock = self._connect()
        except OSError as exc:
            self._out.put({"type": "_ERROR", "error": str(exc)})
            return
        with sock:
            while True:
                try:
                    header, payload = read_frame(sock)
                except (ConnectionError, OSError):
                    break
                event = dict(header)
                if payload:
                    event["glb"] = payload
                self._out.put(event)
        self._out.put({"type": "_EOF"})


def glb_to_mesh(glb: bytes):
    """Decode binary glb into a pyvista mesh. Call on the main thread only.

    trimesh.load of a glb returns a Scene (a multi-geometry container), so it is
    concatenated to a single mesh before wrapping into pyvista.
    """
    import pyvista as pv
    import trimesh

    loaded = trimesh.load(io.BytesIO(glb), file_type="glb")
    geom = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
    return pv.wrap(geom)


_COLORS = ["tan", "steelblue", "lightgreen", "lightcoral", "plum", "khaki"]


class PyvistaScene:
    """Apply scene events to a pyvista Plotter. Main-thread only (VTK actors)."""

    def __init__(self, plotter):
        self._plotter = plotter
        self._color_for: dict[str, str] = {}
        self._framed = False  # fit the camera once, then leave it to the user

    def clear(self) -> None:
        self._plotter.clear_actors()
        self._color_for.clear()
        self._framed = False

    def apply(self, event: dict) -> str:
        etype = event.get("type")
        if etype == "UPSERT":
            name = event["name"]
            color = self._color_for.setdefault(name, _COLORS[len(self._color_for) % len(_COLORS)])
            mesh = glb_to_mesh(event["glb"])
            # add_mesh(name=...) replaces the same-named actor in place.
            self._plotter.add_mesh(mesh, name=name, color=color, show_edges=True)
            if not self._framed:
                self._plotter.view_isometric()
                self._plotter.reset_camera()
                self._framed = True
        elif etype == "REMOVE":
            self._plotter.remove_actor(event.get("name"))
            self._color_for.pop(event.get("name"), None)
        elif etype == "RESET":
            self.clear()
        return etype or "?"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("socket_path", help="path to the viewer UDS")
    args = parser.parse_args()

    import pyvista as pv

    events: queue.Queue = queue.Queue()
    FrameReader(args.socket_path, events).start()

    plotter = pv.Plotter(window_size=(900, 700))
    plotter.set_background("white")
    plotter.add_text("build123d live viewer", font_size=10)
    scene = PyvistaScene(plotter)
    plotter.show(interactive_update=True, auto_close=False)
    print(f"viewer ready for {args.socket_path}; close the window to quit.")

    while True:
        rendered = False
        try:  # drain everything pending, then render once (coalesce bursts)
            while True:
                event = events.get_nowait()
                etype = event.get("type")
                if etype == "_ERROR":  # could not connect (e.g. permission denied)
                    print(f"reader error: {event.get('error')}", file=sys.stderr)
                    plotter.close()
                    return 1
                if etype == "_EOF":
                    # Server went away (e.g. it was stopped). Drop the now-stale
                    # geometry, keep the window open, and wait for it to return;
                    # on reconnect the server re-sends HELLO + a full-scene dump.
                    print("server disconnected; waiting for it to return ...")
                    scene.clear()
                    rendered = True
                    FrameReader(args.socket_path, events).start()
                    break
                scene.apply(event)
                rendered = True
        except queue.Empty:
            pass

        if rendered:
            plotter.render()
        try:  # pump the interactor so the window stays responsive
            plotter.update(stime=50)
        except Exception:  # noqa: BLE001 - the window was closed
            break
        if getattr(plotter, "render_window", True) is None:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
