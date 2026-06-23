"""recover() heals an invalid solid, or fails without touching the geometry.

The contract recover() must honour: it either returns a faithful valid solid
(re-registered) or it fails and leaves the original shape exactly as it was — it
never hands back a distorted solid that merely happens to be watertight, and
never replaces a registered object on failure.

The heal-success path (defeature of an unorientable face, +0.005% volume) and the
single-solid fail-untouched path are validated end-to-end against benchmark
fixtures 217 and 240 in the cadgenbench-build123d harness — an unorientable face
cannot be constructed reliably in a few lines of build123d. These unit tests pin
the contracts that *are* constructible here: the already-valid no-op, the
single-solid guard, and the resolver error.
"""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.recover import recover


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def test_recover_already_valid_is_noop(session):
    """A solid that already passes the gate is returned as-is, untouched."""
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    before = session.objects["part"]
    out = recover(session, "part")
    assert "already valid" in out
    # No heal applied: the registered object is the very same instance.
    assert session.objects["part"] is before


def test_recover_refuses_multi_solid(session):
    """recover heals one body. A multi-solid shape is ambiguous (healing one while
    dropping another would look like a recovery), so it is refused with a clear
    message rather than silently operating on the first solid."""
    # Two boxes meeting at an edge stay a 2-solid compound (no manifold fuse).
    execute_code(session, "show(Box(10, 10, 10) + Pos(10, 10, 0) * Box(10, 10, 10), 'two')")
    before = session.objects["two"]
    out = recover(session, "two")
    payload = json.loads(out)
    assert payload["n_solids"] == 2
    assert "single solid" in payload["error"]
    # Refused, not healed: geometry untouched.
    assert session.objects["two"] is before


def test_recover_unknown_object_errors(session):
    """An unknown object name returns the resolver's error, not a crash."""
    out = recover(session, "nope")
    assert "Unknown object" in out
