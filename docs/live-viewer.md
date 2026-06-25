# Live session viewer

Stream the build123d session's geometry to an interactive 3D viewer over a Unix
domain socket (UDS), so a human can watch and rotate the model while an agent
drives the MCP server. The agent keeps getting its fixed-viewpoint `render_view`
PNGs; the human gets a live view that updates as the session changes.

This page documents the **server-side publisher** (the UDS interface). A native
viewer window is a separate, optional consumer. A minimal stdlib example ships in
[`examples/live_viewer_client.py`](../examples/live_viewer_client.py), and a richer
graphical viewer can be built on top of the same protocol.

## Enabling it

Start the server with a socket path:

```bash
build123d-mcp --viewer-socket /tmp/b123d.sock
# or
BUILD123D_VIEWER_SOCKET=/tmp/b123d.sock build123d-mcp
```

POSIX only (uses `AF_UNIX`). When the flag is absent the feature is fully inert
and a pure agent run pays nothing. When the flag is set, the server keeps a
current cache of the scene (tessellating changed shapes after each mutating
tool), so a viewer attaching at any time, early or late, gets a correct
full-scene dump; broadcasting to zero clients is a no-op.

Try it with the bundled example consumer:

```bash
python examples/live_viewer_client.py /tmp/b123d.sock
# add --save-dir DIR to also write each shape's glb to disk
```

## How it works

- The publisher runs in the **server** process on a background daemon thread. It
  never blocks the agent: broadcasts append to bounded per-client buffers, and a
  slow client has its oldest frames dropped rather than stalling the producer.
- After each geometry-mutating tool (`execute`, `reset`, `restore_snapshot`,
  `import_cad_file`, `load_part`), the server asks the worker for **per-shape
  deltas**. Only the shapes whose identity changed are tessellated, via the same
  bounded, hard-killable out-of-process path `render_view` uses, and encoded to
  glTF-binary (glb) with a small self-contained writer (no extra dependency, no
  OCC/VTK in the encode step).
- A viewer that connects mid-session receives `HELLO` followed by an `UPSERT` for
  every shape currently in the scene, so a late attach is correct.
- If the worker is restarted (e.g. after an `execute` timeout), the publisher
  emits `RESET` so clients clear their now-stale scene.
- While a viewer is configured, the server keeps one encoded glb per named shape
  (the cache that serves the on-connect dump), so the scene's geometry is held
  twice: as OCC geometry in the worker and as glb bytes in the server. For
  typical CAD parts this is negligible.

Updates are per-tool-call, not per-`show()`: the viewer reflects the session state
after each MCP call, which is the granularity a human reviewer watches at.

## Wire protocol

Length-prefixed frames:

```
[u32 BE json_len][json header (utf-8)][u32 BE bin_len][binary payload]
```

The JSON header is `{type, session_id, seq, name?, units:"mm"}`, where `seq` is
monotonic per publisher. Event types:

| type     | payload      | meaning                                              |
|----------|--------------|------------------------------------------------------|
| `HELLO`  | none         | sent first on connect; carries `session_id`          |
| `UPSERT` | binary glb   | add or replace the named shape with this mesh        |
| `REMOVE` | none         | remove the named shape from the scene                |
| `RESET`  | none         | clear the whole scene (worker restarted / `reset()`) |

The `UPSERT` payload is a standard binary glTF (glb) of a single shape, readable
by three.js' `GLTFLoader`, `trimesh`, `pyvista`, Blender, etc. Vertices are in
world-space millimetres.

A minimal client reads a frame as: read 4 bytes → `json_len`; read `json_len`
bytes → header; read 4 bytes → `bin_len`; read `bin_len` bytes → payload. See
`examples/live_viewer_client.py` for a complete, dependency-free reader.

## Scope and limitations

- POSIX only (AF_UNIX). A remote or headless host would need a UDS to WebSocket
  or TCP bridge, which is not included here.
- Per-shape deltas are detected by object identity. A shape mutated in place
  without re-registering it via `show()` keeps its identity and is not re-sent
  until the next `show()` rebinds the name (the established "re-show after edit"
  convention).
- The socket is created with mode 0600, so only the server's user can connect.
  The publisher also runs in the server process, outside the sandboxed
  `execute()` worker, so under the default sandbox user code cannot reach the
  socket. Starting the server with `--no-sandbox` or `--allow-all-imports` lifts
  the import allowlist, so worker code could then connect like any local process.
  The 0600 permission stays as the backstop.
