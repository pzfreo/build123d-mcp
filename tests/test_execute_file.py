"""Source-backed execution is clean, atomic, and provenance-bearing."""

import hashlib
import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute_file import execute_file_code, read_source
from build123d_mcp.tools.session_state import session_state
from build123d_mcp.worker import WorkerSession


def _source(tmp_path, text: str):
    path = tmp_path / "model.py"
    path.write_text(text, encoding="utf-8")
    resolved, code, digest = read_source(str(path))
    assert digest == hashlib.sha256(text.encode()).hexdigest()
    return resolved, code, digest


def test_execute_file_promotes_clean_model_with_provenance(tmp_path):
    session = Session()
    session.execute("from build123d import *\nstale = 99\nresult = Box(1, 1, 1)")
    path, code, digest = _source(
        tmp_path, "from build123d import *\nwidth = 4\nresult = Box(width, 3, 2)\n"
    )

    report = json.loads(execute_file_code(session, code, path, digest, snapshot="source-v1"))

    assert report["ok"] is True
    assert report["source_sha256"] == digest
    assert "stale" not in session.namespace
    assert session.execute_history == [code]
    assert session.current_shape.volume == pytest.approx(24)
    assert session.source_provenance["sha256"] == digest
    assert session.snapshots["source-v1"]["source_provenance"]["sha256"] == digest
    assert json.loads(session_state(session))["source_provenance"]["sha256"] == digest


def test_execute_file_runtime_failure_restores_active_state(tmp_path):
    session = Session()
    session.execute("from build123d import *\nshow(Box(2, 2, 2), 'safe')\nkept = 7")
    before = session.current_shape
    path, code, digest = _source(
        tmp_path,
        "from build123d import *\nshow(Box(9, 9, 9), 'partial')\nraise RuntimeError('boom')\n",
    )

    report = json.loads(execute_file_code(session, code, path, digest))

    assert report["ok"] is False and report["rolled_back"] is True
    assert session.current_shape is before
    assert set(session.objects) == {"safe"}
    assert session.namespace["kept"] == 7
    assert "partial" not in session.objects


def test_execute_file_requires_requested_result_and_rolls_back(tmp_path):
    session = Session()
    session.execute("from build123d import *\nshow(Box(2, 2, 2), 'safe')")
    before = session.current_shape
    path, code, digest = _source(tmp_path, "from build123d import *\nresult = Box(3, 3, 3)\n")

    report = json.loads(execute_file_code(session, code, path, digest, result_name="part"))

    assert report["ok"] is False
    assert "named 'part'" in report["error"]
    assert session.current_shape is before


def test_worker_execute_file_replaces_replay_root(tmp_path):
    path, code, digest = _source(tmp_path, "from build123d import *\nresult = Box(4, 3, 2)\n")
    worker = WorkerSession(exec_timeout=30)
    try:
        worker.execute("from build123d import *\nold = 1\nresult = Box(1, 1, 1)")
        report = json.loads(worker.execute_file(code, path, digest, snapshot="canonical"))
        assert report["ok"] is True
        assert worker._execute_history == [code]
        state = json.loads(worker.session_state())
        assert "old" not in state["variables"]
        assert state["source_provenance"]["sha256"] == digest
        assert "canonical" in state["snapshots"]
    finally:
        worker._kill_worker()


def test_read_source_rejects_non_python(tmp_path):
    path = tmp_path / "model.txt"
    path.write_text("result = 1", encoding="utf-8")
    with pytest.raises(ValueError, match="only .py"):
        read_source(str(path))
