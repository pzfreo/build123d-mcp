"""Shared path validation for file-reading and file-writing tools.

Allowed roots (identical for reads and writes):
  - the current working directory (the MCP server's `cwd`)
  - `tempfile.gettempdir()` (`$TMPDIR` on macOS, `/tmp` on most Linux)
  - `/tmp` itself when present (covers Linux installs where `gettempdir()`
    returns something else, and macOS where `/tmp` is a symlink to `/private/tmp`)

Validation runs after `os.path.realpath()`, so symlink escapes and `..`
traversal are rejected naturally rather than relying on textual checks.
"""

from __future__ import annotations

import contextvars
import os
import tempfile

_root_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "b123d_fs_root", default=None
)


def _allowed_roots() -> list[str]:
    roots: list[str] = [
        os.path.realpath(os.getcwd()),
        os.path.realpath(tempfile.gettempdir()),
    ]
    if os.path.isdir("/tmp"):
        slash_tmp = os.path.realpath("/tmp")
        if slash_tmp not in roots:
            roots.append(slash_tmp)
    pinned = _root_var.get()
    if pinned:
        pinned = os.path.realpath(pinned)
        if pinned not in roots:
            roots.append(pinned)
    return roots


def _safe_path(filename: str, kind: str) -> str:
    """Resolve `filename` and reject paths outside the allowed roots.

    `kind` is the word used in the error message ("read" or "write").
    Returns the resolved absolute path on success. Raises ``ValueError``
    if the resolved path is not under one of the allowed roots — this
    covers absolute paths to sensitive locations, `..` traversal, and
    symlink escapes in a single check.
    """
    resolved = os.path.realpath(filename)
    for root in _allowed_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    raise ValueError(
        f"Path '{filename}' resolves to '{resolved}', which is outside the allowed {kind} roots."
    )


def safe_output_path(filename: str) -> str:
    """Resolve `filename` and reject paths outside the allowed write roots.

    See ``_safe_path``; used by file-writing tools (export, render_view,
    script, render_drawing, save_drawing_annotations).
    """
    return _safe_path(filename, "write")


def safe_input_path(filename: str) -> str:
    """Resolve `filename` and reject paths outside the allowed read roots.

    See ``_safe_path``; used by file-reading tools (import_cad_file,
    render_drawing, inspect_drawing, lint_drawing) and their sidecar reads.
    """
    return _safe_path(filename, "read")


# Resource limits for model-supplied file inputs (issue #189). A model-callable
# server should reject obviously excessive inputs *before* the expensive
# parse/import/rasterise, rather than letting the work run until the exec
# timeout kills the worker and destroys session state. Defaults are generous —
# they reject only the absurd — and are env-overridable, matching the
# BUILD123D_EXEC_TIMEOUT convention. Each limit is the single source of truth:
# (default value, override env var). Read at call time so the override is
# honoured per process and tests can set it with monkeypatch.setenv.
_INPUT_SIZE_LIMITS = {
    "svg": (20 * 1024 * 1024, "BUILD123D_MAX_SVG_BYTES"),  # 20 MB
    "cad": (200 * 1024 * 1024, "BUILD123D_MAX_CAD_BYTES"),  # 200 MB
}
_RASTER_WIDTH_LIMIT = (10000, "BUILD123D_MAX_RASTER_WIDTH")  # px


def check_input_size(path: str, kind: str) -> None:
    """Reject an oversized input file before parsing/importing it.

    `kind` is ``"svg"`` or ``"cad"``, selecting the byte limit. No-op when the
    file is missing or unreadable — the caller's own existence check reports
    that. Raises ``ValueError`` when the file exceeds the limit; callers let it
    propagate, like the path-policy check above.
    """
    default_bytes, env_var = _INPUT_SIZE_LIMITS[kind]
    max_bytes = int(os.environ.get(env_var, default_bytes))
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size > max_bytes:
        raise ValueError(
            f"Input file '{path}' is {size} bytes, exceeding the {max_bytes}-byte limit "
            f"for {kind} inputs. Raise {env_var} to allow a larger file."
        )


def check_raster_width(width: int) -> None:
    """Reject an extreme raster width before the output bitmap is allocated.

    A bitmap is width × height × 4 bytes, so an unbounded width (e.g. 100000 px)
    can demand tens of GB. Raises ``ValueError`` when exceeded; the caller lets
    it propagate.
    """
    default_width, env_var = _RASTER_WIDTH_LIMIT
    max_width = int(os.environ.get(env_var, default_width))
    if width > max_width:
        raise ValueError(
            f"Requested raster width {width}px exceeds the {max_width}px limit. "
            f"Raise {env_var} to allow a larger raster."
        )
