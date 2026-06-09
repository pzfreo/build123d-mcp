import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.library import _LibraryIndex, load_part, search_library

# --- fixture helpers ---

PART_BOX = """\
PART_INFO = {
    "description": "A parametric box",
    "tags": ["basic", "box"],
    "parameters": {
        "width":  {"type": "float", "default": 10.0, "description": "width mm"},
        "height": {"type": "float", "default": 5.0,  "description": "height mm"},
    }
}
from build123d import *
def make(width=10.0, height=5.0):
    return Box(width, width, height)
"""

PART_CYLINDER = """\
PART_INFO = {
    "description": "A simple cylinder",
    "tags": ["basic", "round"],
    "parameters": {
        "radius": {"type": "float", "default": 3.0, "description": "radius mm"},
        "height": {"type": "float", "default": 8.0, "description": "height mm"},
    }
}
from build123d import *
def make(radius=3.0, height=8.0):
    return Cylinder(radius, height)
"""

# Part with no PART_INFO — still has make(), just no searchable metadata
PART_BARE = """\
from build123d import *
def make():
    return Box(2, 2, 2)
"""

# Part with no make() — should fail on load
PART_NO_MAKE = """\
PART_INFO = {"description": "broken part", "tags": [], "parameters": {}}
from build123d import *
result = Box(1, 1, 1)
"""


@pytest.fixture
def lib_dir(tmp_path):
    """Library with two parts at root and one in a subdirectory."""
    (tmp_path / "box.py").write_text(PART_BOX)
    (tmp_path / "cylinder.py").write_text(PART_CYLINDER)
    sub = tmp_path / "hardware"
    sub.mkdir()
    (sub / "bare.py").write_text(PART_BARE)
    return tmp_path


@pytest.fixture
def index(lib_dir):
    return _LibraryIndex(str(lib_dir))


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


# --- scanning ---


def test_scan_finds_root_parts(index):
    index.ensure_fresh()
    assert "box" in index._index
    assert "cylinder" in index._index


def test_scan_finds_subdirectory_part(index):
    index.ensure_fresh()
    assert "hardware/bare" in index._index
    assert index._index["hardware/bare"]["category"] == "hardware"


def test_scan_ignores_non_py_files(tmp_path):
    (tmp_path / "readme.txt").write_text("not a part")
    (tmp_path / "box.py").write_text(PART_BOX)
    idx = _LibraryIndex(str(tmp_path))
    idx.ensure_fresh()
    assert list(idx._index.keys()) == ["box"]


def test_scan_extracts_metadata(index):
    index.ensure_fresh()
    part = index._index["box"]
    assert part["description"] == "A parametric box"
    assert "box" in part["tags"]
    assert "width" in part["parameters"]
    assert part["parameters"]["width"]["default"] == 10.0


def test_scan_part_without_part_info(index):
    index.ensure_fresh()
    bare = index._index["hardware/bare"]
    assert bare["description"] == ""
    assert bare["tags"] == []
    assert bare["parameters"] == {}


# --- mtime invalidation ---


def test_mtime_triggers_rescan(tmp_path):
    (tmp_path / "box.py").write_text(PART_BOX)
    idx = _LibraryIndex(str(tmp_path))
    idx.ensure_fresh()
    assert "cylinder" not in idx._index

    # Write a new file and backdate _last_scan so mtime check fires
    (tmp_path / "cylinder.py").write_text(PART_CYLINDER)
    idx._last_scan = 0.0  # force rescan
    idx.ensure_fresh()
    assert "cylinder" in idx._index


def test_no_rescan_when_nothing_changed(tmp_path):
    (tmp_path / "box.py").write_text(PART_BOX)
    idx = _LibraryIndex(str(tmp_path))
    idx.ensure_fresh()
    scan_time = idx._last_scan
    idx.ensure_fresh()
    assert idx._last_scan == scan_time  # no re-scan happened


# --- search ---


def test_search_empty_returns_all(index):
    results = index.search("")
    names = {r["name"] for r in results}
    assert {"box", "cylinder", "hardware/bare"} == names


