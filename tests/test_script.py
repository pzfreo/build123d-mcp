"""Tests for the script() tool."""
import json
import os
import tempfile
import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.script import script


@pytest.fixture
def session():
    s = Session()
    return s


def test_two_blocks_in_script(session):
    session.execute("from build123d import *")
    session.execute("result = Box(10, 10, 10)")
    result = json.loads(script(session))
    assert "script" in result
    assert result["blocks"] == 2
    assert "from build123d import *" in result["script"]
    assert "Box(10, 10, 10)" in result["script"]


def test_failed_execute_not_included(session):
    session.execute("from build123d import *")
    # This should fail:
    session.execute("this is not valid python !!!")
    result = json.loads(script(session))
    # Only the successful first block should be in history
    assert result["blocks"] == 1
    assert "not valid" not in result["script"]


def test_reset_clears_history(session):
    session.execute("from build123d import *")
    session.execute("x = 1")
    assert len(session.execute_history) == 2
    session.reset()
    result = json.loads(script(session))
    assert result["blocks"] == 0
    assert result["script"] == ""


def test_prepends_build123d_import_when_missing(session):
    # Execute code without build123d import first
    session.execute("x = 1 + 1")
    result = json.loads(script(session))
    assert result["script"].startswith("from build123d import *")


def test_no_duplicate_import_when_already_present(session):
    session.execute("from build123d import *")
    session.execute("x = 1")
    result = json.loads(script(session))
    # Should only have one "from build123d import *"
    assert result["script"].count("from build123d import *") == 1


def test_save_to_writes_file(session):
    session.execute("from build123d import *")
    session.execute("result = Box(5, 5, 5)")
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
        path = tf.name
    try:
        result = json.loads(script(session, save_to=path))
        assert "script_path" in result
        assert result["blocks"] == 2
        assert os.path.exists(path)
        with open(path, "r") as f:
            content = f.read()
        assert "Box(5, 5, 5)" in content
    finally:
        os.unlink(path)


def test_blocks_separator(session):
    session.execute("from build123d import *")
    session.execute("a = Box(10, 10, 10)")
    session.execute("b = Cylinder(5, 20)")
    result = json.loads(script(session))
    assert result["blocks"] == 3
    # Blocks separated by double newline
    assert "\n\n" in result["script"]
