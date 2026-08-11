"""Atomic, source-backed execution for reproducible generation workflows."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from build123d_mcp.tools._paths import safe_input_path

_DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024


def read_source(path: str) -> tuple[str, str, str]:
    """Read one safe UTF-8 Python source file and return path, text, SHA-256."""
    resolved = safe_input_path(path)
    if not resolved.lower().endswith(".py"):
        raise ValueError("execute_file() accepts only .py source files.")
    max_bytes = int(os.environ.get("BUILD123D_MAX_SCRIPT_BYTES", _DEFAULT_MAX_SOURCE_BYTES))
    size = os.path.getsize(resolved)
    if size > max_bytes:
        raise ValueError(
            f"Source file '{resolved}' is {size} bytes, exceeding the {max_bytes}-byte limit. "
            "Raise BUILD123D_MAX_SCRIPT_BYTES to allow a larger file."
        )
    with open(resolved, "rb") as handle:
        raw = handle.read()
    try:
        code = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Source file '{resolved}' is not valid UTF-8: {exc}") from exc
    return resolved, code, hashlib.sha256(raw).hexdigest()


def _active_state(session: Any) -> dict[str, Any]:
    return {
        "namespace": session.namespace,
        "current_shape": session.current_shape,
        "objects": session.objects,
        "object_groups": session.object_groups,
        "drawing_annotations": session.drawing_annotations,
        "drawing_page": session.drawing_page,
        "geometry_refs": session.geometry_refs,
        "source_provenance": getattr(session, "source_provenance", None),
        "viewer_baseline": session._viewer_baseline,
    }


def _restore_active_state(session: Any, state: dict[str, Any]) -> None:
    session.namespace = state["namespace"]
    session.current_shape = state["current_shape"]
    session.objects = state["objects"]
    session.object_groups = state["object_groups"]
    session.drawing_annotations = state["drawing_annotations"]
    session.drawing_page = state["drawing_page"]
    session.geometry_refs = state["geometry_refs"]
    session.source_provenance = state["source_provenance"]
    session._viewer_baseline = state["viewer_baseline"]


def execute_file_code(
    session: Any,
    code: str,
    source_path: str,
    source_sha256: str,
    result_name: str = "",
    snapshot: str = "",
) -> str:
    """Execute source in a clean namespace and atomically promote its geometry."""
    previous = _active_state(session)
    history_before = list(session.execute_history)

    session.namespace = {}
    session.current_shape = None
    session.objects = {}
    session.object_groups = {}
    session.drawing_annotations = {}
    session.drawing_page = None
    session.geometry_refs = {}
    session._viewer_baseline = {}
    session.execute_history = []
    session._inject_builtins()

    output = session.execute(code)
    failed = output.startswith("Error:") or output.startswith("Constraint failed:")
    if failed:
        _restore_active_state(session, previous)
        session.execute_history = history_before
        return json.dumps(
            {
                "ok": False,
                "source_path": source_path,
                "source_sha256": source_sha256,
                "error": output,
                "rolled_back": True,
            },
            indent=2,
        )

    if result_name:
        candidate = session.objects.get(result_name, session.namespace.get(result_name))
        try:
            from build123d import BuildPart, Shape

            if isinstance(candidate, BuildPart):
                candidate = candidate.part
            if not isinstance(candidate, Shape):
                candidate = None
        except ImportError:
            candidate = None
        if candidate is None:
            _restore_active_state(session, previous)
            session.execute_history = history_before
            return json.dumps(
                {
                    "ok": False,
                    "source_path": source_path,
                    "source_sha256": source_sha256,
                    "error": f"No build123d shape named '{result_name}' was produced.",
                    "rolled_back": True,
                },
                indent=2,
            )
        session.release_object_name(result_name)
        session.objects[result_name] = candidate
        session.current_shape = candidate

    if session.current_shape is None:
        _restore_active_state(session, previous)
        session.execute_history = history_before
        return json.dumps(
            {
                "ok": False,
                "source_path": source_path,
                "source_sha256": source_sha256,
                "error": "Source produced no shape; assign a Shape to `result` or call show().",
                "rolled_back": True,
            },
            indent=2,
        )

    session.source_provenance = {
        "path": source_path,
        "sha256": source_sha256,
        "result_name": result_name
        or next(
            (name for name, shape in session.objects.items() if shape is session.current_shape),
            "current_shape",
        ),
    }
    if snapshot:
        session.save_snapshot(snapshot)

    return json.dumps(
        {
            "ok": True,
            "source_path": source_path,
            "source_sha256": source_sha256,
            "result_name": session.source_provenance["result_name"],
            "snapshot": snapshot or None,
            "output": output,
        },
        indent=2,
    )
