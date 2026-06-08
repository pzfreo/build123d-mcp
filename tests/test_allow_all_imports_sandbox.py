"""--allow-all-imports relaxes only the import allowlist (issue #187).

The README documents that with ``--allow-all-imports`` the other sandbox
layers — restricted builtins, exec timeout, and the dunder-attribute block —
still apply. These tests pin that posture for ``check_ast``: import checks are
skipped, but blocked-builtin calls and dunder-attribute traversal are not.
"""

import pytest

import build123d_mcp.security as _sec
from build123d_mcp.security import check_ast


@pytest.fixture
def allow_all(monkeypatch):
    """Enable ALLOW_ALL_IMPORTS for the test, auto-restored afterwards."""
    monkeypatch.setattr(_sec, "ALLOW_ALL_IMPORTS", True)


def test_import_allowlist_relaxed(allow_all):
    """The whole point of the flag: arbitrary imports pass the AST check."""
    check_ast("import os")  # must not raise


def test_blocked_call_still_rejected(allow_all):
    """Blocked bare-name calls remain blocked under allow-all."""
    with pytest.raises(ValueError, match="not allowed"):
        check_ast("open('/etc/passwd')")


def test_dunder_traversal_still_rejected(allow_all):
    """Dunder-attribute traversal stays blocked under allow-all (issue #187)."""
    with pytest.raises(ValueError, match="dunder attribute"):
        check_ast("(1).__class__.__mro__")


def test_subclasses_dunder_still_rejected(allow_all):
    """The __subclasses__ escape chain stays blocked under allow-all."""
    with pytest.raises(ValueError, match="dunder attribute"):
        check_ast("().__class__.__base__.__subclasses__()")


def test_allowed_inspection_dunder_permitted(allow_all):
    """Read-only inspection dunders remain usable under allow-all."""
    check_ast("x.__class__")  # in _ALLOWED_DUNDER_ATTRS — must not raise
