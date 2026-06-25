#!/usr/bin/env python3
"""Minimal, dependency-free consumer for the build123d-mcp live viewer socket.

Connects to the Unix domain socket published by ``build123d-mcp --viewer-socket
PATH`` and prints each scene event as it arrives. Optionally writes every shape's
glTF-binary (glb) to a directory so you can open it in any 3D viewer.

This is a reference consumer for the wire protocol (see docs/live-viewer.md). It
uses only the Python standard library and renders nothing itself. A graphical
viewer (pyvista, three.js in a browser, …) can be built on the same frames.

Usage:
    python examples/live_viewer_client.py /tmp/b123d.sock
    python examples/live_viewer_client.py /tmp/b123d.sock --save-dir /tmp/scene
"""

import argparse
import json
import os
import socket
import struct
import sys


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


def _safe_filename(name: str) -> str | None:
    """Return a safe ``<name>.glb`` basename, or None if it cannot be made safe.

    Shape names come from the server (the agent's show() calls) and are not path
    validated, so strip any directory part and reject empty/dot names before
    using one as a path under --save-dir.
    """
    base = os.path.basename(name)
    if not base or base in (".", ".."):
        return None
    return f"{base}.glb"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket_path", help="path to the viewer UDS")
    parser.add_argument(
        "--save-dir",
        metavar="DIR",
        default="",
        help="if set, write each UPSERT's glb to DIR/<name>.glb",
    )
    args = parser.parse_args()

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(args.socket_path)
    except OSError as exc:
        print(f"could not connect to {args.socket_path}: {exc}", file=sys.stderr)
        return 1

    print(f"connected to {args.socket_path}")
    try:
        while True:
            header, payload = read_frame(sock)
            etype = header.get("type")
            seq = header.get("seq")
            name = header.get("name", "")
            if etype == "UPSERT":
                print(f"[{seq}] UPSERT {name}  ({len(payload)} bytes glb)")
                fname = _safe_filename(name) if args.save_dir else None
                if fname:
                    out = os.path.join(args.save_dir, fname)
                    with open(out, "wb") as f:
                        f.write(payload)
                    print(f"        wrote {out}")
            elif etype == "REMOVE":
                print(f"[{seq}] REMOVE {name}")
                fname = _safe_filename(name) if args.save_dir else None
                if fname:
                    try:
                        os.unlink(os.path.join(args.save_dir, fname))
                    except OSError:
                        pass
            elif etype == "RESET":
                print(f"[{seq}] RESET: scene cleared")
            elif etype == "HELLO":
                print(f"[{seq}] HELLO session={header.get('session_id')}")
            else:
                print(f"[{seq}] {etype} {header}")
    except (ConnectionError, KeyboardInterrupt) as exc:
        print(f"disconnected: {exc}")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