def test_search_by_keyword(index):
    results = index.search("box")
    assert any(r["name"] == "box" for r in results)
    assert all(r["name"] != "cylinder" for r in results)


def test_search_by_tag(index):
    results = index.search("round")
    assert len(results) == 1
    assert results[0]["name"] == "cylinder"


def test_search_by_category(index):
    results = index.search("hardware")
    assert len(results) == 1
    assert results[0]["name"] == "hardware/bare"


def test_search_no_match(index):
    results = index.search("nonexistent_xyz")
    assert results == []


def test_search_returns_parameter_specs(index):
    results = index.search("box")
    box = next(r for r in results if r["name"] == "box")
    assert "width" in box["parameters"]
    assert box["parameters"]["width"]["default"] == 10.0


def test_search_library_empty_message(tmp_path):
    idx = _LibraryIndex(str(tmp_path))
    result = search_library(idx, "")
    assert "empty" in result.lower()


def test_search_library_no_match_message(index):
    result = search_library(index, "xyz_not_found")
    assert "No parts found" in result


def test_search_library_returns_json(index):
    result = search_library(index, "box")
    data = json.loads(result)
    assert isinstance(data, list)
    assert data[0]["name"] == "box"


# --- load_part ---


def test_load_part_default_params(session, index):
    load_part(session, index, "box")
    assert "box" in session.objects
    assert abs(session.objects["box"].volume - 500.0) < 1.0  # 10*10*5


def test_load_part_with_override(session, index):
    load_part(session, index, "box", '{"width": 20.0}')
    # 20*20*5 = 2000
    assert abs(session.objects["box"].volume - 2000.0) < 1.0


def test_load_part_sets_current_shape(session, index):
    load_part(session, index, "cylinder")
    assert session.current_shape is session.objects["cylinder"]


def test_load_part_returns_feedback(session, index):
    result = load_part(session, index, "box")
    assert "box" in result
    assert "volume" in result
    assert "faces" in result


def test_load_part_bare_no_params(session, index):
    load_part(session, index, "hardware/bare")
    assert "hardware/bare" in session.objects
    assert abs(session.objects["hardware/bare"].volume - 8.0) < 0.1  # 2*2*2


def test_load_part_unknown_raises(session, index):
    with pytest.raises(ValueError, match="Unknown part"):
        load_part(session, index, "nonexistent")


def test_load_part_bad_json_raises(session, index):
    with pytest.raises(ValueError, match="Invalid params JSON"):
        load_part(session, index, "box", "{not valid json")


def test_load_part_unknown_param_raises(session, index):
    with pytest.raises(ValueError, match="Unknown parameters"):
        load_part(session, index, "box", '{"totally_wrong": 1.0}')


def test_load_part_no_make_raises(tmp_path, session):
    (tmp_path / "broken.py").write_text(PART_NO_MAKE)
    idx = _LibraryIndex(str(tmp_path))
    with pytest.raises(ValueError, match="no make\\(\\) function"):
        load_part(session, idx, "broken")


def test_load_part_blocked_import_raises(tmp_path, session):
    (tmp_path / "evil.py").write_text("import os\ndef make(): pass")
    idx = _LibraryIndex(str(tmp_path))
    with pytest.raises(ValueError, match="not allowed"):
        load_part(session, idx, "evil")


def test_server_library_guard_uses_session_has_library(monkeypatch):
    """The search_library/load_part wrappers gate on _session.has_library
    (#183 — replaces the separate server._has_library global). An inverted
    has_library would make these guard assertions fail, so a single
    no-library session covers both the property and the wrapper guard."""
    import build123d_mcp.server as srv
    from build123d_mcp.worker import WorkerSession

    no_lib = WorkerSession(exec_timeout=30)
    monkeypatch.setattr(srv, "_session", no_lib, raising=False)
    try:
        assert no_lib.has_library is False
        assert "No part library configured" in srv.search_library("x")
        assert "No part library configured" in srv.load_part("widget")
    finally:
        no_lib._kill_worker()
